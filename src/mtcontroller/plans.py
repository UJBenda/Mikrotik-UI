"""Plány změn konfigurace (požadovaný stav → diff → schválení → aplikace).

Zásady:
- Modul smí na zařízení zapisovat jen v režimu `manage` (device_module).
  Výchozí je `observe` – controller jen čte a ukazuje odchylky.
- Plán vzniká z konkrétní inventury (basis_sha). Pokud se zařízení mezitím
  změní, plán se při schválení zneplatní (superseded) a je nutné ho vygenerovat znovu.
- U modulů v security.four_eyes_modules musí plán schválit jiný uživatel než autor.
- Aplikace jde přes frontu příkazů (kind 'apply_plan'); výsledek se potvrdí ackem.
"""

from __future__ import annotations

from typing import Any

from . import inventory, ntp, portcfg
from .config import Settings
from .db import Database, Tx, tenant_scope
from .services import NotFound, ServiceError, _s, audit, enqueue

MODULES = {"ntp", "ports", "vlan", "firewall", "capsman"}
MANAGEABLE = {"ntp", "ports"}
CONFIRM_MINUTES = 5  # výchozí okno pro potvrzení změny s automatickým návratem


def basis_for(module: str, inv: dict) -> str:
    if module == "ports":
        return portcfg.config_basis(inv)
    return inventory.basis_sha(inv, ntp.SECTIONS if module == "ntp" else [])


def set_module_mode(db: Database, tenant_id: Any, device_id: Any, module: str, mode: str,
                    user_id: Any = None, ip: str | None = None) -> None:
    if module not in MODULES or mode not in ("observe", "manage"):
        raise ServiceError("neplatný modul nebo režim")
    if module not in MANAGEABLE and mode == "manage":
        raise ServiceError(f"modul {module} zatím umí jen režim observe")
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state <> 'removed'", {"d": str(device_id)}):
            raise NotFound("zařízení neexistuje")
        tx.execute(
            """INSERT INTO device_module (tenant_id, device_id, module, mode, changed_by)
               VALUES (%(t)s, %(d)s, %(m)s, %(mode)s, %(u)s)
               ON CONFLICT (device_id, module) DO UPDATE
                 SET mode = EXCLUDED.mode, changed_by = EXCLUDED.changed_by, changed_at = now()""",
            {"t": str(tenant_id), "d": str(device_id), "m": module, "mode": mode, "u": _s(user_id)})
        audit(tx, "device.module_mode", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"module": module, "mode": mode}, ip=ip)


def module_modes(tx: Tx, device_id: Any) -> dict[str, str]:
    rows = tx.fetchall("SELECT module, mode FROM device_module WHERE device_id = %(d)s", {"d": str(device_id)})
    return {m: "observe" for m in MODULES} | {r["module"]: r["mode"] for r in rows}


