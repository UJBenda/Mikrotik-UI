"""Profily portů (UniFi Port Profiles): pojmenovaná sada PVID, tagovaných VLAN,
PoE a STP edge. Profil se na port použije přes běžný plán změny portu
(commit-confirm), takže tu není žádná vlastní cesta na zařízení."""

from __future__ import annotations

from typing import Any

from .db import Database, DatabaseError, tenant_scope
from .portcfg import POE_MODES
from .services import NotFound, ServiceError, audit

EDGE = ("auto", "yes", "no")
FIELDS = ("pvid", "tagged", "poe", "edge")


def _vid(v: Any) -> int:
    try:
        n = int(str(v).strip())
    except ValueError:
        raise ServiceError(f"neplatné VLAN ID {v!r}") from None
    if not 1 <= n <= 4094:
        raise ServiceError(f"VLAN ID {n} mimo rozsah 1–4094")
    return n


def clean(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not 1 <= len(name) <= 64 or any(ord(c) < 32 for c in name):
        raise ServiceError("neplatné jméno profilu (1–64 znaků)")
    out: dict[str, Any] = {"name": name, "pvid": None, "tagged": None, "poe": None, "edge": None}
    if data.get("pvid") not in (None, ""):
        out["pvid"] = _vid(data["pvid"])
    if data.get("tagged") is not None:
        t = data["tagged"]
        items = t if isinstance(t, list) else str(t).replace(";", ",").split(",")
        vids = sorted({_vid(x) for x in items if str(x).strip()})
        if len(vids) > 64:
            raise ServiceError("nejvýš 64 tagovaných VLAN")
        out["tagged"] = ",".join(map(str, vids))
    if data.get("poe") not in (None, ""):
        if data["poe"] not in POE_MODES:
            raise ServiceError("neplatný režim PoE")
        out["poe"] = data["poe"]
    if data.get("edge") not in (None, ""):
        if data["edge"] not in EDGE:
            raise ServiceError("neplatné STP edge")
        out["edge"] = data["edge"]
    if all(out[k] is None for k in FIELDS):
        raise ServiceError("profil musí něco nastavovat")
    return out


def _row(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "pvid": r["pvid"],
            "tagged": r["tagged"].split(",") if r["tagged"] else ([] if r["tagged"] == "" else None),
            "poe": r["poe"], "edge": r["edge"]}


def list_profiles(tx) -> list[dict]:
    return [_row(r) for r in tx.fetchall("SELECT id, name, pvid, tagged, poe, edge FROM port_profile ORDER BY name")]


def create(db: Database, tid: str, data: dict, user_id: Any, ip: str | None) -> dict:
    p = clean(data)
    with tenant_scope(db, tid) as tx:
        try:
            r = tx.fetchone("""INSERT INTO port_profile (tenant_id, name, pvid, tagged, poe, edge, created_by)
                               VALUES (%(t)s, %(n)s, %(p)s, %(tg)s, %(poe)s, %(e)s, %(u)s)
                               RETURNING id, name, pvid, tagged, poe, edge""",
                            {"t": tid, "n": p["name"], "p": p["pvid"], "tg": p["tagged"], "poe": p["poe"],
                             "e": p["edge"], "u": str(user_id) if user_id else None})
        except DatabaseError as e:
            if e.is_unique_violation:
                raise ServiceError(f"profil {p['name']!r} už existuje") from e
            raise
        audit(tx, "port_profile.create", tenant_id=tid, user_id=user_id, target=f"port_profile:{r['id']}", after=p, ip=ip)
        return _row(r)


def delete(db: Database, tid: str, pid: int, user_id: Any, ip: str | None) -> None:
    with tenant_scope(db, tid) as tx:
        r = tx.fetchone("DELETE FROM port_profile WHERE id = %(i)s RETURNING id, name, pvid, tagged, poe, edge", {"i": pid})
        if not r:
            raise NotFound("profil neexistuje")
        audit(tx, "port_profile.delete", tenant_id=tid, user_id=user_id, target=f"port_profile:{pid}", before=_row(r), ip=ip)


def desired(profile: dict) -> dict:
    """Profil → „desired“ pro portcfg.plan (jen vlastnosti, které profil nastavuje)."""
    return {k: profile[k] for k in FIELDS if profile.get(k) is not None}


def matching(profiles: list[dict], state: dict) -> dict | None:
    """První profil, kterému port odpovídá (všechny nastavené vlastnosti sedí)."""
    for p in profiles:
        if p["pvid"] is not None and p["pvid"] != state.get("pvid"):
            continue
        if p["tagged"] is not None and sorted(p["tagged"], key=int) != sorted(state.get("tagged") or [], key=int):
            continue
        if p["poe"] is not None and p["poe"] != state.get("poe"):
            continue
        if p["edge"] is not None and p["edge"] != state.get("edge"):
            continue
        return p
    return None
