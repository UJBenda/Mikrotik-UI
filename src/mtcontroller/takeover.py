"""Řízený režim (F5a): záměr lokality, převzetí zařízení načisto a sladění se záměrem.

Převzetí jde jako plán (module 'takeover') stejnou cestou jako ostatní změny:
draft → schválení → příkaz apply_plan. Rozdíly:
- příkaz udělá zálohu, stáhne bootstrap (/api/checkin/bootstrap) a po ack resetuje
  konfiguraci s run-after-reset=bootstrap;
- bootstrap nastaví návrat ze zálohy, konfiguraci a znovu správu controllerem;
- potvrzení přijde, až se zařízení ozve s běžícím bootstrapem (značka tk=1)
  a s běžícím tunelem; po potvrzení je zařízení řízené (device.managed_at).
Sladění (module 'switch') počítá rozdíl modelu proti inventuře řízeného zařízení
a jde přes commit-confirm jako změna portů.
"""

from __future__ import annotations

from typing import Any

from . import dhcpguard, intent, inventory, ntp, routeros, switchcfg
from .config import Settings
from .db import Database, Tx, tenant_scope
from .security import SecretBox, random_password
from .services import NotFound, ServiceError, _s, audit, get_secret, put_secret

INTENT_MODULE = "intent"
TAKEOVER = "takeover"
SWITCH = "switch"
FRESH_MINUTES = 15  # převzetí jen z čerstvé inventury
BASIS_SECTIONS = ["ethernet", "bridges", "bridge_ports", "bridge_vlans", "vlans", "addresses", "dhcp_client",
                  "dhcp_servers", "fw_filter", "fw_nat"]
PLACEHOLDER = "********"


# --- záměr lokality ----------------------------------------------------------------------------

def load_intent(tx: Tx, site_id: Any) -> dict | None:
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s",
                    {"s": str(site_id), "m": INTENT_MODULE})
    return r["data"] if r else None


def _site_devices(tx: Tx, site_id: Any) -> list[dict]:
    return tx.fetchall(
        """SELECT id, serial, identity, model, role::text AS role, ros_version, state, managed_at
           FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline') ORDER BY identity, serial""",
        {"s": str(site_id)})


def site_intent_view(db: Database, tenant_id: Any, site_id: Any) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        cur = load_intent(tx, site_id)
        devs = _site_devices(tx, site_id)
        invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
    observed = dhcpguard.site_networks(devs, invs)["networks"]
    return {
        "intent": cur,
        "suggested": None if cur else intent.from_observed(observed),
        "purposes": intent.PURPOSES,
        "devices": [{"id": str(d["id"]), "name": d["identity"] or d["serial"], "model": d["model"], "role": d["role"],
                     "managed": d["managed_at"] is not None} for d in devs],
    }


