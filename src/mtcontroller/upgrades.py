"""Postupná aktualizace RouterOS v lokalitě (rolling upgrade).

Vždy jen jedno zařízení: další se spustí, až se předchozí vrátí online
s cílovou verzí. Pořadí: okrajová zařízení první (AP, PtP, switche),
gateway poslední – restart nikdy neodřízne cestu k ještě neaktualizovaným.
Při chybě nebo když se zařízení do STEP_TIMEOUT nevrátí, se úloha zastaví
a zbytek lokality zůstane beze změny.

Každý krok jde přes services.request_command(kind="upgrade"), tedy se stejnými
kontrolami jako ruční aktualizace (verze nabízená zařízením, kritické problémy).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from . import ros_versions, services
from .db import Database, operator_scope, tenant_scope
from .services import NotFound, ServiceError, audit

log = logging.getLogger("mtc.upgrades")

ROLE_ORDER = {"ap": 0, "ptp": 1, "unknown": 2, "switch": 3, "gateway": 4}
STEP_TIMEOUT = dt.timedelta(minutes=30)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def candidates(tx, site_id: Any) -> list[dict]:
    """Zařízení lokality s dostupnou aktualizací, v pořadí aktualizace."""
    rows = tx.fetchall(
        """SELECT d.id, d.identity, d.serial, d.role::text AS role, d.ros_version,
                  i.data->>'latest-version' AS latest
           FROM device d JOIN device_inventory i ON i.device_id = d.id AND i.section = 'update'
           WHERE d.site_id = %(s)s AND d.state = 'adopted' AND i.data->>'latest-version' IS NOT NULL""",
        {"s": str(site_id)})
    out = []
    for r in rows:
        if ros_versions.parse(r["latest"]) <= ros_versions.parse(r["ros_version"] or ""):
            continue
        if any(i.severity == "critical" for i in ros_versions.issues_for(r["latest"])):
            continue
        out.append(r)
    out.sort(key=lambda r: (ROLE_ORDER.get(r["role"], 2), r["identity"] or r["serial"]))
    return out


def start(db: Database, tenant_id: Any, site_id: Any, user_id: Any = None, ip: str | None = None) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        if tx.fetchone("SELECT 1 FROM upgrade_job WHERE site_id = %(s)s AND status = 'running'",
                       {"s": str(site_id)}):
            raise ServiceError("v lokalitě už postupná aktualizace běží")
        devs = candidates(tx, site_id)
        if not devs:
            raise ServiceError("v lokalitě není žádné online zařízení s dostupnou aktualizací")
        steps = [{"device_id": str(d["id"]), "label": d["identity"] or d["serial"], "role": d["role"],
                  "from": (d["ros_version"] or "").split(" ")[0], "version": d["latest"],
                  "status": "pending", "command_id": None, "started_at": None, "message": None} for d in devs]
        job = tx.fetchone(
            """INSERT INTO upgrade_job (tenant_id, site_id, steps, created_by)
               VALUES (%(t)s, %(s)s, %(st)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "s": str(site_id), "st": steps, "u": str(user_id) if user_id else None})
        audit(tx, "site.upgrade_start", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"job_id": job["id"], "devices": [(s["label"], s["version"]) for s in steps]}, ip=ip)
        return {"job_id": job["id"], "steps": steps}


def cancel(db: Database, tenant_id: Any, job_id: int, user_id: Any = None, ip: str | None = None) -> None:
    """Zastaví další kroky; právě běžící aktualizace zařízení doběhne."""
    with tenant_scope(db, tenant_id) as tx:
        n = tx.execute("""UPDATE upgrade_job SET status = 'cancelled', finished_at = now(),
                                 message = 'zrušeno uživatelem'
                          WHERE id = %(j)s AND status = 'running'""", {"j": job_id})
        if not n:
            raise NotFound("běžící úloha neexistuje")
        audit(tx, "site.upgrade_cancel", tenant_id=tenant_id, user_id=user_id, target=f"upgrade_job:{job_id}", ip=ip)


def _finish(tx, job: dict, steps: list[dict], status: str, message: str | None) -> None:
    tx.execute("""UPDATE upgrade_job SET steps = %(st)s, status = %(s)s, finished_at = now(), message = %(m)s
                  WHERE id = %(j)s""", {"st": steps, "s": status, "m": message, "j": job["id"]})
    audit(tx, f"site.upgrade_{status}", tenant_id=job["tenant_id"], target=f"upgrade_job:{job['id']}",
          after={"message": message})


