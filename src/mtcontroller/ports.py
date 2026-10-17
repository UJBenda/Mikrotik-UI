"""Tabulka portů zařízení (jako port tabulka UniFi): linka, PoE, VLAN.

Zdroj: inventura – /interface (čítače), /interface/ethernet/monitor once
(rychlost, duplex, SFP), /interface/ethernet/poe/monitor once, bridge porty
a bridge VLAN tabulka (PVID, tagované/netagované VLAN).
"""

from __future__ import annotations

from typing import Any


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _members(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [x for x in str(v or "").split(",") if x]


def port_info(inv: dict) -> dict[str, dict]:
    """{jméno portu: {link, rate, duplex, sfp, poe, poe_power, bridge, pvid, tagged, untagged}}"""
    out: dict[str, dict] = {}

    def get(name: str) -> dict:
        return out.setdefault(name, {"link": None, "rate": None, "full_duplex": None, "sfp_rx_power": None,
                                     "poe": None, "poe_power": None, "bridge": None, "pvid": None,
                                     "tagged": [], "untagged": [], "stp_role": None, "stp_forwarding": None,
                                     "edge": None})

    for m in _rows(inv, "ethernet_monitor"):
        if not m.get("name"):
            continue
        p = get(str(m["name"]))
        p["link"] = m.get("status")
        p["rate"] = m.get("rate")
        p["full_duplex"] = _true(m["full-duplex"]) if m.get("full-duplex") is not None else None
        p["sfp_rx_power"] = m.get("sfp-rx-power")
    for m in _rows(inv, "poe_monitor"):
        if m.get("name"):
            p = get(str(m["name"]))
            p["poe"] = m.get("poe-out-status")
            p["poe_power"] = m.get("poe-out-power")
    for bp in _rows(inv, "bridge_ports"):
        if bp.get("interface") and not _true(bp.get("disabled")):
            p = get(str(bp["interface"]))
            p["bridge"] = bp.get("bridge")
            p["pvid"] = bp.get("pvid")
    for sp in _rows(inv, "stp_ports"):
        if sp.get("interface") and not _true(sp.get("disabled")):
            p = get(str(sp["interface"]))
            p["stp_role"] = sp.get("role")
            p["stp_forwarding"] = _true(sp["forwarding"]) if sp.get("forwarding") is not None else None
            p["edge"] = _true(sp["edge-port"]) if sp.get("edge-port") is not None else None
    for v in _rows(inv, "bridge_vlans"):
        if _true(v.get("disabled")):
            continue
        ids = str(v.get("vlan-ids") or "")
        tagged = _members(v.get("current-tagged") if v.get("current-tagged") is not None else v.get("tagged"))
        untagged = _members(v.get("current-untagged") if v.get("current-untagged") is not None else v.get("untagged"))
        # i samotný bridge (VLAN do CPU, např. správa)
        for name in tagged:
            get(name)["tagged"].append(ids)
        for name in untagged:
            get(name)["untagged"].append(ids)
    for p in out.values():
        p["tagged"] = sorted(set(p["tagged"]), key=_vkey)
        p["untagged"] = sorted(set(p["untagged"]), key=_vkey)
    return out


def _vkey(s: str) -> tuple:
    head = s.split(",")[0].split("-")[0]
    return (int(head) if head.isdigit() else 1 << 30, s)
