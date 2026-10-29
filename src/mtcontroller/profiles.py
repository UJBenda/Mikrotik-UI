"""Profily portů (UniFi Port Profiles): pojmenovaná sada PVID, tagovaných VLAN,
PoE, STP edge a PoE hlídače (restart napájení, když zařízení na portu přestane odpovídat). Profil se na port použije přes běžný plán změny portu
(commit-confirm), takže tu není žádná vlastní cesta na zařízení."""

from __future__ import annotations

from typing import Any

from .db import Database, DatabaseError, tenant_scope
from .portcfg import POE_MODES
from .services import NotFound, ServiceError, audit

EDGE = ("auto", "yes", "no")
FIELDS = ("pvid", "tagged", "poe", "edge", "poe_watchdog")
COLS = "id, name, pvid, tagged, poe, edge, poe_watchdog, poe_watchdog_timeout, builtin"
ALL = "*"  # tagged = všechny VLAN sítí lokality zařízení (rozvine se při použití)
TRUNK_NAME = "Trunk – všechny VLAN"


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
    out: dict[str, Any] = {"name": name, "pvid": None, "tagged": None, "poe": None, "edge": None,
                           "poe_watchdog": None, "poe_watchdog_timeout": None}
    if data.get("pvid") not in (None, ""):
        out["pvid"] = _vid(data["pvid"])
    t = data.get("tagged")
    items = [] if t is None else [str(x).strip() for x in (t if isinstance(t, list) else str(t).replace(";", ",").split(","))]
    if t is not None and [x.lower() for x in items if x] in ([ALL], ["all"], ["vše"]):
        out["tagged"] = ALL
    elif t is not None:
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
    if data.get("poe_watchdog") not in (None, ""):
        out["poe_watchdog"] = bool(data["poe_watchdog"]) if not isinstance(data["poe_watchdog"], str) \
            else data["poe_watchdog"] in ("1", "true", "yes", "on")
        if out["poe_watchdog"]:
            try:
                t = int(data.get("poe_watchdog_timeout") or 60)
            except (TypeError, ValueError):
                raise ServiceError("PoE hlídač: neplatný čas") from None
            if not 10 <= t <= 3600:
                raise ServiceError("PoE hlídač: čas bez odpovědi 10 s – 1 h")
            out["poe_watchdog_timeout"] = t
            if out["poe"] == "off":
                raise ServiceError("PoE hlídač nejde s vypnutým PoE")
    if all(out[k] is None for k in FIELDS):
        raise ServiceError("profil musí něco nastavovat")
    return out


def _row(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "pvid": r["pvid"],
            "tagged": r["tagged"].split(",") if r["tagged"] else ([] if r["tagged"] == "" else None),
            "poe": r["poe"], "edge": r["edge"], "poe_watchdog": r.get("poe_watchdog"),
            "poe_watchdog_timeout": r.get("poe_watchdog_timeout"), "builtin": bool(r.get("builtin")),
            "all_vlans": r["tagged"] == ALL}


def _ensure_builtin(tx) -> None:
    """Vestavěný trunk (VLAN 1 netagovaná, všechny sítě lokality tagovaně) – jednou na tenanta."""
    if tx.fetchone("SELECT 1 FROM port_profile WHERE builtin"):
        return
    t = tx.fetchone("SELECT mtc_current_tenant() AS t")
    if not t or not t["t"]:
        return
    tx.execute("""INSERT INTO port_profile (tenant_id, name, pvid, tagged, builtin) VALUES (%(t)s, %(n)s, 1, %(a)s, true)
                  ON CONFLICT (tenant_id, name) DO UPDATE SET builtin = true""",
               {"t": str(t["t"]), "n": TRUNK_NAME, "a": ALL})


def list_profiles(tx) -> list[dict]:
    _ensure_builtin(tx)
    return [_row(r) for r in tx.fetchall(f"SELECT {COLS} FROM port_profile ORDER BY builtin DESC, name")]


def site_vlans(tx, device_id: Any) -> list[int]:
    """VLAN sítí lokality zařízení (záměr lokality); bez záměru prázdné."""
    r = tx.fetchone("""SELECT p.data FROM device d JOIN site_policy p ON p.site_id = d.site_id AND p.module = 'intent'
                       WHERE d.id = %(d)s""", {"d": str(device_id)})
    nets = ((r or {}).get("data") or {}).get("networks") or []
    return sorted({int(n["vlan_id"]) for n in nets if str(n.get("vlan_id", "")).isdigit()})


def expand(profile: dict, vids: list[int] | None) -> dict:
    """Profil s „všechny VLAN“ → konkrétní tagované VLAN (bez PVID, ten jde netagovaně)."""
    if profile.get("tagged") != [ALL]:
        return profile
    pvid = profile.get("pvid") or 1
    return {**profile, "tagged": [str(v) for v in vids or [] if v != pvid]}


def get(tx, pid: int) -> dict:
    r = tx.fetchone(f"SELECT {COLS} FROM port_profile WHERE id = %(i)s", {"i": int(pid)})
    if not r:
        raise NotFound("profil neexistuje")
    return _row(r)


