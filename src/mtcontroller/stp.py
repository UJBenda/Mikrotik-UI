"""STP/RSTP/MSTP lokality (jen čtení): root bridge, role portů, rizika smyček.

Zdroj: inventura – /interface/bridge (protocol-mode), /interface/bridge/monitor
once (root-bridge, root-bridge-id, root-port), /interface/bridge/port (role,
forwarding) a sousedé (MNDP/LLDP) pro rozpoznání linek mezi spravovanými zařízeními.
"""

from __future__ import annotations

import re
from typing import Any

from .clients import uplink_ports

_BLOCKING_ROLES = {"alternate-port", "backup-port"}
_MAC = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def root_mac(root_id: Any) -> str | None:
    """"0x8000.48:A9:8A:12:34:56" / "8000.48:A9:..." → "48:A9:8A:12:34:56"."""
    m = _MAC.search(str(root_id or ""))
    return m.group(1).upper() if m else None


def site_stp(devices: list[dict], invs: dict[str, dict]) -> dict:
    infra_macs: set[str] = set()
    owner: dict[str, str] = {}
    names = {str(d["id"]): d.get("identity") or d.get("serial") for d in devices}
    for d in devices:
        did = str(d["id"])
        inv = invs.get(did, {})
        for i in _rows(inv, "interfaces"):
            if i.get("mac-address"):
                mac = str(i["mac-address"]).upper()
                infra_macs.add(mac)
                owner[mac] = did
        for b in _rows(inv, "stp_bridges"):
            if b.get("current-mac-address"):
                mac = str(b["current-mac-address"]).upper()
                infra_macs.add(mac)
                owner[mac] = did

    bridges: list[dict] = []
    blocked: list[dict] = []
    warnings: list[dict] = []
    for d in devices:
        did = str(d["id"])
        inv = invs.get(did, {})
        label = names[did]
        uplinks = uplink_ports(inv, infra_macs)
        mon = {str(b.get("name")): b for b in _rows(inv, "stp_bridges")}
        ports_by_bridge: dict[str, list[str]] = {}
        for bp in _rows(inv, "bridge_ports"):
            if bp.get("bridge") and bp.get("interface") and not _true(bp.get("disabled")):
                ports_by_bridge.setdefault(str(bp["bridge"]), []).append(str(bp["interface"]))
        for br in _rows(inv, "bridges"):
            if _true(br.get("disabled")) or not br.get("name"):
                continue
            bname = str(br["name"])
            mode = str(br.get("protocol-mode") or "none")
            m = mon.get(bname, {})
            links = sorted(p for p in ports_by_bridge.get(bname, []) if p in uplinks)
            rid = m.get("root-bridge-id")
            rmac = root_mac(rid)
            bridges.append({
                "device_id": did, "device": label, "bridge": bname, "protocol": mode,
                "root": _true(m.get("root-bridge")), "root_id": rid,
                "root_device": names.get(owner.get(rmac or "", "")), "root_port": m.get("root-port") or None,
                "root_path_cost": m.get("root-path-cost"), "managed_links": links,
            })
            if mode == "none" and len(links) >= 2:
                warnings.append({"severity": "warning",
                                 "text": f"{label} {bname}: STP vypnuté, ale {len(links)} linky ke spravovaným "
                                         f"zařízením ({', '.join(links)}) – riziko smyčky"})
        for p in _rows(inv, "stp_ports"):
            if _true(p.get("disabled")) or _true(p.get("inactive")):
                continue
            role = str(p.get("role") or "")
            if role in _BLOCKING_ROLES:
                blocked.append({"device_id": did, "device": label, "port": p.get("interface"),
                                "bridge": p.get("bridge"), "role": role})

    roots = {}
    for b in bridges:
        if b["protocol"] != "none" and b["root_id"]:
            roots.setdefault(str(b["root_id"]), b)
    for rid, b in roots.items():
        if not b["root_device"]:
            warnings.append({"severity": "warning",
                             "text": f"STP root bridge {rid} není spravované zařízení (vidí ho {b['device']} "
                                     f"{b['bridge']}) – nastav nižší priority na páteřním switchi"})
    if len(roots) > 1:
        warnings.append({"severity": "info",
                         "text": f"v lokalitě je {len(roots)} různých STP root bridge – oddělené L2 sítě, "
                                 "nebo STP neprochází mezi switchi (MSTP regiony, filtrace BPDU)"})
    for b in blocked:
        warnings.append({"severity": "info",
                         "text": f"{b['device']} {b['port']}: zablokováno STP ({b['role']}) – redundantní cesta"})
    return {"bridges": bridges, "blocked": blocked, "warnings": warnings,
            "roots": [{"root_id": rid, "device": b["root_device"]} for rid, b in roots.items()]}
