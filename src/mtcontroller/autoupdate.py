"""Automatické a naplánované aktualizace lokality.

Politika je v `site_policy` modul `autoupdate`:
  {enabled, days: [0–6, 0 = pondělí], from: "HH:MM", to: "HH:MM", delay_days, canary,
   scheduled: {at, order, by} | None, seen: {verze: kdy poprvé nabídnuta}, last: {at, result}}

- **Automaticky**: v servisním okně (dny + čas, místní čas serveru) worker spustí postupnou aktualizaci
  (`upgrades.start`) zařízení, jejichž nová verze je nabízená aspoň `delay_days` dní – čerstvé vydání
  se tak nenasazuje hned. Nejvýš jednou za 20 hodin.
- **Naplánovaně**: jednorázově v zadaný čas, volitelně jen vybraná zařízení v daném pořadí.

Obojí běží stejnou úlohou jako ruční spuštění: po jednom zařízení, od konce sítě, s kanárkem,
RouterOS i RouterBOOT; při chybě se zastaví. Během úlohy se neposílají upozornění (maintenance).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from . import upgrades
from .db import Database, Tx, operator_scope, tenant_scope
from .services import NotFound, ServiceError, audit

MODULE = "autoupdate"
DEFAULT = {"enabled": False, "days": [1, 2, 3], "from": "02:00", "to": "05:00", "delay_days": 7, "canary": 10}
MIN_GAP = dt.timedelta(hours=20)
SCHEDULE_GRACE = dt.timedelta(hours=6)   # o kolik se naplánovaný start smí opozdit (worker stál)
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _ts(v: Any) -> dt.datetime | None:
    try:
        t = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def validate(data: Any, now: dt.datetime | None = None) -> dict:
    """Uživatelská část politiky (bez `seen` a `last`)."""
    if not isinstance(data, dict):
        raise ServiceError("neplatné nastavení aktualizací")
    out = dict(DEFAULT)
    out["enabled"] = bool(data.get("enabled", False))
    days = data.get("days", DEFAULT["days"])
    if not isinstance(days, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        raise ServiceError("dny: seznam čísel 0 (pondělí) až 6 (neděle)")
    out["days"] = sorted(set(days))
    if out["enabled"] and not out["days"]:
        raise ServiceError("vyber aspoň jeden den")
    for k in ("from", "to"):
        v = str(data.get(k, DEFAULT[k]))
        if not _HM.match(v):
            raise ServiceError("čas ve tvaru HH:MM")
        out[k] = v
    if out["from"] == out["to"]:
        raise ServiceError("začátek a konec okna se musí lišit")
    try:
        out["delay_days"] = int(data.get("delay_days", DEFAULT["delay_days"]))
        out["canary"] = int(data.get("canary", DEFAULT["canary"]))
    except (TypeError, ValueError):
        raise ServiceError("neplatné číslo") from None
    if not 0 <= out["delay_days"] <= 60 or not 0 <= out["canary"] <= upgrades.CANARY_MAX:
        raise ServiceError("odklad 0–60 dní, sledování prvního zařízení 0–120 min")
    sch = data.get("scheduled")
    out["scheduled"] = None
    if sch:
        at = _ts(sch.get("at")) if isinstance(sch, dict) else None
        if not at:
            raise ServiceError("neplatný čas naplánované aktualizace")
        if at < (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(minutes=5):
            raise ServiceError("naplánovaný čas je v minulosti")
        order = sch.get("order")
        if order is not None and (not isinstance(order, list) or not order or len(order) > 500
                                  or len(set(map(str, order))) != len(order)):
            raise ServiceError("neplatný výběr zařízení")
        out["scheduled"] = {"at": at.isoformat(timespec="seconds"), "order": [str(o) for o in order] if order else None}
    return out


def get(tx: Tx, site_id: Any) -> dict:
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s", {"s": str(site_id), "m": MODULE})
    return {**DEFAULT, "scheduled": None, "last": None, **((r["data"] if r else None) or {})}


def _save(tx: Tx, tenant_id: Any, site_id: Any, data: dict, user_id: Any = None) -> None:
    tx.execute("""INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
                  VALUES (%(t)s, %(s)s, %(m)s, %(d)s, %(u)s)
                  ON CONFLICT (site_id, module) DO UPDATE SET data = EXCLUDED.data, updated_at = now(),
                      updated_by = coalesce(EXCLUDED.updated_by, site_policy.updated_by)""",
               {"t": str(tenant_id), "s": str(site_id), "m": MODULE, "d": data, "u": str(user_id) if user_id else None})


def set_policy(db: Database, tenant_id: Any, site_id: Any, data: Any, user_id: Any = None, ip: str | None = None) -> dict:
    new = validate(data)
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        old = get(tx, site_id)
        if new["scheduled"]:
            new["scheduled"]["by"] = str(user_id) if user_id else None
        merged = {**new, "seen": old.get("seen") or {}, "last": old.get("last")}
        _save(tx, tenant_id, site_id, merged, user_id)
        keys = ("enabled", "days", "from", "to", "delay_days", "canary", "scheduled")
        audit(tx, "site.autoupdate", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              before={k: old.get(k) for k in keys}, after={k: new.get(k) for k in keys}, ip=ip)
        return merged


def in_window(pol: dict, local: dt.datetime) -> bool:
    """Je místní čas v okně? Okno přes půlnoc (22:00–04:00) patří ke dni, kdy začalo."""
    a, b = pol["from"], pol["to"]
    hm = local.strftime("%H:%M")
    if a < b:
        return local.weekday() in pol["days"] and a <= hm < b
    if hm >= a:
        return local.weekday() in pol["days"]
    return hm < b and (local.weekday() - 1) % 7 in pol["days"]


def run(db: Database, now: dt.datetime | None = None, local: dt.datetime | None = None) -> int:
    """Krok workeru. Vrací počet spuštěných úloh."""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = local or now.astimezone()
    with operator_scope(db) as tx:
        rows = tx.fetchall("SELECT tenant_id, site_id, data FROM site_policy WHERE module = %(m)s", {"m": MODULE})
    started = 0
    for r in rows:
        pol = {**DEFAULT, "scheduled": None, "last": None, "seen": {}, **(r["data"] or {})}
        if not pol["enabled"] and not pol["scheduled"]:
            continue
        tid, sid = r["tenant_id"], r["site_id"]
        with tenant_scope(db, tid) as tx:
            cands = upgrades.candidates(tx, sid)
            busy = bool(tx.fetchone("SELECT 1 FROM upgrade_job WHERE site_id = %(s)s AND status = 'running'", {"s": str(sid)}))
        changed = False
        seen = dict(pol.get("seen") or {})
        for c in cands:
            if c["kind"] == "upgrade" and c["latest"] not in seen:
                seen[c["latest"]] = now.isoformat(timespec="seconds")
                changed = True
        # verze, které už žádné zařízení nenabízí, zapomenout
        for v in [v for v in seen if v not in {c["latest"] for c in cands}]:
            del seen[v]
            changed = True
        pol["seen"] = seen
        order, why = None, None
        sch = pol["scheduled"]
        at = _ts(sch.get("at")) if sch else None
        if sch and at and at <= now:
            pol["scheduled"] = None
            changed = True
            if now - at > SCHEDULE_GRACE:
                pol["last"] = {"at": now.isoformat(timespec="seconds"), "result": "naplánovaná aktualizace propadla (controller neběžel)"}
            elif busy:
                pol["last"] = {"at": now.isoformat(timespec="seconds"), "result": "naplánovaná aktualizace nespuštěna – jiná už běží"}
            else:
                ids = {str(c["id"]) for c in cands}
                order = [o for o in sch["order"] if o in ids] if sch.get("order") else [str(c["id"]) for c in cands]
                why = "naplánovaná"
        elif pol["enabled"] and not busy and in_window(pol, local):
            last = _ts((pol.get("last") or {}).get("started"))
            if not last or now - last >= MIN_GAP:
                ripe = [c for c in cands if c["kind"] == "firmware" or
                        now - (_ts(seen.get(c["latest"])) or now) >= dt.timedelta(days=pol["delay_days"])]
                if ripe:
                    order, why = [str(c["id"]) for c in ripe], "automatická"
        if order is not None:
            try:
                if not order:
                    raise ServiceError("žádné z vybraných zařízení nemá dostupnou aktualizaci")
                res = upgrades.start(db, tid, sid, (sch or {}).get("by") if why == "naplánovaná" else None, None, order,
                                     pol["canary"])
                pol["last"] = {"at": now.isoformat(timespec="seconds"), "started": now.isoformat(timespec="seconds"),
                               "result": f"{why} aktualizace spuštěna ({len(res['steps'])} zařízení)", "job_id": res["job_id"]}
                started += 1
            except ServiceError as e:
                pol["last"] = {"at": now.isoformat(timespec="seconds"), "result": f"{why} aktualizace nespuštěna: {e}",
                               **({"started": now.isoformat(timespec="seconds")} if why == "automatická" else {})}
            changed = True
        if changed:
            with tenant_scope(db, tid) as tx:
                # uživatel mohl mezitím politiku změnit – přepsat jen stavové položky
                cur = get(tx, sid)
                cur["seen"], cur["last"] = pol["seen"], pol["last"]
                if pol["scheduled"] is None and sch and (cur.get("scheduled") or {}).get("at") == sch.get("at"):
                    cur["scheduled"] = None
                _save(tx, tid, sid, cur)
                if order is not None:
                    audit(tx, "site.autoupdate_run", tenant_id=tid, target=f"site:{sid}", after=pol["last"])
    return started
