"""Postupná aktualizace RouterOS v lokalitě (rolling upgrade).

Vždy jen jedno zařízení: další se spustí, až se předchozí vrátí online
s cílovou verzí. Pořadí „od konce“: podle topologie (sousedé MNDP/LLDP) nejdřív
zařízení nejdál od brány, gateway poslední – restart nikdy neodřízne cestu
k ještě neaktualizovaným. Bez topologie podle role (AP, PtP, switche, gateway).
Ručně: technik vybere zařízení a pořadí sám.
Při chybě nebo když se zařízení do STEP_TIMEOUT nevrátí, se úloha zastaví
a zbytek lokality zůstane beze změny.

Kanárek: první zařízení se po aktualizaci nechá `canary` minut běžet (stav „soak“).
Když během té doby vypadne, hlásí jinou verzi nebo se u něj otevře kritický alert,
úloha se zastaví dřív, než se nová verze dostane na zbytek lokality.

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
CANARY_DEFAULT = 10   # minut sledování prvního zařízení (API); 0 = bez kanárka
CANARY_MAX = 120
CHECKIN_STALE = dt.timedelta(minutes=5)


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
        r["kind"] = "upgrade"
        out.append(r)
    # zařízení s aktuálním RouterOS, ale starším RouterBOOT (firmware se po dřívější aktualizaci nedotáhl)
    have = {str(r["id"]) for r in out}
    for r in tx.fetchall(
            """SELECT d.id, d.identity, d.serial, d.role::text AS role, d.ros_version,
                      i.data->>'current-firmware' AS fw_from, i.data->>'upgrade-firmware' AS fw_to
               FROM device d JOIN device_inventory i ON i.device_id = d.id AND i.section = 'routerboard'
               WHERE d.site_id = %(s)s AND d.state = 'adopted'""", {"s": str(site_id)}):
        if r["fw_from"] and r["fw_to"] and r["fw_from"] != r["fw_to"] and str(r["id"]) not in have:
            r.update(kind="firmware", latest=(r["ros_version"] or "").split(" ")[0])
            out.append(r)
    depth = depths(tx, site_id)
    for r in out:
        r["depth"] = depth.get(str(r["id"]))
    # od konce sítě: nejvzdálenější od brány první, gateway vždy poslední; bez topologie podle role
    out.sort(key=lambda r: (r["role"] == "gateway", -(r["depth"] if r["depth"] is not None else -1),
                            ROLE_ORDER.get(r["role"], 2), r["identity"] or r["serial"]))
    return out


def depths(tx, site_id: Any) -> dict[str, int]:
    """Vzdálenost zařízení od brány lokality (počet skoků v grafu sousedů MNDP/LLDP).
    Kořen = zařízení s rolí gateway; bez gateway prázdné (řadí se pak podle role)."""
    from collections import deque

    from . import dhcpguard, inventory

    devs = tx.fetchall("""SELECT id, identity, role::text AS role FROM device WHERE site_id = %(s)s
                          AND state IN ('adopted', 'offline')""", {"s": str(site_id)})
    invs = {str(d["id"]): inventory.load(tx, d["id"], ["neighbors", "interfaces"]) for d in devs}
    adj = dhcpguard.site_graph(devs, invs)
    roots = [str(d["id"]) for d in devs if d["role"] == "gateway"]
    dist: dict[str, int] = {r: 0 for r in roots}
    q = deque(roots)
    while q:
        a = q.popleft()
        for b in adj.get(a, {}):
            if b not in dist:
                dist[b] = dist[a] + 1
                q.append(b)
    return dist


def start(db: Database, tenant_id: Any, site_id: Any, user_id: Any = None, ip: str | None = None,
          order: list[str] | None = None, canary: int = 0) -> dict:
    """order = ruční výběr a pořadí (id zařízení); None = automaticky od konce sítě.
    canary = kolik minut sledovat první zařízení, než se pokračuje (0 = hned dál)."""
    try:
        canary = max(0, min(CANARY_MAX, int(canary or 0)))
    except (TypeError, ValueError):
        raise ServiceError("neplatná doba sledování prvního zařízení") from None
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        if tx.fetchone("SELECT 1 FROM upgrade_job WHERE site_id = %(s)s AND status = 'running'",
                       {"s": str(site_id)}):
            raise ServiceError("v lokalitě už postupná aktualizace běží")
        devs = candidates(tx, site_id)
        if order is not None:
            by_id = {str(d["id"]): d for d in devs}
            if not isinstance(order, list) or len(order) != len(set(map(str, order))):
                raise ServiceError("neplatné pořadí zařízení")
            unknown = [str(o) for o in order if str(o) not in by_id]
            if unknown:
                raise ServiceError("některé zařízení v pořadí nemá dostupnou aktualizaci nebo není online – obnov stránku")
            devs = [by_id[str(o)] for o in order]
        if not devs:
            raise ServiceError("v lokalitě není žádné online zařízení s dostupnou aktualizací")
        steps = [{"device_id": str(d["id"]), "label": d["identity"] or d["serial"], "role": d["role"],
                  "from": (d["ros_version"] or "").split(" ")[0], "version": d["latest"], "kind": d.get("kind", "upgrade"),
                  **({"fw": f"{d['fw_from']} → {d['fw_to']}"} if d.get("kind") == "firmware" else {}),
                  "status": "pending", "command_id": None, "started_at": None, "message": None} for d in devs]
        if canary and len(steps) > 1:
            steps[0]["canary"] = canary
        job = tx.fetchone(
            """INSERT INTO upgrade_job (tenant_id, site_id, steps, created_by)
               VALUES (%(t)s, %(s)s, %(st)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "s": str(site_id), "st": steps, "u": str(user_id) if user_id else None})
        audit(tx, "site.upgrade_start", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"job_id": job["id"], "devices": [(s["label"], s["version"]) for s in steps],
                     "order": "manual" if order is not None else "auto",
                     "canary_minutes": steps[0].get("canary", 0)}, ip=ip)
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


