"""Historie portů (UniFi Port Time Machine): co se na portu změnilo a kdy.

Při každé inventuře se stav fyzických portů porovná s předchozí inventurou
a změny se uloží do `port_event`. Vlastnost se porovnává jen tehdy, když
obě inventury obsahují sekci, ze které pochází – výpadek jedné sekce REST
tak nevyrobí falešné změny.
"""

from __future__ import annotations

import re
from typing import Any

PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp)")

# vlastnost → sekce inventury, ze které pochází
SOURCES = {
    "link": ("interfaces",), "disabled": ("interfaces",), "comment": ("interfaces",),
    "flaps": ("interfaces",),
    "rate": ("ethernet_monitor",), "duplex": ("ethernet_monitor",),
    "poe": ("ethernet",), "poe_status": ("poe_monitor",),
    "pvid": ("bridge_ports",), "tagged": ("bridge_vlans",), "stp_role": ("stp_ports",),
    "neighbor": ("neighbors",),
}
LABELS = {
    "link": "linka", "disabled": "vypnutý", "comment": "popis", "flaps": "výpadky linky",
    "rate": "rychlost", "duplex": "duplex", "poe": "PoE režim", "poe_status": "PoE stav",
    "pvid": "native VLAN", "tagged": "tagované VLAN", "stp_role": "STP role", "neighbor": "soused",
}
KEEP_SECTIONS = sorted({s for v in SOURCES.values() for s in v})


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _members(v: Any) -> list[str]:
    return [x for x in str(v or "").split(",") if x]


def snapshot(inv: dict) -> dict[str, dict[str, Any]]:
    """{port: {vlastnost: hodnota}} pro fyzické porty."""
    out: dict[str, dict[str, Any]] = {}
    for i in _rows(inv, "interfaces"):
        name = str(i.get("name") or "")
        if i.get("type") != "ether" or not PHYS_RE.match(name):
            continue
        out[name] = {"link": "up" if _true(i.get("running")) else "down",
                     "disabled": "ano" if _true(i.get("disabled")) else "ne",
                     "comment": str(i.get("comment") or ""),
                     "_link_downs": int(str(i.get("link-downs"))) if str(i.get("link-downs") or "").isdigit() else None}
    for m in _rows(inv, "ethernet_monitor"):
        p = out.get(str(m.get("name")))
        if p is not None:
            up = m.get("status") == "link-ok"
            p["rate"] = str(m.get("rate") or "") if up else ""
            p["duplex"] = ("full" if _true(m.get("full-duplex")) else "half") if up and m.get("full-duplex") is not None else ""
    for e in _rows(inv, "ethernet"):
        p = out.get(str(e.get("name")))
        if p is not None:
            p["poe"] = str(e.get("poe-out") or "")
    for m in _rows(inv, "poe_monitor"):
        p = out.get(str(m.get("name")))
        if p is not None:
            p["poe_status"] = str(m.get("poe-out-status") or "")
    if "bridge_ports" in inv:
        for p in out.values():
            p["pvid"] = ""
        for bp in _rows(inv, "bridge_ports"):
            p = out.get(str(bp.get("interface")))
            if p is not None:
                p["pvid"] = str(bp.get("pvid") or "1")
    if "bridge_vlans" in inv:
        tagged: dict[str, list[str]] = {n: [] for n in out}
        for v in _rows(inv, "bridge_vlans"):
            if _true(v.get("disabled")):
                continue
            for m in _members(v.get("tagged")):
                if m in tagged:
                    tagged[m].append(str(v.get("vlan-ids")))
        for n, p in out.items():
            p["tagged"] = ",".join(sorted(tagged[n], key=lambda x: (len(x), x)))
    if "stp_ports" in inv:
        for p in out.values():
            p["stp_role"] = ""
        for s in _rows(inv, "stp_ports"):
            p = out.get(str(s.get("interface")))
            if p is not None:
                p["stp_role"] = str(s.get("role") or "")
    if "neighbors" in inv:
        neigh: dict[str, set[str]] = {n: set() for n in out}
        for n in _rows(inv, "neighbors"):
            for part in re.split(r"[,/]", str(n.get("interface") or "")):
                if part in neigh:
                    neigh[part].add(str(n.get("identity") or n.get("mac-address") or "?"))
        for n, p in out.items():
            p["neighbor"] = ", ".join(sorted(neigh[n]))
    return out


def diff(old_inv: dict, new_inv: dict) -> list[dict]:
    """Změny mezi dvěma inventurami: [{port, field, old, new}]."""
    if not old_inv or not new_inv:
        return []
    a, b = snapshot(old_inv), snapshot(new_inv)
    out = []
    for port in sorted(set(a) & set(b)):
        pa, pb = a[port], b[port]
        for field, secs in SOURCES.items():
            if not all(s in old_inv and s in new_inv for s in secs):
                continue
            if field == "flaps":
                # výpadky mezi inventurami, kdy linka skončila ve stejném stavu (jinak je to změna „link“)
                da, db = pa.get("_link_downs"), pb.get("_link_downs")
                if da is not None and db is not None and db > da and pa.get("link") == pb.get("link"):
                    out.append({"port": port, "field": "flaps", "old": None, "new": str(db - da)})
                continue
            va, vb = pa.get(field), pb.get(field)
            if va is None or vb is None or va == vb:
                continue
            out.append({"port": port, "field": field, "old": str(va)[:300], "new": str(vb)[:300]})
    return out


def describe(e: dict) -> str:
    f, old, new = e["field"], e.get("old"), e.get("new")
    if f == "link":
        return "linka nahoře" if new == "up" else "linka dole"
    if f == "flaps":
        return f"linka {new}× spadla a obnovila se"
    if f == "disabled":
        return "port vypnut" if new == "ano" else "port zapnut"
    if f == "neighbor":
        if not new:
            return f"soused odpojen ({old})"
        return f"připojen soused {new}" + (f" (dříve {old})" if old else "")
    return f"{LABELS.get(f, f)}: {old or '–'} → {new or '–'}"


def record(tx, tenant_id: Any, device_id: Any, old_inv: dict, new_inv: dict) -> int:
    events = diff(old_inv, new_inv)
    for e in events:
        tx.execute("""INSERT INTO port_event (tenant_id, device_id, port, field, old, new)
                      VALUES (%(t)s, %(d)s, %(p)s, %(f)s, %(o)s, %(n)s)""",
                   {"t": str(tenant_id), "d": str(device_id), "p": e["port"][:100], "f": e["field"],
                    "o": e["old"], "n": e["new"]})
    return len(events)


def history(tx, device_id: Any, port: str | None = None, days: int = 30, limit: int = 300) -> list[dict]:
    rows = tx.fetchall(
        """SELECT at, port, field, old, new FROM port_event
           WHERE device_id = %(d)s AND (%(p)s::text IS NULL OR port = %(p)s)
             AND at >= now() - make_interval(days => %(days)s)
           ORDER BY at DESC, id DESC LIMIT %(l)s""",
        {"d": str(device_id), "p": port, "days": days, "l": limit})
    return [dict(r, text=describe(r)) for r in rows]
