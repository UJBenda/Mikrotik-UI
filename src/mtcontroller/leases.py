"""DHCP leases a statické IP (rezervace) jednoho DHCP serveru.

Přehled sítě: leases (statické = rezervace, dynamické), zařízení s ručně
nastavenou IP (jsou v ARP, ale ne v leases), výrobce podle MAC, poloha
z korelace klientů (switch/port, AP/SSID), poznámka z controlleru, mapa
adres podsítě a konflikty (stejná IP na víc MAC, ruční IP v rozsahu poolu).

Změny (rezervace, změna IP, popis, DHCP options, address-listy, blokování,
zrušení rezervace, nová DHCP option) jdou jako plán modulu `dhcp`
s návratovým skriptem – stejně jako změny portů.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any

from . import oui
from .health import pool_ranges
from .routeros import q

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
OPT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
LIST_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}(,[A-Za-z0-9_.-]{1,40}){0,9}$")
COMMENT_RE = re.compile(r"^[\x20-\x7e]{0,100}$")
MAP_MIN_PREFIX = 22   # mapa adres jen do /22 (1022 adres)
# předvolby DHCP options pro UI: (kód, popis, typ hodnoty)
OPTION_PRESETS = [(6, "DNS servery", "ip"), (42, "NTP servery", "ip"), (66, "TFTP server (telefony)", "text"),
                  (67, "Boot soubor", "text"), (150, "TFTP server Cisco", "ip"), (252, "WPAD URL", "text")]


class LeaseError(ValueError):
    pass


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def norm_mac(v: Any) -> str:
    h = re.sub(r"[^0-9A-Fa-f]", "", str(v or "")).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def _members(v: Any) -> list[str]:
    return [x for x in str(v or "").split(",") if x]


def find_server(inv: dict, name: str) -> dict:
    srv = next((s for s in _rows(inv, "dhcp_servers") if str(s.get("name")) == name), None)
    if not srv:
        raise LeaseError(f"DHCP server {name} na zařízení není")
    return srv


def server_network(inv: dict, srv: dict) -> tuple[ipaddress.IPv4Interface | None, list[tuple[int, int]]]:
    """(adresa rozhraní serveru = brána a podsíť, rozsahy poolu včetně next-pool)."""
    iface = str(srv.get("interface") or "")
    addr = None
    for a in _rows(inv, "addresses"):
        if a.get("interface") == iface and not _true(a.get("disabled")):
            try:
                ai = ipaddress.ip_interface(str(a.get("address")))
            except ValueError:
                continue
            if ai.version == 4:
                addr = ai
                break
    pools = {str(p.get("name")): p for p in _rows(inv, "ip_pools")}
    ranges: list[tuple[int, int]] = []
    name, seen = str(srv.get("address-pool") or ""), set()
    while name in pools and name not in seen:
        seen.add(name)
        ranges += pool_ranges(pools[name].get("ranges"))
        name = str(pools[name].get("next-pool") or "")
    return addr, ranges


def _in(ip: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= ip <= b for a, b in ranges)


def server_view(inv: dict, server: str, clients: dict[str, dict], notes: dict[str, str]) -> dict:
    """Přehled jednoho DHCP serveru. clients = {MAC: řádek tabulky client}, notes = {MAC: poznámka}."""
    srv = find_server(inv, server)
    addr, ranges = server_network(inv, srv)
    net = addr.network if addr else None
    rows: dict[str, dict] = {}
    for l in _rows(inv, "dhcp_leases"):
        if str(l.get("server")) != server:
            continue
        mac = norm_mac(l.get("active-mac-address") or l.get("mac-address"))
        if not mac:
            continue
        static = not _true(l.get("dynamic"))
        r = rows.get(mac)
        if r and r["kind"] == "static" and not static:
            continue
        rows[mac] = {
            "mac": mac, "ip": l.get("active-address") or l.get("address"),
            "reserved_ip": l.get("address") if static else None,
            "kind": "static" if static else "dynamic", "status": l.get("status"),
            "hostname": l.get("host-name"), "comment": l.get("comment") or "",
            "options": _members(l.get("dhcp-option")), "address_lists": l.get("address-lists") or "",
            "block_access": _true(l.get("block-access")), "expires": l.get("expires-after"),
            "last_seen": l.get("last-seen"), "disabled": _true(l.get("disabled")),
        }
    iface = str(srv.get("interface") or "")
    gw_ip = str(addr.ip) if addr else None
    for a in _rows(inv, "arp"):
        mac = norm_mac(a.get("mac-address"))
        ip = str(a.get("address") or "")
        if not mac or mac in rows or ip == gw_ip or (a.get("complete") is not None and not _true(a.get("complete"))):
            continue
        try:
            if net and ipaddress.IPv4Address(ip) not in net:
                continue
        except ValueError:
            continue
        if a.get("interface") not in (iface, None) and net is None:
            continue
        rows[mac] = {"mac": mac, "ip": ip, "reserved_ip": None, "kind": "manual", "status": "arp",
                     "hostname": None, "comment": "", "options": [], "address_lists": "", "block_access": False,
                     "expires": None, "last_seen": None, "disabled": False}
    by_ip: dict[str, list[str]] = {}
    for r in rows.values():
        c = clients.get(r["mac"]) or {}
        r["hostname"] = r["hostname"] or c.get("hostname")
        r["vendor"] = oui.vendor(r["mac"])
        r["note"] = notes.get(r["mac"], "")
        r["where"] = {"device": c.get("device"), "port": c.get("port"), "ssid": c.get("ssid"),
                      "signal": c.get("signal"), "seen": c.get("last_seen")} if c else None
        r["warnings"] = []
        if r["ip"]:
            by_ip.setdefault(str(r["ip"]), []).append(r["mac"])
            try:
                n = int(ipaddress.IPv4Address(str(r["ip"])))
            except ValueError:
                n = None
            if n is not None and r["kind"] == "manual" and _in(n, ranges):
                r["warnings"].append("ručně nastavená IP leží v rozsahu poolu – DHCP ji může přidělit jinému")
            if n is not None and r["kind"] == "static" and _in(n, ranges):
                r["warnings"].append("rezervace v rozsahu poolu (funguje, přehlednější je mimo pool)")
            if n is not None and net and ipaddress.IPv4Address(n) not in net:
                r["warnings"].append(f"IP mimo podsíť {net}")
    conflicts = {ip: macs for ip, macs in by_ip.items() if len(macs) > 1}
    for ip, macs in conflicts.items():
        for m in macs:
            rows[m]["warnings"].append(f"konflikt: IP {ip} používá i {', '.join(x for x in macs if x != m)}")
    items = sorted(rows.values(), key=lambda r: _ipkey(r["ip"]))
    used = {int(ipaddress.IPv4Address(str(r["ip"]))) for r in items if r["ip"] and _valid(r["ip"])}
    pool_size = sum(b - a + 1 for a, b in ranges)
    in_pool_used = sum(1 for n in used if _in(n, ranges))
    out = {
        "server": server, "interface": iface, "disabled": _true(srv.get("disabled")),
        "subnet": str(net) if net else None, "gateway": gw_ip, "lease_time": srv.get("lease-time"),
        "pool": [f"{ipaddress.IPv4Address(a)}-{ipaddress.IPv4Address(b)}" for a, b in ranges],
        "leases": items,
        "summary": {"static": sum(1 for r in items if r["kind"] == "static"),
                    "dynamic": sum(1 for r in items if r["kind"] == "dynamic"),
                    "manual": sum(1 for r in items if r["kind"] == "manual"),
                    "pool_size": pool_size, "pool_free": max(0, pool_size - in_pool_used),
                    "conflicts": len(conflicts)},
        "options": [{"name": o.get("name"), "code": o.get("code"), "value": o.get("value")}
                    for o in _rows(inv, "dhcp_options")],
        "presets": [{"code": c, "label": l, "type": t} for c, l, t in OPTION_PRESETS],
        "suggest": suggest_ip(net, ranges, used, gw_ip),
        "network": network_view(inv, net),
        "map": None,
    }
    if net and net.prefixlen >= MAP_MIN_PREFIX:
        kind_by_ip: dict[int, str] = {}
        prio = {"static": 0, "dynamic": 1, "manual": 2}
        for r in sorted(items, key=lambda r: prio[r["kind"]]):
            if r["ip"] and _valid(r["ip"]):
                kind_by_ip.setdefault(int(ipaddress.IPv4Address(str(r["ip"]))), r["kind"])
        cells = []
        for h in net.hosts():
            n = int(h)
            if str(h) == gw_ip:
                st = "gateway"
            elif n in kind_by_ip:
                st = kind_by_ip[n] + ("-conflict" if str(h) in conflicts else "")
            else:
                st = "pool" if _in(n, ranges) else "free"
            cells.append(st)
        out["map"] = {"first": str(next(net.hosts())), "cells": cells}
    return out


def _valid(ip: Any) -> bool:
    try:
        ipaddress.IPv4Address(str(ip))
        return True
    except ValueError:
        return False


def _ipkey(ip: Any) -> int:
    try:
        return int(ipaddress.IPv4Address(str(ip)))
    except ValueError:
        return 1 << 33


def suggest_ip(net, ranges, used: set[int], gw: str | None) -> str | None:
    """První volná adresa mimo pool (pro rezervaci)."""
    if not net:
        return None
    for h in net.hosts():
        n = int(h)
        if str(h) != gw and n not in used and not _in(n, ranges):
            return str(h)
    return None


# --- plán změny leasu --------------------------------------------------------------------------

def option_value(kind: str, raw: str) -> str:
    """Hodnota DHCP option ve formátu RouterOS: ip → 'a.b.c.d' (víc IP přes čárku), text → s'…', hex → 0x…"""
    raw = str(raw or "").strip()
    if kind == "ip":
        ips = [x.strip() for x in raw.split(",") if x.strip()]
        if not ips or len(ips) > 8:
            raise LeaseError("zadej 1–8 IP adres oddělených čárkou")
        for x in ips:
            try:
                ipaddress.IPv4Address(x)
            except ValueError:
                raise LeaseError(f"neplatná IP {x!r}") from None
        return "".join(f"'{x}'" for x in ips)
    if kind == "text":
        if not re.fullmatch(r"[\x20-\x26\x28-\x7e]{1,200}", raw):
            raise LeaseError("text: 1–200 znaků ASCII bez apostrofu")
        return f"s'{raw}'"
    if kind == "hex":
        h = raw.lower().removeprefix("0x")
        if not re.fullmatch(r"([0-9a-f]{2}){1,120}", h):
            raise LeaseError("hex: sudý počet hex číslic")
        return "0x" + h
    raise LeaseError("typ hodnoty: ip | text | hex")


def config_basis(inv: dict) -> str:
    """Otisk statických leases a DHCP options (dynamické leases se mění pořád)."""
    data = {
        "l": sorted((str(l.get("server")), norm_mac(l.get("mac-address")), str(l.get("address")),
                     str(l.get("comment") or ""), str(l.get("dhcp-option") or ""), str(l.get("address-lists") or ""),
                     str(l.get("block-access") or "")) for l in _rows(inv, "dhcp_leases") if not _true(l.get("dynamic"))),
        "o": sorted((str(o.get("name")), str(o.get("code")), str(o.get("value"))) for o in _rows(inv, "dhcp_options")),
        "s": sorted(str(s.get("name")) for s in _rows(inv, "dhcp_servers")),
        "n": sorted((str(n.get("address")), str(n.get("dns-server") or ""), str(n.get("ntp-server") or ""),
                     str(n.get("domain") or ""), str(n.get("dhcp-option") or "")) for n in _rows(inv, "dhcp_networks")),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def plan(inv: dict, server: str, mac: str, desired: dict) -> dict:
    """{changes, apply, revert, warnings, summary}. desired: address, comment, options, address_lists,
    block_access, remove, new_options=[{name, code, type, value}]. Nic nemění."""
    srv = find_server(inv, server)
    mac = norm_mac(mac)
    if not MAC_RE.match(mac):
        raise LeaseError("neplatná MAC adresa")
    addr, ranges = server_network(inv, srv)
    leases = [l for l in _rows(inv, "dhcp_leases") if str(l.get("server")) == server]
    static = next((l for l in leases if norm_mac(l.get("mac-address")) == mac and not _true(l.get("dynamic"))), None)
    dynamic = next((l for l in leases if norm_mac(l.get("active-mac-address") or l.get("mac-address")) == mac
                    and _true(l.get("dynamic"))), None)
    S, M = q(server), q(mac)
    find_st = f"[find server={S} mac-address={M} dynamic=no]"
    apply: list[str] = []
    revert: list[str] = []
    changes: list[dict] = []
    warn: list[str] = []

    known_opts = {str(o.get("name")) for o in _rows(inv, "dhcp_options")}
    for o in desired.get("new_options") or []:
        name, code = str(o.get("name") or ""), o.get("code")
        if not OPT_NAME_RE.match(name):
            raise LeaseError("jméno DHCP option: písmena, číslice, _ . - (max. 40)")
        if name in known_opts:
            raise LeaseError(f"DHCP option {name} už existuje")
        try:
            code = int(code)
        except (TypeError, ValueError):
            raise LeaseError("kód DHCP option musí být číslo") from None
        if not 1 <= code <= 254:
            raise LeaseError("kód DHCP option 1–254")
        val = option_value(str(o.get("type") or ""), str(o.get("value") or ""))
        apply.append(f'/ip dhcp-server option add name={q(name)} code={code} value={q(val)} comment="mtc"')
        revert.append(f'/ip dhcp-server option remove [find name={q(name)} comment="mtc"]')
        changes.append({"field": "option", "before": None, "after": f"{name} ({code}) = {val}"})
        known_opts.add(name)

    if desired.get("remove"):
        if not static:
            raise LeaseError("klient nemá statickou rezervaci")
        apply.append(f"/ip dhcp-server lease remove {find_st}")
        revert.append(_add_cmd(S, M, static))
        changes.append({"field": "remove", "before": static.get("address"), "after": None})
        return _finish(changes, apply, revert, warn, mac, srv)

    cur = static or {}
    want_ip = str(desired.get("address") or cur.get("address") or (dynamic or {}).get("active-address")
                   or (dynamic or {}).get("address") or "")
    try:
        ipn = ipaddress.IPv4Address(want_ip)
    except ValueError:
        raise LeaseError("zadej platnou IPv4 adresu") from None
    if addr:
        net = addr.network
        if ipn not in net or ipn in (net.network_address, net.broadcast_address):
            raise LeaseError(f"IP {ipn} není použitelná adresa v podsíti {net}")
        if ipn == addr.ip:
            raise LeaseError("to je adresa brány")
    for l in leases:
        other = norm_mac(l.get("active-mac-address") or l.get("mac-address"))
        if other != mac and str(l.get("active-address") or l.get("address")) == str(ipn):
            raise LeaseError(f"IP {ipn} už má {other}" + (" (rezervace)" if not _true(l.get("dynamic")) else ""))
    for a in _rows(inv, "arp"):
        other = norm_mac(a.get("mac-address"))
        if other and other != mac and str(a.get("address")) == str(ipn):
            raise LeaseError(f"IP {ipn} právě používá {other} (ARP)")
    if _in(int(ipn), ranges):
        warn.append("IP leží v rozsahu poolu – rezervace funguje, ale přehlednější je adresa mimo pool")
    sets: list[str] = []
    olds: list[str] = []

    def field(key: str, ros: str, new: str, old: str, label: str) -> None:
        if new != old:
            sets.append(f"{ros}={q(new)}")
            olds.append(f"{ros}={q(old)}")
            changes.append({"field": label, "before": old or None, "after": new or None})

    if "comment" in desired:
        c = str(desired.get("comment") or "")
        if not COMMENT_RE.match(c):
            raise LeaseError("popis na MikroTiku: max. 100 znaků ASCII")
        field("comment", "comment", c, str(cur.get("comment") or ""), "popis")
    if "options" in desired:
        opts = [str(o) for o in desired.get("options") or []]
        for o in opts:
            if o not in known_opts:
                raise LeaseError(f"DHCP option {o} na zařízení není")
        field("options", "dhcp-option", ",".join(opts), str(cur.get("dhcp-option") or ""), "DHCP options")
    if "address_lists" in desired:
        al = str(desired.get("address_lists") or "").replace(" ", "")
        if al and not LIST_RE.match(al):
            raise LeaseError("address-listy: jména oddělená čárkou")
        field("address_lists", "address-lists", al, str(cur.get("address-lists") or ""), "address-listy")
    if "block_access" in desired:
        b = "yes" if desired.get("block_access") else "no"
        old = "yes" if _true(cur.get("block-access")) else "no"
        if b != old:
            sets.append(f"block-access={b}")
            olds.append(f"block-access={old}")
            changes.append({"field": "zablokovat", "before": old == "yes", "after": b == "yes"})

    if static:
        if str(ipn) != str(static.get("address")):
            sets.insert(0, f"address={ipn}")
            olds.insert(0, f"address={static.get('address')}")
            changes.insert(0, {"field": "IP", "before": static.get("address"), "after": str(ipn)})
        if not sets:
            return _finish(changes, apply, revert, warn, mac, srv)
        apply.append(f"/ip dhcp-server lease set {find_st} " + " ".join(sets))
        revert.append(f"/ip dhcp-server lease set {find_st} " + " ".join(olds))
    elif dynamic:
        # dynamický lease → statický (rezervace), pak nastavení
        apply.append(f"/ip dhcp-server lease make-static [find server={S} mac-address={M} dynamic=yes]")
        apply.append(f"/ip dhcp-server lease set {find_st} " + " ".join([f"address={ipn}"] + sets))
        revert.append(f"/ip dhcp-server lease remove {find_st}")
        old_ip = dynamic.get("active-address") or dynamic.get("address")
        changes.insert(0, {"field": "rezervace", "before": f"dynamická {old_ip}", "after": f"statická {ipn}"})
        if str(old_ip) != str(ipn):
            warn.append("klient dostane novou IP až při obnovení leasu (nebo po odpojení a připojení)")
    else:
        apply.append(f"/ip dhcp-server lease add server={S} mac-address={M} address={ipn} " + " ".join(sets))
        revert.append(f"/ip dhcp-server lease remove {find_st}")
        changes.insert(0, {"field": "rezervace", "before": None, "after": f"statická {ipn}"})
    return _finish(changes, apply, revert, warn, mac, srv)


def _add_cmd(S: str, M: str, l: dict) -> str:
    parts = [f"/ip dhcp-server lease add server={S} mac-address={M} address={l.get('address')}"]
    for key, ros in (("comment", "comment"), ("dhcp-option", "dhcp-option"), ("address-lists", "address-lists")):
        if l.get(key):
            parts.append(f"{ros}={q(str(l[key]))}")
    if _true(l.get("block-access")):
        parts.append("block-access=yes")
    return " ".join(parts)


def _finish(changes, apply, revert, warn, mac, srv) -> dict:
    if not changes:
        return {"changes": [], "apply": "", "revert": "", "warnings": [], "summary": "beze změny"}
    desc = ", ".join(f"{c['field']} {_fmt(c['before'])} → {_fmt(c['after'])}" for c in changes)
    return {"changes": changes, "apply": "\n".join(apply) + "\n", "revert": "\n".join(reversed(revert)) + "\n",
            "warnings": warn, "summary": f"DHCP {srv.get('name')} {mac}: {desc}"}


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "ano" if v else "ne"
    return str(v) if v not in (None, "") else "–"


# --- nastavení sítě DHCP (platí pro všechny klienty serveru) ------------------------------------------

DOMAIN_RE = re.compile(r"^([A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}$")


def find_network(inv: dict, net: ipaddress.IPv4Network | None) -> dict | None:
    if net is None:
        return None
    for n in _rows(inv, "dhcp_networks"):
        try:
            if ipaddress.ip_network(str(n.get("address")), strict=False) == net:
                return n
        except ValueError:
            continue
    return None


def network_view(inv: dict, net) -> dict | None:
    n = find_network(inv, net)
    if not n:
        return None
    return {"address": n.get("address"), "gateway": n.get("gateway") or "",
            "dns": [x for x in str(n.get("dns-server") or "").split(",") if x],
            "ntp": [x for x in str(n.get("ntp-server") or "").split(",") if x],
            "domain": n.get("domain") or "", "options": _members(n.get("dhcp-option"))}


def _ip_list(v: Any, label: str) -> list[str]:
    items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v or "")) if x]
    if len(items) > 4:
        raise LeaseError(f"{label}: nejvýš 4 adresy")
    out = []
    for x in items:
        try:
            out.append(str(ipaddress.IPv4Address(str(x).strip())))
        except ValueError:
            raise LeaseError(f"{label}: neplatná IP {x!r}") from None
    return out


def network_plan(inv: dict, server: str, desired: dict) -> dict:
    """Globální nastavení sítě DHCP serveru: DNS, NTP, doména a DHCP options pro všechny klienty
    (/ip dhcp-server network). desired: dns, ntp, domain, options, new_options. Nic nemění."""
    srv = find_server(inv, server)
    addr, _ranges = server_network(inv, srv)
    if not addr:
        raise LeaseError("rozhraní DHCP serveru nemá IPv4 adresu")
    n = find_network(inv, addr.network)
    if not n:
        raise LeaseError(f"pro síť {addr.network} není /ip dhcp-server network")
    A = q(str(n.get("address")))
    find = f"[find address={A}]"
    apply: list[str] = []
    revert: list[str] = []
    changes: list[dict] = []
    known_opts = {str(o.get("name")) for o in _rows(inv, "dhcp_options")}
    for o in desired.get("new_options") or []:
        name, code = str(o.get("name") or ""), o.get("code")
        if not OPT_NAME_RE.match(name):
            raise LeaseError("jméno DHCP option: písmena, číslice, _ . - (max. 40)")
        if name in known_opts:
            raise LeaseError(f"DHCP option {name} už existuje")
        try:
            code = int(code)
        except (TypeError, ValueError):
            raise LeaseError("kód DHCP option musí být číslo") from None
        if not 1 <= code <= 254:
            raise LeaseError("kód DHCP option 1–254")
        val = option_value(str(o.get("type") or ""), str(o.get("value") or ""))
        apply.append(f'/ip dhcp-server option add name={q(name)} code={code} value={q(val)} comment="mtc"')
        revert.append(f'/ip dhcp-server option remove [find name={q(name)} comment="mtc"]')
        changes.append({"field": "option", "before": None, "after": f"{name} ({code}) = {val}"})
        known_opts.add(name)
    sets, olds = [], []

    def field(ros: str, new: str, old: str, label: str) -> None:
        if new != old:
            sets.append(f"{ros}={q(new)}")
            olds.append(f"{ros}={q(old)}")
            changes.append({"field": label, "before": old or None, "after": new or None})

    if "dns" in desired:
        field("dns-server", ",".join(_ip_list(desired.get("dns"), "DNS")), str(n.get("dns-server") or ""), "DNS")
    if "ntp" in desired:
        field("ntp-server", ",".join(_ip_list(desired.get("ntp"), "NTP")), str(n.get("ntp-server") or ""), "NTP")
    if "domain" in desired:
        d = str(desired.get("domain") or "").strip()
        if d and (len(d) > 100 or not DOMAIN_RE.match(d)):
            raise LeaseError("doména: např. firma.local")
        field("domain", d, str(n.get("domain") or ""), "doména")
    if "options" in desired:
        opts = [str(o) for o in desired.get("options") or []]
        if len(opts) > 20:
            raise LeaseError("nejvýš 20 DHCP options")
        for o in opts:
            if o not in known_opts:
                raise LeaseError(f"DHCP option {o} na zařízení není")
        field("dhcp-option", ",".join(opts), str(n.get("dhcp-option") or ""), "DHCP options sítě")
    if sets:
        apply.append(f"/ip dhcp-server network set {find} " + " ".join(sets))
        revert.append(f"/ip dhcp-server network set {find} " + " ".join(olds))
    if not changes:
        return {"changes": [], "apply": "", "revert": "", "warnings": [], "summary": "beze změny"}
    desc = ", ".join(f"{c['field']} {_fmt(c['before'])} → {_fmt(c['after'])}" for c in changes)
    return {"changes": changes, "apply": "\n".join(apply) + "\n", "revert": "\n".join(reversed(revert)) + "\n",
            "warnings": ["klienti dostanou nové nastavení při obnovení leasu"],
            "summary": f"DHCP síť {n.get('address')} ({srv.get('name')}): {desc}"}


def server_for_client(invs: dict[str, dict], mac: str, ip: str | None) -> tuple[str, str] | None:
    """(id zařízení, jméno DHCP serveru) pro klienta: podle leasu s jeho MAC, jinak podle podsítě jeho IP."""
    m = norm_mac(mac)
    for did, inv in invs.items():
        for l in _rows(inv, "dhcp_leases"):
            if norm_mac(l.get("active-mac-address") or l.get("mac-address")) == m and l.get("server"):
                return did, str(l["server"])
    try:
        cip = ipaddress.IPv4Address(str(ip)) if ip else None
    except ValueError:
        cip = None
    if cip:
        for did, inv in invs.items():
            for srv in _rows(inv, "dhcp_servers"):
                addr, _r = server_network(inv, srv)
                if addr and cip in addr.network and not _true(srv.get("disabled")):
                    return did, str(srv.get("name"))
    return None
