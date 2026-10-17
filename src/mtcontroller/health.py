"""Zdravotní kontroly z inventury, které nejsou vidět na první pohled:
zaplnění DHCP poolů a synchronizace hodin (NTP)."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

POOL_FULL_PCT = 90
POOL_MIN_SIZE = 8  # malé pooly (pár adres pro VPN apod.) neřešíme


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def pool_ranges(ranges: Any) -> list[tuple[int, int]]:
    """"10.0.0.10-10.0.0.254,10.0.1.0/24" → [(start, end), …] jako čísla IPv4."""
    out = []
    for part in str(ranges or "").split(","):
        part = part.strip()
        try:
            if "-" in part:
                a, b = (int(ipaddress.IPv4Address(x.strip())) for x in part.split("-", 1))
                if b >= a:
                    out.append((a, b))
            elif "/" in part:
                n = ipaddress.IPv4Network(part, strict=False)
                out.append((int(n.network_address), int(n.broadcast_address)))
            elif part:
                a = int(ipaddress.IPv4Address(part))
                out.append((a, a))
        except ValueError:
            continue
    return out


def dhcp_pools(inv: dict) -> list[dict]:
    """Využití poolů DHCP serverů: [{server, pool, size, used, pct}]."""
    pools = {str(p.get("name")): p for p in _rows(inv, "ip_pools")}
    leases = [l for l in _rows(inv, "dhcp_leases") if str(l.get("status", "")) in ("bound", "offered")]
    out = []
    for srv in _rows(inv, "dhcp_servers"):
        if _true(srv.get("disabled")) or not srv.get("address-pool") or srv.get("address-pool") == "static-only":
            continue
        ranges: list[tuple[int, int]] = []
        name, seen = str(srv["address-pool"]), set()
        while name in pools and name not in seen:  # řetěz next-pool
            seen.add(name)
            ranges += pool_ranges(pools[name].get("ranges"))
            name = str(pools[name].get("next-pool") or "")
        size = sum(b - a + 1 for a, b in ranges)
        if size < POOL_MIN_SIZE:
            continue
        used = 0
        for l in leases:
            if str(l.get("server")) != str(srv.get("name")):
                continue
            try:
                ip = int(ipaddress.IPv4Address(str(l.get("active-address") or l.get("address"))))
            except ValueError:
                continue
            if any(a <= ip <= b for a, b in ranges):
                used += 1
        out.append({"server": srv.get("name"), "pool": srv["address-pool"], "size": size, "used": used,
                    "pct": round(100 * used / size)})
    return out


def uptime_seconds(v: Any) -> int:
    """RouterOS uptime "2w3d04:05:06" / "5d" / "00:10:00" → sekundy."""
    s = str(v or "")
    total = 0
    for n, unit in re.findall(r"(\d+)([wdhms])", s):
        total += int(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    m = re.search(r"(\d+):(\d+):(\d+)$", s)
    if m:
        total += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return total


def ntp_unsynced(inv: dict) -> str | None:
    """Stav NTP klienta, pokud je zapnutý a hodiny nejsou synchronizované
    (déle než hodinu po startu – po bootu chvíli trvá)."""
    c = inv.get("ntp_client") if isinstance(inv.get("ntp_client"), dict) else {}
    res = inv.get("resource") if isinstance(inv.get("resource"), dict) else {}
    if not _true(c.get("enabled")) or uptime_seconds(res.get("uptime")) < 3600:
        return None
    st = str(c.get("status") or "")
    return None if st == "synchronized" else (st or "neznámý")