def _ts(v: Any) -> dt.datetime | None:
    if not v:
        return None
    return v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))


def advance(db: Database, now: dt.datetime | None = None) -> int:
    """Krok workeru: posune všechny běžící úlohy. Vrací počet změněných úloh."""
    now = now or _now()
    with operator_scope(db) as tx:
        jobs = tx.fetchall("SELECT id, tenant_id, site_id, steps, created_by FROM upgrade_job WHERE status = 'running'")
    changed = 0
    for job in jobs:
        try:
            if _advance_one(db, job, now):
                changed += 1
        except Exception:
            log.exception("postupná aktualizace %s", job["id"])
    return changed


def _advance_one(db: Database, job: dict, now: dt.datetime) -> bool:
    steps: list[dict] = list(job["steps"])
    tid = job["tenant_id"]
    cur = next((s for s in steps if s["status"] == "running"), None)
    with tenant_scope(db, tid) as tx:
        if cur:
            cmd = tx.fetchone("SELECT status::text AS status, finished_at FROM device_command WHERE id = %(c)s",
                              {"c": cur["command_id"]})
            dev = tx.fetchone("SELECT state, ros_version, last_checkin_at FROM device WHERE id = %(d)s",
                              {"d": cur["device_id"]})
            started = _ts(cur["started_at"]) or now
            on_target = dev and ros_versions.parse(dev["ros_version"] or "") == ros_versions.parse(cur["version"])
            if cmd and cmd["status"] == "failed":
                cur.update(status="failed", message="zařízení aktualizaci odmítlo (viz /log na zařízení)")
                _finish(tx, job, steps, "stopped", f"{cur['label']}: {cur['message']}")
                return True
            if cmd and cmd["status"] == "done" and dev and dev["state"] == "adopted" and on_target:
                cur.update(status="done", message=None)
            elif now - started > STEP_TIMEOUT:
                cur.update(status="failed", message=f"do {int(STEP_TIMEOUT.total_seconds() // 60)} min se "
                                                    "nevrátilo s novou verzí")
                _finish(tx, job, steps, "stopped", f"{cur['label']}: {cur['message']}")
                return True
            else:
                return False  # čekáme
        nxt = next((s for s in steps if s["status"] == "pending"), None)
        if nxt is None:
            _finish(tx, job, steps, "done", None)
            return True
        dev = tx.fetchone("SELECT state, ros_version FROM device WHERE id = %(d)s", {"d": nxt["device_id"]})
        if dev and ros_versions.parse(dev["ros_version"] or "") >= ros_versions.parse(nxt["version"]):
            nxt.update(status="skipped", message="už má cílovou verzi")
            tx.execute("UPDATE upgrade_job SET steps = %(st)s WHERE id = %(j)s", {"st": steps, "j": job["id"]})
            return True
        if not dev or dev["state"] != "adopted":
            nxt.update(status="failed", message=f"zařízení není online ({dev['state'] if dev else 'odebráno'})")
            _finish(tx, job, steps, "stopped", f"{nxt['label']}: {nxt['message']}")
            return True
    # příkaz mimo transakci výše – request_command si otevře vlastní (se stejnými kontrolami jako v UI)
    try:
        cid = services.request_command(db, tid, nxt["device_id"], "upgrade", job["created_by"], None,
                                       {"version": nxt["version"]})
    except ServiceError as e:
        nxt.update(status="failed", message=str(e))
        with tenant_scope(db, tid) as tx:
            _finish(tx, job, steps, "stopped", f"{nxt['label']}: {e}")
        return True
    nxt.update(status="running", command_id=cid, started_at=now.isoformat())
    with tenant_scope(db, tid) as tx:
        tx.execute("UPDATE upgrade_job SET steps = %(st)s WHERE id = %(j)s", {"st": steps, "j": job["id"]})
    return True


def jobs(tx, site_id: Any, limit: int = 5) -> list[dict]:
    return tx.fetchall(
        """SELECT j.id, j.status::text AS status, j.steps, j.created_at, j.finished_at, j.message, u.email AS created_by
           FROM upgrade_job j LEFT JOIN app_user u ON u.id = j.created_by
           WHERE j.site_id = %(s)s ORDER BY j.id DESC LIMIT %(l)s""", {"s": str(site_id), "l": limit})