def create(db: Database, tid: str, data: dict, user_id: Any, ip: str | None) -> dict:
    p = clean(data)
    with tenant_scope(db, tid) as tx:
        try:
            r = tx.fetchone(f"""INSERT INTO port_profile (tenant_id, name, pvid, tagged, poe, edge, poe_watchdog,
                                                          poe_watchdog_timeout, created_by)
                               VALUES (%(t)s, %(n)s, %(p)s, %(tg)s, %(poe)s, %(e)s, %(w)s, %(wt)s, %(u)s)
                               RETURNING {COLS}""",
                            {"t": tid, "n": p["name"], "p": p["pvid"], "tg": p["tagged"], "poe": p["poe"],
                             "e": p["edge"], "w": p["poe_watchdog"], "wt": p["poe_watchdog_timeout"],
                             "u": str(user_id) if user_id else None})
        except DatabaseError as e:
            if e.is_unique_violation:
                raise ServiceError(f"profil {p['name']!r} už existuje") from e
            raise
        audit(tx, "port_profile.create", tenant_id=tid, user_id=user_id, target=f"port_profile:{r['id']}", after=p, ip=ip)
        return _row(r)


def update(db: Database, tid: str, pid: int, data: dict, user_id: Any, ip: str | None) -> dict:
    p = clean(data)
    with tenant_scope(db, tid) as tx:
        before = get(tx, pid)
        try:
            r = tx.fetchone(f"""UPDATE port_profile SET name = %(n)s, pvid = %(p)s, tagged = %(tg)s, poe = %(poe)s,
                                   edge = %(e)s, poe_watchdog = %(w)s, poe_watchdog_timeout = %(wt)s
                                WHERE id = %(i)s RETURNING {COLS}""",
                            {"n": p["name"], "p": p["pvid"], "tg": p["tagged"], "poe": p["poe"], "e": p["edge"],
                             "w": p["poe_watchdog"], "wt": p["poe_watchdog_timeout"], "i": int(pid)})
        except DatabaseError as e:
            if e.is_unique_violation:
                raise ServiceError(f"profil {p['name']!r} už existuje") from e
            raise
        audit(tx, "port_profile.update", tenant_id=tid, user_id=user_id, target=f"port_profile:{pid}",
              before=before, after=p, ip=ip)
        return _row(r)


def delete(db: Database, tid: str, pid: int, user_id: Any, ip: str | None) -> None:
    with tenant_scope(db, tid) as tx:
        if get(tx, pid)["builtin"]:
            raise ServiceError("vestavěný trunk nejde smazat (můžeš ho upravit nebo přejmenovat)")
        r = tx.fetchone(f"DELETE FROM port_profile WHERE id = %(i)s RETURNING {COLS}", {"i": pid})
        if not r:
            raise NotFound("profil neexistuje")
        audit(tx, "port_profile.delete", tenant_id=tid, user_id=user_id, target=f"port_profile:{pid}", before=_row(r), ip=ip)


def desired(profile: dict, watchdog_target: str | None = None, site_vids: list[int] | None = None) -> tuple[dict, str | None]:
    """Profil → „desired“ pro portcfg.plan (jen vlastnosti, které profil nastavuje).
    PoE hlídač potřebuje cíl (IP/MAC zařízení na portu) – bez něj se vynechá a vrátí se varování."""
    all_vlans = profile.get("tagged") == [ALL]
    profile = expand(profile, site_vids)
    out = {k: profile[k] for k in FIELDS if profile.get(k) is not None and k != "poe_watchdog"}
    warn = None
    if all_vlans and not site_vids:
        warn = "lokalita nemá sítě – profil „všechny VLAN“ netaguje nic (založ sítě v Lokalita → Sítě a DHCP)"
    if profile.get("poe_watchdog") is True:
        if watchdog_target:
            out["poe_watchdog"] = {"enabled": True, "address": watchdog_target,
                                   "timeout": profile.get("poe_watchdog_timeout") or 60}
        else:
            warn = "PoE hlídač vynechán – na portu není známé zařízení (IP/MAC)"
    elif profile.get("poe_watchdog") is False:
        out["poe_watchdog"] = {"enabled": False}
    return out, warn


def matching(profiles: list[dict], state: dict, site_vids: list[int] | None = None) -> dict | None:
    """První profil, kterému port odpovídá (všechny nastavené vlastnosti sedí)."""
    for p in profiles:
        if p.get("tagged") == [ALL]:
            if not site_vids:
                continue
            p = expand(p, site_vids)
        if p["pvid"] is not None and p["pvid"] != state.get("pvid"):
            continue
        if p["tagged"] is not None and sorted(p["tagged"], key=int) != sorted(state.get("tagged") or [], key=int):
            continue
        if p["poe"] is not None and p["poe"] != state.get("poe") and not state.get("is_bond"):
            continue
        if p["edge"] is not None and p["edge"] != state.get("edge"):
            continue
        w = (state.get("poe_watchdog") or {}).get("enabled")
        if p.get("poe_watchdog") is not None and w is not None and bool(p["poe_watchdog"]) != w:
            continue
        return p
    return None
