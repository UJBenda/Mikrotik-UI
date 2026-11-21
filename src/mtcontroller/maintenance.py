"""Servisní okna lokality: během okna se neposílají notifikace nových alertů.

Alerty dál vznikají a jsou vidět v rozhraní – jen se neposílají ven (e-mail, webhook…).
Co po skončení okna ještě trvá, se odešle; co během okna vzniklo i zaniklo, se neodešle vůbec.
Za servisní okno se počítá i běžící postupná aktualizace lokality (restarty jsou očekávané).

Okna jsou v `site_policy` modul `maintenance`: {"windows": [{id, from, to, note, by}]} (časy UTC, ISO).
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from .db import Database, Tx, tenant_scope
from .services import NotFound, ServiceError, audit

MODULE = "maintenance"
MAX_LENGTH = dt.timedelta(days=7)
MAX_AHEAD = dt.timedelta(days=90)
MAX_WINDOWS = 20


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ts(v: Any) -> dt.datetime | None:
    try:
        t = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _windows(data: Any, now: dt.datetime) -> list[dict]:
    """Platná okna, která ještě neskončila, podle začátku."""
    out = []
    for w in (data or {}).get("windows") or []:
        a, b = _ts(w.get("from")), _ts(w.get("to"))
        if a and b and b > now:
            out.append({"id": str(w.get("id")), "from": a.isoformat(timespec="seconds"), "to": b.isoformat(timespec="seconds"),
                        "note": str(w.get("note") or ""), "by": w.get("by"), "active": a <= now})
    return sorted(out, key=lambda w: w["from"])


def status(tx: Tx, site_id: Any, now: dt.datetime | None = None) -> dict:
    """{active: okno|None, windows: [...], upgrade: běží postupná aktualizace}"""
    now = now or _now()
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s", {"s": str(site_id), "m": MODULE})
    ws = _windows(r["data"] if r else None, now)
    up = bool(tx.fetchone("SELECT 1 FROM upgrade_job WHERE site_id = %(s)s AND status = 'running'", {"s": str(site_id)}))
    return {"active": next((w for w in ws if w["active"]), None), "windows": ws, "upgrade": up}


def quiet_sites(tx: Tx, now: dt.datetime | None = None) -> set[str]:
    """Lokality, kde se teď notifikace nových alertů neposílají (operator scope)."""
    now = now or _now()
    out = {str(r["site_id"]) for r in tx.fetchall("SELECT site_id FROM upgrade_job WHERE status = 'running'")}
    for r in tx.fetchall("SELECT site_id, data FROM site_policy WHERE module = %(m)s", {"m": MODULE}):
        if any(w["active"] for w in _windows(r["data"], now)):
            out.add(str(r["site_id"]))
    return out


def _save(tx: Tx, tenant_id: Any, site_id: Any, windows: list[dict], user_id: Any) -> None:
    data = {"windows": [{k: w[k] for k in ("id", "from", "to", "note", "by")} for w in windows]}
    tx.execute("""INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
                  VALUES (%(t)s, %(s)s, %(m)s, %(d)s, %(u)s)
                  ON CONFLICT (site_id, module) DO UPDATE SET data = EXCLUDED.data, updated_by = EXCLUDED.updated_by,
                      updated_at = now()""",
               {"t": str(tenant_id), "s": str(site_id), "m": MODULE, "d": data, "u": str(user_id) if user_id else None})


def add(db: Database, tenant_id: Any, site_id: Any, params: dict, user_id: Any = None, ip: str | None = None,
        now: dt.datetime | None = None) -> dict:
    """params: {minutes} (od teď) nebo {from, to}; volitelně note."""
    now = now or _now()
    if params.get("minutes") not in (None, ""):
        try:
            minutes = int(params["minutes"])
        except (TypeError, ValueError):
            raise ServiceError("neplatná délka okna") from None
        if minutes < 5:
            raise ServiceError("okno musí trvat aspoň 5 minut")
        start, end = now, now + dt.timedelta(minutes=minutes)
    else:
        start, end = _ts(params.get("from")), _ts(params.get("to"))
        if not start or not end:
            raise ServiceError("zadej délku okna nebo začátek a konec")
    if end <= start or end <= now:
        raise ServiceError("konec okna musí být po začátku a v budoucnu")
    if end - start > MAX_LENGTH:
        raise ServiceError("servisní okno může trvat nejvýš 7 dní")
    if start - now > MAX_AHEAD:
        raise ServiceError("okno jde naplánovat nejvýš 90 dní dopředu")
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s FOR UPDATE",
                        {"s": str(site_id), "m": MODULE})
        ws = _windows(r["data"] if r else None, now)
        if len(ws) >= MAX_WINDOWS:
            raise ServiceError("příliš mnoho naplánovaných oken")
        w = {"id": secrets.token_hex(4), "from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds"),
             "note": str(params.get("note") or "").strip()[:120], "by": str(user_id) if user_id else None}
        _save(tx, tenant_id, site_id, ws + [w], user_id)
        audit(tx, "site.maintenance_add", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={k: w[k] for k in ("from", "to", "note")}, ip=ip)
        return status(tx, site_id, now)


def end(db: Database, tenant_id: Any, site_id: Any, window_id: str, user_id: Any = None, ip: str | None = None,
        now: dt.datetime | None = None) -> dict:
    """Ukončí běžící okno nebo zruší naplánované."""
    now = now or _now()
    with tenant_scope(db, tenant_id) as tx:
        r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s FOR UPDATE",
                        {"s": str(site_id), "m": MODULE})
        ws = _windows(r["data"] if r else None, now)
        w = next((x for x in ws if x["id"] == str(window_id)), None)
        if not w:
            raise NotFound("servisní okno neexistuje")
        _save(tx, tenant_id, site_id, [x for x in ws if x is not w], user_id)
        audit(tx, "site.maintenance_end", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              before={k: w[k] for k in ("from", "to", "note")}, ip=ip)
        return status(tx, site_id, now)