def set_site_policy(db: Database, tenant_id: Any, site_id: Any, module: str, data: dict,
                    user_id: Any = None, ip: str | None = None) -> dict:
    if module != "ntp":
        raise ServiceError("politika lokality je zatím jen pro NTP")
    try:
        policy = ntp.validate_policy(data)
    except ntp.PolicyError as e:
        raise ServiceError(str(e)) from e
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT site_id, state FROM device WHERE id = %(d)s",
                          {"d": policy["primary_device_id"]})
        if not dev or str(dev["site_id"]) != str(site_id):
            raise ServiceError("primární zařízení musí patřit do této lokality")
        tx.execute(
            """INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
               VALUES (%(t)s, %(s)s, %(m)s, %(d)s, %(u)s)
               ON CONFLICT (site_id, module) DO UPDATE
                 SET data = EXCLUDED.data, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            {"t": str(tenant_id), "s": str(site_id), "m": module, "d": policy, "u": _s(user_id)})
        audit(tx, "site.policy", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"module": module, **policy}, ip=ip)
    return policy


def get_site_policy(tx: Tx, site_id: Any, module: str) -> dict | None:
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s",
                    {"s": str(site_id), "m": module})
    return r["data"] if r else None


def generate_ntp_plans(db: Database, tenant_id: Any, site_id: Any, user_id: Any = None,
                       ip: str | None = None) -> list[dict]:
    """Pro každé adoptované zařízení lokality spočítá rozdíl proti politice
    a vytvoří plán (draft). Vrací přehled včetně zařízení beze změn."""
    out = []
    with tenant_scope(db, tenant_id) as tx:
        policy = get_site_policy(tx, site_id, "ntp")
        if not policy:
            raise ServiceError("lokalita nemá nastavenou NTP politiku")
        devs = tx.fetchall(
            """SELECT id, serial, identity FROM device WHERE site_id = %(s)s
               AND state IN ('adopted', 'offline') ORDER BY serial""", {"s": str(site_id)})
        invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
        primary_inv = invs.get(policy["primary_device_id"])
        for d in devs:
            did = str(d["id"])
            inv = invs[did]
            item = {"device_id": did, "serial": d["serial"], "identity": d["identity"]}
            if not inv:
                out.append(item | {"result": "skipped", "reason": "inventura zatím neproběhla"})
                continue
            desired, reason = ntp.desired_for(policy, did, inv, primary_inv)
            if desired is None:
                out.append(item | {"result": "skipped", "reason": reason})
                continue
            chg = ntp.changes(desired, inv)
            tx.execute(
                """UPDATE config_plan SET status = 'superseded'
                   WHERE device_id = %(d)s AND module = 'ntp' AND status = 'draft'""", {"d": did})
            if not chg:
                out.append(item | {"result": "ok"})
                continue
            row = tx.fetchone(
                """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, basis_sha, created_by)
                   VALUES (%(t)s, %(d)s, 'ntp', %(sum)s, %(chg)s, %(sc)s, %(b)s, %(u)s) RETURNING id""",
                {"t": str(tenant_id), "d": did, "sum": ntp.summary(chg), "chg": chg,
                 "sc": ntp.script(desired, chg), "b": inventory.basis_sha(inv, ntp.SECTIONS), "u": _s(user_id)})
            out.append(item | {"result": "plan", "plan_id": row["id"], "summary": ntp.summary(chg)})
        audit(tx, "plan.generate", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"module": "ntp", "plans": sum(1 for o in out if o["result"] == "plan")}, ip=ip)
    return out


def approve_plan(db: Database, settings: Settings, tenant_id: Any, plan_id: int, user_id: Any,
                 ip: str | None = None) -> int:
    superseded = False
    with tenant_scope(db, tenant_id) as tx:
        p = tx.fetchone(
            """SELECT p.id, p.device_id, p.module, p.status, p.basis_sha, p.created_by, d.state
               FROM config_plan p JOIN device d ON d.id = p.device_id
               WHERE p.id = %(i)s FOR UPDATE OF p""", {"i": plan_id})
        if not p:
            raise NotFound("plán neexistuje")
        if p["status"] != "draft":
            raise ServiceError(f"plán není ve stavu draft (je {p['status']})")
        if p["module"] == "vlan":
            raise ServiceError("plány VLAN jsou zatím jen náhled")
        if module_modes(tx, p["device_id"]).get(p["module"]) != "manage":
            raise ServiceError(f"modul {p['module']} je na zařízení v režimu observe – nejdřív přepni na manage")
        if p["state"] not in ("adopted", "offline"):
            raise ServiceError("zařízení není adoptované")
        if p["module"] in settings.security.four_eyes_modules and str(p["created_by"]) == str(user_id):
            raise ServiceError("tento plán musí schválit jiný uživatel než autor")
        inv = inventory.load(tx, p["device_id"])
        if basis_for(p["module"], inv) != p["basis_sha"]:
            tx.execute("UPDATE config_plan SET status = 'superseded' WHERE id = %(i)s", {"i": plan_id})
            superseded = True
        else:
            cid = enqueue(tx, tenant_id, p["device_id"], "apply_plan", {"plan_id": plan_id}, user_id)
            tx.execute("UPDATE device_command SET plan_id = %(p)s WHERE id = %(c)s", {"p": plan_id, "c": cid})
            tx.execute(
                """UPDATE config_plan SET status = 'queued', approved_by = %(u)s, approved_at = now(),
                       command_id = %(c)s WHERE id = %(i)s""", {"u": _s(user_id), "c": cid, "i": plan_id})
            audit(tx, "plan.approve", tenant_id=tenant_id, user_id=user_id, target=f"device:{p['device_id']}",
                  after={"plan_id": plan_id, "module": p["module"], "command_id": cid}, ip=ip)
    if superseded:
        raise ServiceError("konfigurace zařízení se od vytvoření plánu změnila – vygeneruj plán znovu")
    return cid


def discard_plan(db: Database, tenant_id: Any, plan_id: int, user_id: Any, ip: str | None = None) -> None:
    with tenant_scope(db, tenant_id) as tx:
        n = tx.execute("UPDATE config_plan SET status = 'discarded' WHERE id = %(i)s AND status = 'draft'",
                       {"i": plan_id})
        if not n:
            raise ServiceError("plán nelze zahodit")
        audit(tx, "plan.discard", tenant_id=tenant_id, user_id=user_id, target=f"plan:{plan_id}", ip=ip)


def create_port_plan(db: Database, tenant_id: Any, device_id: Any, port: str, desired: dict,
                     user_id: Any = None, ip: str | None = None, uplinks: set[str] | None = None) -> dict:
    """Plán změny portu (draft) s návratovým skriptem; předchozí draft pro stejný port zruší."""
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state IN ('adopted', 'offline')",
                           {"d": str(device_id)}):
            raise NotFound("zařízení neexistuje nebo není adoptované")
        inv = inventory.load(tx, device_id)
        if not inv:
            raise ServiceError("inventura zařízení zatím neproběhla")
        try:
            p = portcfg.plan(inv, port, desired, uplinks)
        except portcfg.PortError as e:
            raise ServiceError(str(e)) from e
        if not p["changes"]:
            raise ServiceError("žádná změna oproti aktuálnímu stavu")
        tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s AND module = 'ports'
                      AND status = 'draft' AND changes->>'port' = %(p)s""", {"d": str(device_id), "p": port})
        row = tx.fetchone(
            """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                        confirm_minutes, basis_sha, created_by)
               VALUES (%(t)s, %(d)s, 'ports', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "d": str(device_id), "sum": p["summary"],
             "chg": {"port": port, "changes": p["changes"], "warnings": p["warnings"]},
             "sc": p["apply"], "rv": p["revert"], "cm": CONFIRM_MINUTES, "b": portcfg.config_basis(inv),
             "u": _s(user_id)})
        audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"plan_id": row["id"], "module": "ports", "summary": p["summary"]}, ip=ip)
    return {"plan_id": row["id"], "summary": p["summary"], "warnings": p["warnings"], "changes": p["changes"]}


def plan_script(tx: Tx, plan_id: int, device_id: Any) -> dict:
    r = tx.fetchone("""SELECT script, revert_script, confirm_minutes FROM config_plan
                       WHERE id = %(i)s AND device_id = %(d)s AND status = 'queued'""",
                    {"i": plan_id, "d": str(device_id)})
    if not r:
        raise ServiceError("plán k aplikaci neexistuje")
    return r


def finish_plan(tx: Tx, command_id: int, ok: bool) -> None:
    """Výsledek aplikace. Plán s návratovým skriptem čeká na potvrzení (confirming)."""
    tx.execute(
        """UPDATE config_plan SET finished_at = now(),
               status = CASE WHEN NOT %(ok)s THEN 'failed'::plan_status
                             WHEN revert_script IS NOT NULL THEN 'confirming'::plan_status
                             ELSE 'applied'::plan_status END,
               confirm_deadline = CASE WHEN %(ok)s AND revert_script IS NOT NULL
                                       THEN now() + make_interval(mins => confirm_minutes) END
           WHERE command_id = %(c)s AND status = 'queued'""",
        {"ok": ok, "c": command_id})


def pending_confirm(tx: Tx, device_id: Any) -> dict | None:
    """Plán čekající na potvrzení, pro který ještě není ve frontě potvrzení."""
    return tx.fetchone(
        """SELECT p.id, p.tenant_id FROM config_plan p WHERE p.device_id = %(d)s AND p.status = 'confirming'
             AND NOT EXISTS (SELECT 1 FROM device_command c WHERE c.plan_id = p.id AND c.kind = 'confirm_plan'
                             AND c.status IN ('queued', 'sent'))
           ORDER BY p.id LIMIT 1""", {"d": str(device_id)})


def confirm_done(tx: Tx, plan_id: int, ok: bool) -> None:
    if ok:
        tx.execute("""UPDATE config_plan SET status = 'applied', confirmed_at = now()
                      WHERE id = %(i)s AND status = 'confirming'""", {"i": plan_id})


def rolled_back(tx: Tx, command_id: int) -> dict | None:
    return tx.fetchone(
        """UPDATE config_plan SET status = 'rolled_back', finished_at = now()
           WHERE command_id = %(c)s AND status IN ('confirming', 'queued', 'applied', 'failed')
           RETURNING id, device_id, summary""",
        {"c": command_id})
