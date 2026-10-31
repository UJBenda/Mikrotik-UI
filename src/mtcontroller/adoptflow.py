"""Adopce na jeden krok (UniFi „Adopt & forget“).

Po „Schválit a nastavit“ vede worker zařízení až do cílového stavu; stav je v `device.adopt_flow`:

    provision → inventory → [wifi_pkg] → takeover → [takeover_wait] → wifi → done

- provision: F0 (tunel, účet, check-in) – čeká na stav `adopted`,
- inventory: čerstvá inventura po schválení,
- wifi_pkg: role AP + switch a špatný ovladač Wi-Fi → příkaz `wifi_package` (restart), čeká na nový,
- takeover: podle třídy konfigurace (switchcfg.classify):
  * factory / simple → převzetí načisto (reset s návratem ze zálohy), bez ptaní,
  * conforming (jeden bridge `bridge` s vlan-filtering) → rovnou řízené + sladění (bez resetu),
  * custom → `needs_confirm`: uživatel zvolí „Přepsat načisto“ nebo „Ponechat ke sledování“,
- wifi: CAPsMAN lokality (vybere se sám – první řízený switch) a nasazení Wi-Fi na nové AP.

Čekání (bez sítí v lokalitě, AP bez řízeného switche s CAPsMANem) drží krok s vysvětlením
v `msg` a zkouší se znovu. Převzetí jde stejnou cestou jako ručně (plán, commit-confirm, audit).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from . import inventory, packages, switchcfg, takeover
from .config import Settings
from .db import Database, Tx, operator_scope, tenant_scope
from .security import SecretBox
from .services import ServiceError, _s, audit, enqueue, kick

log = logging.getLogger("mtcontroller.adoptflow")
TARGETS = ("switch", "ap", "monitor")
FINAL = ("done", "failed", "needs_confirm")
PKG_TIMEOUT = dt.timedelta(minutes=30)
STEP_TIMEOUT = dt.timedelta(hours=2)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def start(tx: Tx, tenant_id: Any, device_id: Any, target: str, ap_group: str | None, user_id: Any,
          can_apply: bool) -> dict | None:
    """Při schválení adopce: cílová role a skupina AP do záměru zařízení, řetězec od provisioningu."""
    if target not in TARGETS:
        raise ServiceError("role: switch, AP + switch, nebo jen sledovat")
    if target == "monitor":
        tx.execute("UPDATE device SET adopt_flow = NULL WHERE id = %(d)s", {"d": str(device_id)})
        return None
    tx.execute(
        """INSERT INTO device_intent (tenant_id, device_id, role, ap_group, updated_by)
           VALUES (%(t)s, %(d)s, %(r)s, %(g)s, %(u)s)
           ON CONFLICT (device_id) DO UPDATE SET role = EXCLUDED.role, ap_group = EXCLUDED.ap_group,
               updated_by = EXCLUDED.updated_by, updated_at = now()""",
        {"t": str(tenant_id), "d": str(device_id), "r": target, "g": ap_group if target == "ap" else None,
         "u": _s(user_id)})
    flow = {"target": target, "step": "provision", "msg": "zakládám správu (tunel, účet, check-in)",
            "user": _s(user_id), "can_apply": bool(can_apply), "started": _now(), "at": _now()}
    _save(tx, device_id, flow)
    return flow


def _save(tx: Tx, device_id: Any, flow: dict) -> None:
    tx.execute("UPDATE device SET adopt_flow = %(f)s WHERE id = %(d)s", {"f": flow, "d": str(device_id)})


def _go(tx: Tx, tenant_id: Any, device_id: Any, flow: dict, step: str, msg: str, **extra) -> dict:
    changed = flow.get("step") != step
    flow = {**flow, **extra, "step": step, "msg": msg, "at": _now() if changed else flow.get("at", _now())}
    _save(tx, device_id, flow)
    if changed:
        audit(tx, "device.adopt_flow", tenant_id=tenant_id, target=f"device:{device_id}", after={"step": step, "msg": msg})
    return flow


def _age(flow: dict) -> dt.timedelta:
    try:
        return dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(flow.get("at"))
    except (TypeError, ValueError):
        return dt.timedelta(0)


def decide(tx: Tx, tenant_id: Any, device_id: Any, action: str, user_id: Any = None, ip: str | None = None) -> dict | None:
    """Rozhodnutí uživatele u vlastní konfigurace (custom): přepsat načisto / jen sledovat / zkusit znovu."""
    d = tx.fetchone("SELECT adopt_flow, managed_at FROM device WHERE id = %(d)s", {"d": str(device_id)})
    if not d or not d["adopt_flow"]:
        raise ServiceError("zařízení nemá rozběhnutou adopci")
    flow = d["adopt_flow"]
    audit(tx, "device.adopt_decide", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
          after={"action": action, "step": flow.get("step")}, ip=ip)
    if action == "monitor":
        tx.execute("UPDATE device SET adopt_flow = NULL WHERE id = %(d)s", {"d": str(device_id)})
        return None
    if action == "overwrite":
        return _go(tx, tenant_id, device_id, {**flow, "user": _s(user_id) or flow.get("user")}, "takeover",
                   "převezmu načisto (uživatel potvrdil smazání vlastní konfigurace)", confirm_custom=True)
    if action == "retry":
        return _go(tx, tenant_id, device_id, flow, "inventory", "zkouším znovu od inventury")
    raise ServiceError("neznámá volba")


# --- jeden krok ----------------------------------------------------------------------------------------------

def _pick_capsman(tx: Tx, site_id: Any) -> str | None:
    """CAPsMAN lokality: první řízený switch (nejvíc portů = páteř)."""
    best = None
    for d in takeover._site_devices(tx, site_id):
        if d["managed_at"] is None or d["role"] != "switch" or d["state"] != "adopted":
            continue
        n = len(switchcfg.physical_ports(inventory.load(tx, d["id"], ["ethernet"])))
        if best is None or n > best[0]:
            best = (n, str(d["id"]))
    return best[1] if best else None


def _set_capsman(tx: Tx, tenant_id: Any, site_id: Any, host: str) -> None:
    cur = takeover.load_intent(tx, site_id)
    wifi = {**(cur.get("wifi") or {}), "capsman_device_id": host}
    tx.execute("UPDATE site_policy SET data = %(d)s, updated_at = now() WHERE site_id = %(s)s AND module = 'intent'",
               {"d": {**cur, "wifi": wifi}, "s": str(site_id)})
    audit(tx, "site.capsman_auto", tenant_id=tenant_id, target=f"site:{site_id}", after={"capsman_device_id": host})


def step(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, device_id: Any) -> str | None:
    """Posune řetězec jednoho zařízení o krok. Vrací nový krok (nebo None, když se nic nedělo)."""
    from . import autoapply

    after: list = []   # akce mimo transakci (schválení plánu, nasazení, probuzení)
    with tenant_scope(db, tenant_id) as tx:
        d = tx.fetchone(
            """SELECT id, state, site_id, managed_at, role::text AS role, adopt_flow, approved_at, inventory_at,
                      (inventory_at > now() - make_interval(mins => %(m)s)) AS fresh
               FROM device WHERE id = %(d)s FOR UPDATE""", {"d": str(device_id), "m": takeover.FRESH_MINUTES})
        if not d or not d["adopt_flow"] or d["adopt_flow"].get("step") in FINAL:
            return None
        flow = d["adopt_flow"]
        st = flow.get("step")
        if d["state"] == "removed":
            _go(tx, tenant_id, device_id, flow, "failed", "zařízení bylo odebráno")
            return "failed"
        if st == "provision":
            if d["state"] != "adopted":
                if _age(flow) > STEP_TIMEOUT:
                    _go(tx, tenant_id, device_id, flow, "failed", "provisioning se do 2 hodin nedokončil (tunel, check-in?)")
                    return "failed"
                return None
            _go(tx, tenant_id, device_id, flow, "inventory", "čekám na první inventuru")
            return "inventory"
        if st in ("inventory", "wifi_pkg"):
            started = flow.get("pkg_at") if st == "wifi_pkg" else flow.get("started")
            new_inv = d["fresh"] and d["inventory_at"] is not None and (
                not started or d["inventory_at"] > dt.datetime.fromisoformat(started))
            if not new_inv or d["state"] != "adopted":
                if st == "wifi_pkg" and _age(flow) > PKG_TIMEOUT:
                    _go(tx, tenant_id, device_id, flow, "failed", "výměna ovladače Wi-Fi se do 30 min neprojevila – zkontroluj zařízení")
                    return "failed"
                return None
            inv = inventory.load(tx, device_id)
            if flow["target"] == "ap":
                ws = packages.wifi_status(inv)
                if not ws["ok"]:
                    if ws["fix"] and st == "inventory":
                        enqueue(tx, tenant_id, device_id, "wifi_package", {}, flow.get("user"))
                        after.append(("kick",))
                        flow = _go(tx, tenant_id, device_id, flow, "wifi_pkg",
                                   f"měním ovladač Wi-Fi na {ws['fix']['install']} (restart)", pkg_at=_now())
                        st = "wifi_pkg"
                    else:
                        _go(tx, tenant_id, device_id, flow, "failed", f"Wi-Fi ovladač: {ws['problem']}")
                        return "failed"
            if st != "wifi_pkg" or packages.wifi_status(inv)["ok"]:
                flow = _go(tx, tenant_id, device_id, flow, "takeover", "převádím do řízení")
                st = "takeover"
        if st == "takeover":
            if _takeover(tx, settings, box, tenant_id, d, flow, after) == "plan":
                after.append(("plan",))
        elif st == "takeover_wait":
            p = tx.fetchone("SELECT status::text AS s FROM config_plan WHERE id = %(i)s", {"i": flow.get("plan_id")})
            s = (p or {}).get("s")
            if d["managed_at"] is not None:
                _go(tx, tenant_id, device_id, flow, "wifi", "nastavuji Wi-Fi lokality")
            elif s in ("rolled_back", "failed", "superseded", "discarded"):
                _go(tx, tenant_id, device_id, flow, "failed",
                    "převzetí se nepovedlo – zařízení se vrátilo do původní konfigurace (viz Aktivita → Změny)")
                return "failed"
            elif s == "draft" and _age(flow) > STEP_TIMEOUT:
                _go(tx, tenant_id, device_id, flow, "failed", "převzetí čeká na schválení déle než 2 hodiny")
                return "failed"
        elif st == "wifi":
            site_intent = takeover.load_intent(tx, d["site_id"]) or {}
            host = (site_intent.get("wifi") or {}).get("capsman_device_id")
            if not host and takeover.list_ssids(tx, d["site_id"]):
                host = _pick_capsman(tx, d["site_id"])
                if host:
                    _set_capsman(tx, tenant_id, d["site_id"], host)
            if host:
                after.append(("wifi",))
            _go(tx, tenant_id, device_id, flow, "done", "hotovo – zařízení je řízené")
        site_id = d["site_id"]
    # mimo transakci zámku zařízení
    for a in after:
        try:
            if a[0] == "kick":
                kick(db, settings, box, tenant_id, device_id)
            elif a[0] == "plan":
                _plan(db, settings, box, tenant_id, device_id)
            elif a[0] == "apply":
                autoapply.site(db, settings, box, tenant_id, site_id, flow.get("user"), None,
                               can_approve=flow.get("can_apply", True), wifi=False, only={str(device_id)})
            elif a[0] == "wifi":
                autoapply.site(db, settings, box, tenant_id, site_id, flow.get("user"), None,
                               can_approve=flow.get("can_apply", True), switches=False)
        except ServiceError as e:
            log.warning("adopce %s: %s", device_id, e)
            with tenant_scope(db, tenant_id) as tx:
                f = tx.fetchone("SELECT adopt_flow FROM device WHERE id = %(d)s", {"d": str(device_id)})["adopt_flow"]
                if f:
                    _go(tx, tenant_id, device_id, f, f["step"], f"{f['msg']} – {e}")
    with tenant_scope(db, tenant_id) as tx:
        cur = tx.fetchone("SELECT adopt_flow FROM device WHERE id = %(d)s", {"d": str(device_id)})["adopt_flow"]
    return (cur or {}).get("step")


def _takeover(tx: Tx, settings: Settings, box: SecretBox, tenant_id: Any, d: dict, flow: dict, after: list) -> str | None:
    did = d["id"]
    if d["state"] != "adopted":
        return None
    inv = inventory.load(tx, did)
    model, _di = takeover._model(tx, takeover._device(tx, did), inv)
    if model is None:
        _go(tx, tenant_id, did, flow, "takeover", "čekám: lokalita nemá sítě (Nastavení lokality → Sítě)")
        return None
    if not d["fresh"]:
        _go(tx, tenant_id, did, flow, "takeover", "čekám na čerstvou inventuru")
        return None
    cls = switchcfg.classify(inv, model["role"])
    if cls["class"] == "custom" and not flow.get("confirm_custom"):
        _go(tx, tenant_id, did, flow, "needs_confirm",
            "zařízení má vlastní konfiguraci (" + ", ".join(cls["custom"]) + ") – přepsat načisto, nebo ponechat ke sledování?",
            classification=cls["class"], removed=cls["removed"][:30])
        return "needs_confirm"
    if model["blockers"]:
        # např. AP bez řízeného switche s CAPsMANem – počká, zkusí znovu
        if model["role"] == "ap" and any("CAPsMAN" in b for b in model["blockers"]):
            host = _pick_capsman(tx, d["site_id"])
            if host:
                _set_capsman(tx, tenant_id, d["site_id"], host)
                return None  # příští krok už s CAPsMANem
        _go(tx, tenant_id, did, flow, "takeover", "čekám: " + "; ".join(model["blockers"]))
        return None
    if cls["class"] == "conforming" and any(str(b.get("name")) == switchcfg.BRIDGE for b in inv.get("bridges") or []):
        # VLAN už běží na jednom bridgi: bez resetu – řízené hned, rozdíl doladí sladění (s návratem)
        tx.execute("UPDATE device SET managed_at = now(), role = %(r)s WHERE id = %(d)s",
                   {"r": model["role"], "d": str(did)})
        audit(tx, "device.takeover_done", tenant_id=tenant_id, target=f"device:{did}", after={"class": "conforming"})
        after.append(("apply",))
        _go(tx, tenant_id, did, flow, "wifi", "řízené bez resetu – dolaďuji porty a VLAN podle sítí lokality",
            classification="conforming")
        return "wifi"
    return "plan"  # převzetí načisto: plán se vytvoří mimo zámek (vlastní transakce)


def run_all(db: Database, settings: Settings, box: SecretBox, limit: int = 50) -> int:
    """Krok workeru: posune rozběhnuté adopce (zařízení všech tenantů)."""
    with operator_scope(db) as tx:
        rows = tx.fetchall(
            """SELECT tenant_id, id FROM device WHERE adopt_flow IS NOT NULL
               AND adopt_flow->>'step' NOT IN ('done', 'failed', 'needs_confirm') ORDER BY approved_at LIMIT %(n)s""",
            {"n": limit})
    n = 0
    for r in rows:
        try:
            n += 1 if step(db, settings, box, r["tenant_id"], r["id"]) else 0
        except Exception as e:  # noqa: BLE001 – jedno zařízení nesmí zastavit ostatní
            log.warning("adopce %s: %s", r["id"], e)
    return n


def _plan(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, device_id: Any) -> None:
    """Převzetí načisto (factory / simple / potvrzené custom): plán, schválení, probuzení."""
    from .plans import approve_plan

    with tenant_scope(db, tenant_id) as tx:
        flow = tx.fetchone("SELECT adopt_flow FROM device WHERE id = %(d)s", {"d": str(device_id)})["adopt_flow"]
    try:
        res = takeover.create_takeover_plan(db, settings, box, tenant_id, device_id, bool(flow.get("confirm_custom")),
                                            flow.get("user"))
    except ServiceError as e:
        with tenant_scope(db, tenant_id) as tx:
            _go(tx, tenant_id, device_id, flow, "takeover", f"čekám: {e}")
        return
    msg = "převzetí načisto – záloha, reset a nasazení podle sítí lokality (návrat ze zálohy při výpadku)"
    if flow.get("can_apply", True):
        try:
            approve_plan(db, settings, tenant_id, res["plan_id"], flow.get("user"))
            kick(db, settings, box, tenant_id, device_id)
        except ServiceError as e:
            msg = f"převzetí čeká na schválení: {e}"
    else:
        msg = "převzetí čeká na schválení správcem (Aktivita → Změny)"
    with tenant_scope(db, tenant_id) as tx:
        _go(tx, tenant_id, device_id, flow, "takeover_wait", msg, plan_id=res["plan_id"])