FW_MESSAGE = "RouterOS hotovo, aktualizuje se RouterBOOT (druhý restart)"
FW_SETTLE = dt.timedelta(seconds=75)


def _firmware_state(tx, step: dict, dev: dict | None, started: dt.datetime) -> str:
    """Stav aktualizace RouterBOOT u kroku: "wait" (příkaz čeká / zařízení se po něm ještě neohlásilo) nebo "ok".
    Příkaz `firmware` potvrzuje před restartem, proto se čeká na check-in aspoň FW_SETTLE po potvrzení."""
    if step.get("kind") == "firmware":
        fw = tx.fetchone("SELECT status::text AS status, finished_at FROM device_command WHERE id = %(c)s",
                         {"c": step["command_id"]})
    else:
        fw = tx.fetchone("""SELECT status::text AS status, finished_at FROM device_command
                            WHERE device_id = %(d)s AND kind = 'firmware' AND created_at >= %(t)s
                            ORDER BY id DESC LIMIT 1""", {"d": step["device_id"], "t": started})
    if not fw or fw["status"] == "failed":
        return "ok"  # bez RouterBOARD (CHR) nebo příkaz selhal – RouterOS je aktuální, pokračuje se
    if fw["status"] != "done" or not fw["finished_at"]:
        return "wait"
    seen = dev["last_checkin_at"] if dev else None
    return "ok" if seen and seen >= fw["finished_at"] + FW_SETTLE else "wait"


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
    cur = next((s for s in steps if s["status"] in ("running", "soak")), None)
    with tenant_scope(db, tid) as tx:
        if cur and cur["status"] == "soak":
            # kanárek: po aktualizaci musí zůstat v pořádku, než se pokračuje
            dev = tx.fetchone("SELECT state, ros_version, last_checkin_at FROM device WHERE id = %(d)s",
                              {"d": cur["device_id"]})
            bad = None
            if not dev or dev["state"] != "adopted":
                bad = f"po aktualizaci vypadlo ({dev['state'] if dev else 'odebráno'})"
            elif ros_versions.parse(dev["ros_version"] or "") != ros_versions.parse(cur["version"]):
                bad = f"po aktualizaci hlásí verzi {dev['ros_version']}"
            elif dev["last_checkin_at"] and now - dev["last_checkin_at"] > CHECKIN_STALE and \
                    now - (_ts(cur.get("soak_from")) or now) > CHECKIN_STALE:
                bad = "po aktualizaci se přestalo hlásit"
            else:
                al = tx.fetchone("""SELECT message FROM alert WHERE device_id = %(d)s AND resolved_at IS NULL
                                    AND severity = 'critical' AND opened_at >= %(t)s ORDER BY id LIMIT 1""",
                                 {"d": cur["device_id"], "t": _ts(cur["started_at"]) or now})
                if al:
                    bad = f"po aktualizaci kritický problém: {al['message']}"
            if bad:
                cur.update(status="failed", message=bad)
                _finish(tx, job, steps, "stopped", f"{cur['label']} (první zařízení): {bad} – zbytek lokality zůstal beze změny")
                return True
            if now < (_ts(cur.get("soak_until")) or now):
                return False
            cur.update(status="done", message=None)
        elif cur:
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
            fw = _firmware_state(tx, cur, dev, started) if cmd and cmd["status"] == "done" and on_target else None
            if fw == "wait" and now - started <= STEP_TIMEOUT:
                if cur.get("message") != FW_MESSAGE:
                    cur["message"] = FW_MESSAGE
                    tx.execute("UPDATE upgrade_job SET steps = %(st)s WHERE id = %(j)s", {"st": steps, "j": job["id"]})
                return False
            if cmd and cmd["status"] == "done" and dev and dev["state"] == "adopted" and on_target:
                if cur.get("canary"):
                    until = now + dt.timedelta(minutes=int(cur["canary"]))
                    cur.update(status="soak", soak_from=now.isoformat(), soak_until=until.isoformat(),
                               message=f"sleduje se {cur['canary']} min, pak se pokračuje")
                    tx.execute("UPDATE upgrade_job SET steps = %(st)s WHERE id = %(j)s", {"st": steps, "j": job["id"]})
                    return True
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
        fw_only = nxt.get("kind") == "firmware"
        if not fw_only and dev and ros_versions.parse(dev["ros_version"] or "") >= ros_versions.parse(nxt["version"]):
            nxt.update(status="skipped", message="už má cílovou verzi")
            tx.execute("UPDATE upgrade_job SET steps = %(st)s WHERE id = %(j)s", {"st": steps, "j": job["id"]})
            return True
        if not dev or dev["state"] != "adopted":
            nxt.update(status="failed", message=f"zařízení není online ({dev['state'] if dev else 'odebráno'})")
            _finish(tx, job, steps, "stopped", f"{nxt['label']}: {nxt['message']}")
            return True
    # příkaz mimo transakci výše – request_command si otevře vlastní (se stejnými kontrolami jako v UI)
    try:
        if fw_only:
            cid = services.request_command(db, tid, nxt["device_id"], "firmware", job["created_by"], None)
        else:
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