def set_site_intent(db: Database, tenant_id: Any, site_id: Any, data: dict, user_id: Any = None,
                    ip: str | None = None) -> dict:
    try:
        new = intent.validate(data)
    except intent.IntentError as e:
        raise ServiceError(str(e)) from e
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        old = load_intent(tx, site_id)
        if old:
            # management VLAN řízených zařízení se nesmí změnit (zařízení by se neozvala)
            managed = tx.fetchone("SELECT count(*) AS n FROM device WHERE site_id = %(s)s AND managed_at IS NOT NULL",
                                  {"s": str(site_id)})["n"]
            if managed and (old["mgmt_vlan"] != new["mgmt_vlan"] or old["mgmt_native"] != new["mgmt_native"]):
                raise ServiceError("management síť nelze změnit, dokud jsou na lokalitě řízená zařízení")
        tx.execute(
            """INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
               VALUES (%(t)s, %(s)s, %(m)s, %(d)s, %(u)s)
               ON CONFLICT (site_id, module) DO UPDATE
                 SET data = EXCLUDED.data, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            {"t": str(tenant_id), "s": str(site_id), "m": INTENT_MODULE, "d": new, "u": _s(user_id)})
        audit(tx, "site.intent", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              before=old, after=new, ip=ip)
    return new


# --- výjimky zařízení -------------------------------------------------------------------------------

def load_device_intent(tx: Tx, device_id: Any) -> dict:
    r = tx.fetchone("SELECT ports, mgmt FROM device_intent WHERE device_id = %(d)s", {"d": str(device_id)})
    return {"ports": r["ports"], "mgmt": r["mgmt"]} if r else {"ports": {}, "mgmt": {}}


def set_device_intent(db: Database, tenant_id: Any, device_id: Any, data: dict, user_id: Any = None,
                      ip: str | None = None) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT site_id FROM device WHERE id = %(d)s AND state IN ('adopted', 'offline')",
                          {"d": str(device_id)})
        if not dev:
            raise NotFound("zařízení neexistuje nebo není adoptované")
        site_intent = load_intent(tx, dev["site_id"]) if dev["site_id"] else None
        inv = inventory.load(tx, device_id, ["ethernet"])
        names = {p["name"] for p in switchcfg.physical_ports(inv)}
        try:
            new = intent.validate_device(data, site_intent, names or None)
        except intent.IntentError as e:
            raise ServiceError(str(e)) from e
        if not (data.get("mgmt") or {}).get("mode"):
            new["mgmt"] = {}
        old = load_device_intent(tx, device_id)
        tx.execute(
            """INSERT INTO device_intent (tenant_id, device_id, ports, mgmt, updated_by)
               VALUES (%(t)s, %(d)s, %(p)s, %(m)s, %(u)s)
               ON CONFLICT (device_id) DO UPDATE SET ports = EXCLUDED.ports, mgmt = EXCLUDED.mgmt,
                   updated_by = EXCLUDED.updated_by, updated_at = now()""",
            {"t": str(tenant_id), "d": str(device_id), "p": new["ports"], "m": new["mgmt"], "u": _s(user_id)})
        audit(tx, "device.intent", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              before=old, after=new, ip=ip)
    return new


# --- model a náhled převzetí ---------------------------------------------------------------------------

def _device(tx: Tx, device_id: Any) -> dict:
    d = tx.fetchone(
        """SELECT id, serial, identity, model, role::text AS role, state, site_id, managed_at, inventory_at,
                  api_cert_fingerprint, (inventory_at > now() - make_interval(mins => %(m)s)) AS fresh
           FROM device WHERE id = %(d)s""", {"d": str(device_id), "m": FRESH_MINUTES})
    if not d:
        raise NotFound("zařízení neexistuje")
    return d


def _model(tx: Tx, d: dict, inv: dict) -> tuple[dict | None, dict]:
    site_intent = load_intent(tx, d["site_id"]) if d["site_id"] else None
    if not site_intent:
        return None, {}
    pol = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = 'ntp'", {"s": str(d["site_id"])})
    ntp_pol = (pol or {}).get("data") or {"upstream": ntp.DEFAULT_UPSTREAM, "timezone": ntp.DEFAULT_TZ}
    dev_intent = load_device_intent(tx, d["id"])
    model = switchcfg.build(inv, site_intent, dev_intent, model_name=d["model"] or "",
                            identity=d["identity"] or d["serial"], ntp=ntp_pol)
    return model, dev_intent


def preview(db: Database, tenant_id: Any, device_id: Any) -> dict:
    """Co převzetí udělá: třída konfigurace, co zanikne, role portů, VLAN, překážky."""
    with tenant_scope(db, tenant_id) as tx:
        d = _device(tx, device_id)
        inv = inventory.load(tx, device_id)
        model, dev_intent = _model(tx, d, inv)
        active = tx.fetchone(
            """SELECT id, module, status FROM config_plan WHERE device_id = %(d)s AND module IN ('takeover', 'switch')
               AND status IN ('draft', 'queued', 'confirming') ORDER BY id DESC LIMIT 1""", {"d": str(device_id)})
    out: dict[str, Any] = {
        "managed": d["managed_at"] is not None, "managed_at": d["managed_at"], "state": d["state"],
        "has_intent": model is not None, "device_intent": dev_intent, "active_plan": active,
        "ports": [{"name": p["name"], "port": p["port"]} for p in switchcfg.physical_ports(inv)],
    }
    if model is None:
        out["blockers"] = ["lokalita nemá záměr – nejdřív nadefinuj sítě (Lokalita → Sítě → Záměr)"]
        return out
    cls = switchcfg.classify(inv)
    blockers = list(model["blockers"])
    if not inv:
        blockers.insert(0, "inventura zařízení zatím neproběhla")
    elif not d["fresh"]:
        blockers.insert(0, "inventura je starší než 15 minut – počkej na novou")
    if d["state"] != "adopted":
        blockers.insert(0, "zařízení není online")
    if d["role"] == "gateway":
        blockers.append("zařízení má roli gateway – řízený režim zatím umí jen switch")
    out.update(model=model, classification=cls, blockers=blockers, warnings=model["warnings"])
    if d["managed_at"] is not None:
        r = switchcfg.reconcile(inv, model)
        out["reconcile"] = {"changes": r["changes"], "additive": r["additive"]}
    return out


def create_takeover_plan(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, device_id: Any,
                         confirm_custom: bool = False, user_id: Any = None, ip: str | None = None) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        d = _device(tx, device_id)
        if d["managed_at"] is not None:
            raise ServiceError("zařízení už je řízené – změny dělej sladěním se záměrem")
        if d["state"] != "adopted":
            raise ServiceError("zařízení není online")
        inv = inventory.load(tx, device_id)
        if not inv or not d["fresh"]:
            raise ServiceError("inventura chybí nebo je starší než 15 minut – počkej na novou")
        if d["role"] == "gateway":
            raise ServiceError("zařízení má roli gateway – řízený režim zatím umí jen switch")
        model, _di = _model(tx, d, inv)
        if model is None:
            raise ServiceError("lokalita nemá záměr – nejdřív nadefinuj sítě")
        if model["blockers"]:
            raise ServiceError("převzetí nelze: " + "; ".join(model["blockers"]))
        cls = switchcfg.classify(inv)
        if cls["class"] == "custom" and not confirm_custom:
            raise ServiceError("zařízení má vlastní konfiguraci (" + ", ".join(cls["custom"]) +
                               ") – převzetí ji smaže; potvrď to výslovně")
        if tx.fetchone("""SELECT 1 FROM config_plan WHERE device_id = %(d)s AND module = 'takeover'
                          AND status IN ('queued', 'confirming')""", {"d": str(device_id)}):
            raise ServiceError("převzetí tohoto zařízení už probíhá")
        tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s
                      AND module = 'takeover' AND status = 'draft'""", {"d": str(device_id)})
        put_secret(tx, box, tenant_id, device_id, "takeover_backup", random_password())
        preview_script = _bootstrap(settings, model, "<sstp>", "<api>", "<klic>", PLACEHOLDER, 0, preview=True)
        summary = (f"Převzetí do řízení ({cls['class']}): {len(model['vlans'])} sítí, "
                   f"uplink {model['uplink']}, trunky {sum(1 for p in model['ports'] if p['mode'] == 'trunk')}, "
                   f"přístupové {sum(1 for p in model['ports'] if p['mode'] == 'access')}")
        row = tx.fetchone(
            """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                        confirm_minutes, basis_sha, created_by)
               VALUES (%(t)s, %(d)s, 'takeover', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "d": str(device_id), "sum": summary,
             "chg": {"model": model, "class": cls["class"], "removed": cls["removed"], "warnings": model["warnings"],
                     "fp_before": d["api_cert_fingerprint"]},
             "sc": preview_script, "rv": "zaloha .backup pred resetem (system backup load)",
             "cm": switchcfg.TAKEOVER_CONFIRM_MINUTES, "b": inventory.basis_sha(inv, BASIS_SECTIONS), "u": _s(user_id)})
        audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"plan_id": row["id"], "module": "takeover", "class": cls["class"], "summary": summary}, ip=ip)
    return {"plan_id": row["id"], "summary": summary, "warnings": model["warnings"], "removed": cls["removed"]}


# --- doručení na zařízení ----------------------------------------------------------------------------------

def _urls(settings: Settings, command_id: int) -> dict:
    base = f"https://{settings.web.checkin_host}"
    return {"checkin": f"{base}/api/checkin", "ack": f"{base}/api/checkin/ack?id={command_id}",
            "bootstrap": f"{base}/api/checkin/bootstrap?id={command_id}"}


def _bootstrap(settings: Settings, model: dict, sstp_pw: str, api_pw: str, key: str, backup_pw: str,
               command_id: int, *, serial: str = "SERIAL", preview: bool = False) -> str:
    from . import ipam
    from .services import _concentrator, sstp_username

    opts = routeros.FetchOpts(settings.security.device_check_certificate)
    conc = _concentrator(settings, None)
    adopt = routeros.provision_body(routeros.ProvisionParams(
        mgmt_source=str(ipam.concentrator_address(settings.tunnel.pool)), connect_host=conc.connect_host,
        sstp_user=sstp_username(serial), sstp_password=sstp_pw, api_password=api_pw), opts)
    u = _urls(settings, command_id)
    install = routeros.checkin_install(u["checkin"], key, opts)
    if preview:
        # v náhledu (DB, UI) bez tajemství a bez dlouhých vložených skriptů
        adopt = "# sprava controllerem: SSTP tunel, ucet mtc-ctl, certifikat REST, firewall mtc (jako pri adopci)"
        install = "# skript a scheduler mtc-checkin (jako pri adopci)"
    return switchcfg.bootstrap(model, adopt, install, reverted_url=u["ack"] + "&status=reverted",
                               auth_header="Authorization: Bearer " + key, check_certificate=opts.check_certificate,
                               backup_password=backup_pw)


def render_takeover(tx: Tx, settings: Settings, box: SecretBox, cmd: dict, dev: dict, ack_url: str, key: str) -> str:
    """Příkaz před resetem: záloha + stažení bootstrapu; reset až po ack (tail)."""
    opts = routeros.FetchOpts(settings.security.device_check_certificate)
    u = _urls(settings, cmd["id"])
    body = switchcfg.takeover_body(u["bootstrap"], "Authorization: Bearer " + key, opts.check_certificate,
                                   get_secret(tx, box, dev["id"], "takeover_backup") or "")
    return routeros.wrap_command(ack_url, key, body, opts, tail=switchcfg.TAKEOVER_TAIL)


def bootstrap_script(db: Database, settings: Settings, box: SecretBox, device_ref: dict, command_id: int,
                     ip: str | None = None) -> str:
    """Bootstrap pro zařízení, které právě provádí schválené převzetí (příkaz ve stavu sent)."""
    with tenant_scope(db, device_ref["tenant_id"]) as tx:
        row = tx.fetchone(
            """SELECT c.id, p.changes, d.serial FROM device_command c
               JOIN config_plan p ON p.id = c.plan_id JOIN device d ON d.id = c.device_id
               WHERE c.id = %(c)s AND c.device_id = %(d)s AND c.kind = 'apply_plan' AND c.status = 'sent'
                 AND p.module = 'takeover' AND p.status = 'queued'""",
            {"c": command_id, "d": str(device_ref["id"])})
        if not row:
            raise NotFound("žádné probíhající převzetí")
        did = device_ref["id"]
        script = _bootstrap(settings, row["changes"]["model"], get_secret(tx, box, did, "sstp_password") or "",
                            get_secret(tx, box, did, "api_password") or "", get_secret(tx, box, did, "checkin_key") or "",
                            get_secret(tx, box, did, "takeover_backup") or "", command_id, serial=row["serial"])
        audit(tx, "device.bootstrap_fetch", tenant_id=device_ref["tenant_id"], target=f"device:{did}",
              after={"command_id": command_id}, ip=ip)
    return script


def takeover_active(tx: Tx, device_id: Any) -> bool:
    return tx.fetchone("""SELECT 1 FROM config_plan WHERE device_id = %(d)s AND module = 'takeover'
                          AND status IN ('queued', 'confirming')""", {"d": str(device_id)}) is not None


def finished(tx: Tx, tenant_id: Any, plan_id: int, ok: bool) -> None:
    """Výsledek potvrzení převzetí: ok → zařízení je řízené; jinak návrat (záloha)."""
    p = tx.fetchone("SELECT device_id, changes FROM config_plan WHERE id = %(i)s", {"i": plan_id})
    if not p:
        return
    did = p["device_id"]
    if ok:
        tx.execute("UPDATE device SET managed_at = now(), role = 'switch', inventory_at = NULL WHERE id = %(d)s",
                   {"d": str(did)})
        audit(tx, "device.takeover_done", tenant_id=tenant_id, target=f"device:{did}", after={"plan_id": plan_id})
    else:
        tx.execute("""UPDATE config_plan SET status = 'rolled_back', finished_at = now()
                      WHERE id = %(i)s AND status IN ('confirming', 'queued')""", {"i": plan_id})
        rolled_back(tx, tenant_id, plan_id)


def rolled_back(tx: Tx, tenant_id: Any, plan_id: int) -> None:
    """Zařízení je zpět v původní konfiguraci (záloha) – i s původním certifikátem REST."""
    p = tx.fetchone("SELECT device_id, changes FROM config_plan WHERE id = %(i)s AND module = 'takeover'", {"i": plan_id})
    if not p:
        return
    fp = (p["changes"] or {}).get("fp_before")
    if fp:
        tx.execute("UPDATE device SET api_cert_fingerprint = %(f)s, inventory_at = NULL WHERE id = %(d)s",
                   {"f": fp, "d": str(p["device_id"])})
    audit(tx, "device.takeover_rolled_back", tenant_id=tenant_id, target=f"device:{p['device_id']}",
          after={"plan_id": plan_id})


# --- sladění řízených zařízení ------------------------------------------------------------------------------

def generate_switch_plans(db: Database, settings: Settings, tenant_id: Any, site_id: Any, user_id: Any = None,
                          ip: str | None = None, auto: bool = True) -> list[dict]:
    """Plány sladění pro řízená zařízení lokality. Při auto_trunk v záměru se plány,
    které jen přidávají tagované VLAN, rovnou schválí (nemohou přerušit provoz)."""
    from .plans import CONFIRM_MINUTES, approve_plan

    out: list[dict] = []
    auto_ids: list[int] = []
    with tenant_scope(db, tenant_id) as tx:
        site_intent = load_intent(tx, site_id)
        if not site_intent:
            raise ServiceError("lokalita nemá záměr")
        for d in _site_devices(tx, site_id):
            if d["managed_at"] is None:
                continue
            did = str(d["id"])
            item = {"device_id": did, "device": d["identity"] or d["serial"]}
            dd = _device(tx, did)
            inv = inventory.load(tx, did)
            if not inv:
                out.append(item | {"result": "skipped", "reason": "inventura zatím neproběhla"})
                continue
            model, _di = _model(tx, dd, inv)
            if model["blockers"]:
                out.append(item | {"result": "blocked", "reason": "; ".join(model["blockers"])})
                continue
            r = switchcfg.reconcile(inv, model)
            tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s
                          AND module = 'switch' AND status = 'draft'""", {"d": did})
            if not r["changes"]:
                out.append(item | {"result": "ok"})
                continue
            row = tx.fetchone(
                """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                            confirm_minutes, basis_sha, created_by)
                   VALUES (%(t)s, %(d)s, 'switch', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
                {"t": str(tenant_id), "d": did, "sum": "Sladění se záměrem: " + r["summary"],
                 "chg": {"changes": r["changes"], "additive": r["additive"], "warnings": model["warnings"]},
                 "sc": r["apply"], "rv": r["revert"], "cm": CONFIRM_MINUTES, "b": switchcfg.basis(inv),
                 "u": _s(user_id)})
            out.append(item | {"result": "plan", "plan_id": row["id"], "summary": r["summary"], "additive": r["additive"]})
            if auto and r["additive"] and site_intent.get("auto_trunk", True):
                auto_ids.append(row["id"])
        audit(tx, "plan.generate", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"module": "switch", "plans": sum(1 for o in out if o["result"] == "plan"), "auto": len(auto_ids)},
              ip=ip)
    for pid in auto_ids:
        try:
            approve_plan(db, settings, tenant_id, pid, user_id, ip)
            next(o for o in out if o.get("plan_id") == pid)["result"] = "auto"
        except ServiceError as e:
            next(o for o in out if o.get("plan_id") == pid)["reason"] = str(e)
    return out

