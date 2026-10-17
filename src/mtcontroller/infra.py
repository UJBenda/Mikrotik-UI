"""Infrastruktura lokality (jako „Infrastructure Topology“ v UniFi Network 10):
strom WAN → gateway → switche → AP s porty na spojích, zátěží, PoE a STP.

Zdroj: inventury spravovaných zařízení – sousedé (MNDP/LLDP) pro spoje,
/ip/route 0.0.0.0/0 a /ip/dhcp-client pro WAN, /interface/bridge/monitor pro
STP root, /system/resource pro CPU a paměť, ethernet/PoE monitor pro porty.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from . import stp

PORT_RE = re.compile(r"^(ether|sfp|combo|qsfp|wlan|wifi)")
PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp)")


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _obj(inv: dict, key: str) -> dict:
    v = inv.get(key)
    return v if isinstance(v, dict) else {}


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _port_of(iface: Any) -> str:
    """'bridge/ether3', 'ether3,bridge' → 'ether3' (fyzický port má přednost)."""
    parts = [p for p in re.split(r"[,/]", str(iface or "")) if p]
    for p in parts:
        if PORT_RE.match(p):
            return p
    return parts[0] if parts else ""


def _port_num(name: str) -> str:
    m = re.search(r"(\d+)(?!.*\d)", name or "")
    return m.group(1) if m else (name or "")


def _pct(free: Any, total: Any) -> int | None:
    try:
        return round(100 - 100 * int(free) / int(total))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _ports(inv: dict) -> list[dict]:
    mon = {str(m.get("name")): m for m in _rows(inv, "ethernet_monitor")}
    out = []
    for i in _rows(inv, "interfaces"):
        name = str(i.get("name") or "")
        if i.get("type") != "ether" or not PHYS_RE.match(name):
            continue
        m = mon.get(name, {})
        if _true(i.get("disabled")):
            cls = "off"
        elif not _true(i.get("running")) or (m.get("status") and m.get("status") != "link-ok"):
            cls = "down"
        elif m.get("rate") and "G" not in str(m.get("rate")):
            cls = "fe"
        else:
            cls = "gbe"
        out.append({"name": name, "cls": cls})
    out.sort(key=lambda p: (not p["name"].startswith(("ether", "combo")), int(_port_num(p["name"]) or 0)))
    return out


def _wans(inv: dict) -> list[dict]:
    addrs: dict[str, str] = {}
    for a in _rows(inv, "addresses"):
        if not _true(a.get("disabled")) and a.get("interface"):
            addrs.setdefault(str(a["interface"]), str(a.get("address") or "").split("/")[0])
    dhcp = {str(d.get("interface")): d for d in _rows(inv, "dhcp_client") if not _true(d.get("disabled"))}
    out, seen = [], set()
    routes = [r for r in _rows(inv, "routes_default")
              if not _true(r.get("disabled")) and str(r.get("routing-table") or "main") == "main"]
    for r in sorted(routes, key=lambda r: int(str(r.get("distance") or "1")) if str(r.get("distance") or "1").isdigit() else 99):
        imm = str(r.get("immediate-gw") or "")
        iface = imm.split("%", 1)[1] if "%" in imm else ""
        gw = str(r.get("gateway") or "")
        if not iface and "%" in gw:
            iface = gw.split("%", 1)[1]
        key = iface or gw
        if key in seen:
            continue
        seen.add(key)
        d = dhcp.get(iface, {})
        out.append({"interface": iface or None, "port": _port_of(iface) if iface else None,
                    "gateway": gw.split("%")[0] or d.get("gateway"), "address": addrs.get(iface) or
                    str(d.get("address") or "").split("/")[0] or None,
                    "distance": r.get("distance"), "active": _true(r.get("active")),
                    "dhcp": d.get("status"), "comment": r.get("comment")})
    # DHCP klient bez výchozí routy v inventuře (např. add-default-route=no) – i tak WAN kandidát
    for iface, d in dhcp.items():
        if iface not in seen and _true(d.get("add-default-route", "yes")) and d.get("status") == "bound":
            out.append({"interface": iface, "port": _port_of(iface), "gateway": d.get("gateway"),
                        "address": str(d.get("address") or "").split("/")[0] or None, "distance": None,
                        "active": False, "dhcp": d.get("status"), "comment": None})
    return out


def site_infra(devices: list[dict], invs: dict[str, dict]) -> dict:
    ids = [str(d["id"]) for d in devices]
    dev = {str(d["id"]): d for d in devices}
    owner: dict[str, str] = {}
    by_ident: dict[str, str] = {}
    for did in ids:
        inv = invs.get(did, {})
        for i in _rows(inv, "interfaces"):
            if i.get("mac-address"):
                owner[str(i["mac-address"]).upper()] = did
        for b in _rows(inv, "stp_bridges"):
            if b.get("current-mac-address"):
                owner[str(b["current-mac-address"]).upper()] = did
        if dev[did].get("identity"):
            by_ident[str(dev[did]["identity"])] = did

    # spoje: (a, b) → port na a, port na b
    ports: dict[tuple[str, str], str] = {}
    externals: list[dict] = []
    for a in ids:
        for n in _rows(invs.get(a, {}), "neighbors"):
            b = owner.get(str(n.get("mac-address") or "").upper()) or by_ident.get(str(n.get("identity") or ""))
            port = _port_of(n.get("interface"))
            if b and b != a:
                ports.setdefault((a, b), port)
            elif not b:
                externals.append({"parent": a, "port": port, "label": n.get("identity") or n.get("mac-address"),
                                  "platform": n.get("board") or n.get("platform"), "address": n.get("address")})
    adj: dict[str, set[str]] = {d: set() for d in ids}
    for a, b in ports:
        adj[a].add(b)
        adj[b].add(a)

    infos: dict[str, dict] = {}
    for did in ids:
        inv = invs.get(did, {})
        res = _obj(inv, "resource")
        mon = _rows(inv, "stp_bridges")
        prio = next((b.get("priority") for b in _rows(inv, "bridges") if b.get("priority")), None)
        infos[did] = {
            "id": did, "label": dev[did].get("identity") or dev[did].get("serial"), "model": dev[did].get("model"),
            "role": dev[did].get("role"), "state": dev[did].get("state"), "version": dev[did].get("ros_version"),
            "cpu": int(res["cpu-load"]) if str(res.get("cpu-load", "")).isdigit() else None,
            "mem": _pct(res.get("free-memory"), res.get("total-memory")),
            "poe_w": round(sum(float(p.get("poe-out-power") or 0) for p in _rows(inv, "poe_monitor")), 1),
            "stp_root": any(_true(b.get("root-bridge")) for b in mon),
            "stp_priority": int(str(prio), 16) if prio and str(prio).startswith("0x") else (int(prio) if str(prio or "").isdigit() else None),
            "ports": _ports(inv), "wans": _wans(inv),
            "parent": None, "level": 0, "uplink_port": None, "parent_port": None,
        }

    # kořen: gateway → zařízení s WAN → STP root → první
    def rank(did: str) -> tuple:
        i = infos[did]
        return (i["role"] != "gateway", not i["wans"], not i["stp_root"], i["state"] != "adopted", i["label"] or "")

    order: list[str] = []
    seen: set[str] = set()
    for root in sorted(ids, key=rank):
        if root in seen:
            continue
        seen.add(root)
        q = deque([root])
        while q:
            a = q.popleft()
            order.append(a)
            for b in sorted(adj[a], key=lambda x: (ROLE_ORDER.get(infos[x]["role"], 9), infos[x]["label"] or "")):
                if b in seen:
                    continue
                seen.add(b)
                infos[b].update(parent=a, level=infos[a]["level"] + 1,
                                parent_port=ports.get((a, b)), uplink_port=ports.get((b, a)))
                q.append(b)

    wans = []
    for did in order:
        if infos[did]["parent"] is None:
            for w in infos[did]["wans"]:
                wans.append(dict(w, device_id=did))
    st = stp.site_stp(devices, invs)
    warnings = [w for w in st["warnings"] if w["severity"] == "warning"]
    if st["blocked"]:
        warnings.append({"severity": "info", "text": f"STP blokuje {len(st['blocked'])} port(y) – v síti je redundantní cesta "
                                                    "(v pořádku, pokud je záměrná)"})
    for did in ids:
        if infos[did]["state"] == "offline":
            warnings.append({"severity": "warning", "text": f"{infos[did]['label']}: offline – spoj k němu je přerušený"})
    return {"nodes": [infos[d] for d in order], "wans": wans, "externals": externals, "warnings": warnings}


ROLE_ORDER = {"gateway": 0, "switch": 1, "ptp": 2, "ap": 3, "unknown": 4}
