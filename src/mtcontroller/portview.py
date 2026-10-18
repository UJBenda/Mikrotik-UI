"""Podklady pro panel portů ve stylu UniFi: co je na portu připojené (s ikonou),
anomálie portu za 24 h a jména/podsítě VLAN.

Vše jen čte – inventuru, tabulku klientů, historii portů a vzorky čítačů.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import defaultdict
from typing import Any

PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp)")
CLIENT_FRESH_MINUTES = 30

# typ zařízení podle modelu MikroTiku (board z MNDP/LLDP)
_ROS_BOARD = [
    (re.compile(r"^(rb)?(cap|wap|audience|netmetal|sxt|lhg|ldf|dynadish|mantbox|omnitik|netbox|groove|disc|cube|wireless wire)", re.I), "ap"),
    (re.compile(r"^(crs|css|netpower|fiberbox|cs)", re.I), "switch"),
    (re.compile(r"^(ccr|rb|hap|hex|l009|ltap|chateau|knot|ax|e50|ts|rds)", re.I), "router"),
]
# typ koncového zařízení podle hostname / platformy / popisu portu (heuristika)
_HOST_KIND = [
    (re.compile(r"(cam|ipc|kamer|hikvision|dahua|axis|nvr|dvr|reolink)", re.I), "camera"),
    (re.compile(r"(nas|synology|diskstation|qnap|truenas|backup)", re.I), "nas"),
    (re.compile(r"(print|tisk|mfp|laserjet|officejet|brother|canon|kyocera|ricoh|xerox|konica|epson|^hp[a-f0-9]{6})", re.I), "printer"),
    (re.compile(r"(srv|server|esx|vmware|proxmox|pve|idrac|ilo|ipmi|hyper-?v|dc\d)", re.I), "server"),
    (re.compile(r"(phone|iphone|android|galaxy|pixel|yealink|grandstream|snom|fanvil|cisco-?sep|sip)", re.I), "phone"),
    (re.compile(r"(uap|u6|u7|unifi|ap\d|-ap|ap-|aironet|ruckus|aruba)", re.I), "ap"),
    (re.compile(r"(switch|sw\d|-sw|sw-|usw|procurve|catalyst|tl-sg|gs\d{3})", re.I), "switch"),
    (re.compile(r"(tv|chromecast|appletv|roku|shield|bravia|webos|tizen)", re.I), "tv"),
    (re.compile(r"(pc|nb|ntb|desktop|laptop|notebook|workstation|win|macbook|imac|thinkpad|dell|lenovo)", re.I), "pc"),
]


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _port_of(iface: Any) -> list[str]:
    """Soused hlášený na "bridge/ether3" nebo "ether3,bridge" → ["ether3"]."""
    return [p for p in re.split(r"[,/]", str(iface or "")) if PHYS_RE.match(p)]


def device_kind(*texts: Any, mikrotik: bool = False) -> str:
    """Odhad typu zařízení pro ikonu: ap, switch, router, camera, nas, printer, server, phone, tv, pc, unknown."""
    if mikrotik:
        for t in texts:
            for rx, kind in _ROS_BOARD:
                if t and rx.search(str(t)):
                    return kind
        return "router"
    for t in texts:
        if not t:
            continue
        for rx, kind in _HOST_KIND:
            if rx.search(str(t)):
                return kind
    return "unknown"


def connected(inv: dict, clients: list[dict], managed: dict[str, str]) -> dict[str, dict]:
    """Co je na kterém fyzickém portu: {port: {name, kind, mac, ip, count, managed, source}}.

    Přednost má soused z MNDP/LLDP (je to infrastruktura), jinak klienti
    z korelace, kteří byli na portu vidět v posledních minutách.
    `managed` = {identity: role} spravovaných zařízení.
    """
    out: dict[str, dict] = {}
    comments = {str(i.get("name")): i.get("comment") for i in _rows(inv, "interfaces")}
    for n in _rows(inv, "neighbors"):
        ident = n.get("identity") or ""
        platform = str(n.get("platform") or "")
        board = n.get("board") or ""
        is_ros = "mikrotik" in platform.lower() or (bool(board) and not platform)
        for port in _port_of(n.get("interface")):
            if port in out:
                continue
            if ident in managed:
                kind = {"gateway": "router", "switch": "switch", "ap": "ap", "ptp": "ap"}.get(managed[ident], "router")
            else:
                kind = device_kind(board, ident, platform, mikrotik=is_ros)
                if kind == "unknown" and (n.get("system-caps") or "").find("bridge") >= 0:
                    kind = "switch"
            out[port] = {"name": ident or n.get("mac-address") or "?", "kind": kind,
                         "mac": n.get("mac-address"), "ip": n.get("address") or n.get("address4"),
                         "model": board or platform or None, "count": 1,
                         "managed": ident in managed, "source": "neighbor"}
    by_port: dict[str, list[dict]] = defaultdict(list)
    for c in clients:
        if c.get("port"):
            by_port[str(c["port"])].append(c)
    for port, cl in by_port.items():
        if port in out or not PHYS_RE.match(port):
            continue
        cl.sort(key=lambda c: (c.get("hostname") is None, c.get("ip") is None, c.get("mac") or ""))
        first = cl[0]
        name = first.get("hostname") or first.get("ip") or first.get("mac")
        if len(cl) == 1:
            kind = device_kind(first.get("hostname"), comments.get(port))
        else:
            # víc MAC za portem = nespravovaný switch, hub nebo virtualizace
            kind = "switch" if len(cl) > 3 else device_kind(first.get("hostname"), comments.get(port))
            name = f"{name} +{len(cl) - 1}"
        out[port] = {"name": name, "kind": kind, "mac": first.get("mac"), "ip": first.get("ip"),
                     "model": None, "count": len(cl), "managed": False, "source": "client"}
    return out


def recent_clients(tx, device_id: Any) -> list[dict]:
    return tx.fetchall(
        """SELECT mac, ip, hostname, port, vlan FROM client
           WHERE device_id = %(d)s AND last_seen >= now() - make_interval(mins => %(m)s)""",
        {"d": str(device_id), "m": CLIENT_FRESH_MINUTES})


def managed_roles(tx) -> dict[str, str]:
    return {r["identity"]: r["role"] for r in tx.fetchall(
        "SELECT identity, role::text AS role FROM device WHERE identity IS NOT NULL")}


# --- anomálie portu (0–100) za posledních 24 h -----------------------------------------------

def score(link_changes: int, flaps: int, errors: int, half_duplex: bool, fe_on_gbe: bool = False) -> dict:
    """Skóre anomálie a důvody. Váhy: výpadky linky nejvíc (smyčka/kabel), chyby logaritmicky."""
    s = 0.0
    why = []
    downs = link_changes // 2 + flaps
    if downs:
        s += min(60, 15 * downs)
        why.append(f"{downs}× výpadek linky")
    if errors > 0:
        s += min(40, 10 * math.log10(errors + 1))
        why.append(f"{errors} chyb")
    if half_duplex:
        s += 30
        why.append("half-duplex")
    if fe_on_gbe:
        s += 10
        why.append("jen 100 Mb/s")
    return {"score": int(min(100, round(s))), "reasons": why}


def anomalies(tx, device_id: Any, inv: dict) -> dict[str, dict]:
    """{port: {score, reasons}} – jen porty se skóre > 0."""
    d = str(device_id)
    changes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in tx.fetchall(
            """SELECT port, field, new FROM port_event
               WHERE device_id = %(d)s AND field IN ('link', 'flaps') AND at >= now() - interval '24 hours'""",
            {"d": d}):
        if r["field"] == "link":
            changes[r["port"]][0] += 1
        else:
            changes[r["port"]][1] += int(r["new"]) if str(r["new"] or "").isdigit() else 1
    errs: dict[str, int] = {}
    for r in tx.fetchall(
            """SELECT name, max(coalesce(rx_errors, 0) + coalesce(tx_errors, 0))
                          - min(coalesce(rx_errors, 0) + coalesce(tx_errors, 0)) AS e
               FROM interface_sample WHERE device_id = %(d)s AND at >= now() - interval '24 hours'
               GROUP BY name""", {"d": d}):
        if r["e"] and int(r["e"]) > 0:
            errs[r["name"]] = int(r["e"])
    duplex: dict[str, tuple[bool, bool]] = {}
    for m in _rows(inv, "ethernet_monitor"):
        name = str(m.get("name") or "")
        if m.get("status") != "link-ok":
            continue
        half = m.get("full-duplex") is not None and not _true(m.get("full-duplex"))
        rate = str(m.get("rate") or "")
        # FE linka na gigabitovém portu = typicky vadný kabel (2 páry)
        fe = rate.endswith("Mbps") and rate.startswith("100") and name.startswith("ether")
        duplex[name] = (half, fe)
    out = {}
    for port in set(changes) | set(errs) | set(duplex):
        if not PHYS_RE.match(port):
            continue
        lc, fl = changes.get(port, (0, 0))
        half, fe = duplex.get(port, (False, False))
        res = score(lc, fl, errs.get(port, 0), half, fe)
        if res["score"] > 0:
            out[port] = res
    return out


# --- jména a podsítě VLAN ----------------------------------------------------------------------

def vlan_names(inv: dict) -> dict[str, dict]:
    """{vid: {name, subnet}} z /interface/vlan a /ip/address (VLAN rozhraní na bridgi)."""
    nets: dict[str, str] = {}
    for a in _rows(inv, "addresses"):
        if _true(a.get("disabled")) or not a.get("interface") or not a.get("address"):
            continue
        try:
            net = ipaddress.ip_interface(str(a["address"])).network
        except ValueError:
            continue
        nets.setdefault(str(a["interface"]), str(net))
    out: dict[str, dict] = {}
    for v in _rows(inv, "vlans"):
        vid = str(v.get("vlan-id") or "")
        if not vid.isdigit() or _true(v.get("disabled")):
            continue
        name = str(v.get("name") or "")
        cur = out.get(vid)
        if cur and cur.get("subnet"):
            continue
        out[vid] = {"name": name or None, "subnet": nets.get(name)}
    return out
