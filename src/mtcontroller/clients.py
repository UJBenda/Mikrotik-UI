"""Klienti lokality: korelace DHCP, ARP, MAC tabulek bridge a wifi registrací.

Poloha drátového klienta: MAC je v bridge host tabulkách vidět na mnoha
portech (uplinky). Skutečný port je ten, kde MAC je a který NENÍ uplink
(port, na kterém má zařízení souseda z MNDP/LLDP). Když je MAC jen na uplinkách,
klient je za nespravovaným zařízením – vybere se okrajový port s nejvíc MAC.
Wifi klient má přesnou polohu z registration table (AP, rozhraní, SSID, signál).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _mac(v: Any) -> str:
    return str(v or "").upper()


def uplink_ports(inv: dict, infra_macs: set[str]) -> set[str]:
    """Porty vedoucí ke spravovaným zařízením (soused z MNDP/LLDP je náš)."""
    out = set()
    for n in _rows(inv, "neighbors"):
        if _mac(n.get("mac-address")) not in infra_macs:
            continue
        for part in str(n.get("interface", "")).split(","):
            if part:
                out.add(part)
    return out


def correlate(devices: list[dict], invs: dict[str, dict]) -> list[dict]:
    infra_macs: set[str] = set()
    for d in devices:
        for i in _rows(invs.get(str(d["id"]), {}), "interfaces"):
            if i.get("mac-address"):
                infra_macs.add(_mac(i["mac-address"]))
    names = {str(d["id"]): d.get("identity") or d.get("serial") for d in devices}

    clients: dict[str, dict] = {}

    def get(mac: str) -> dict:
        return clients.setdefault(mac, {"mac": mac, "ip": None, "hostname": None, "device_id": None,
                                        "device": None, "port": None, "vlan": None, "wifi": None,
                                        "dhcp_status": None, "candidates": 0})

    # 1) identita: DHCP a ARP
    for d in devices:
        inv = invs.get(str(d["id"]), {})
        for l in _rows(inv, "dhcp_leases"):
            mac = _mac(l.get("active-mac-address") or l.get("mac-address"))
            if not mac or mac in infra_macs:
                continue
            c = get(mac)
            c["ip"] = l.get("active-address") or l.get("address") or c["ip"]
            c["hostname"] = l.get("host-name") or c["hostname"]
            c["dhcp_status"] = l.get("status") or c["dhcp_status"]
        for a in _rows(inv, "arp"):
            mac = _mac(a.get("mac-address"))
            if mac and mac not in infra_macs and (a.get("complete") is None or _true(a.get("complete"))):
                c = get(mac)
                c["ip"] = c["ip"] or a.get("address")

    # 2) poloha: wifi (přesně)
    wifi_seen: set[str] = set()
    for d in devices:
        did = str(d["id"])
        inv = invs.get(did, {})
        for r in _rows(inv, "wifi_clients") + _rows(inv, "wireless_clients"):
            mac = _mac(r.get("mac-address"))
            if not mac:
                continue
            c = get(mac)
            c.update(device_id=did, device=names[did], port=r.get("interface"),
                     wifi={"ssid": r.get("ssid"), "signal": r.get("signal") or r.get("signal-strength"),
                           "tx_rate": r.get("tx-rate"), "rx_rate": r.get("rx-rate"), "uptime": r.get("uptime")})
            c["ip"] = c["ip"] or r.get("last-ip")
            wifi_seen.add(mac)

    # 3) poloha: drát z bridge host tabulek
    port_count: dict[tuple[str, str], int] = defaultdict(int)
    seen: dict[str, list[tuple[str, str, Any, bool]]] = defaultdict(list)
    for d in devices:
        did = str(d["id"])
        inv = invs.get(did, {})
        uplinks = uplink_ports(inv, infra_macs)
        for h in _rows(inv, "bridge_hosts"):
            if _true(h.get("local")):
                continue
            mac = _mac(h.get("mac-address"))
            port = str(h.get("on-interface") or "")
            if not mac or not port or mac in infra_macs:
                continue
            port_count[(did, port)] += 1
            seen[mac].append((did, port, h.get("vid"), port in uplinks))
    for mac, places in seen.items():
        c = get(mac)
        if mac in wifi_seen:
            # VLAN wifi klienta doplníme z bridge host tabulky AP
            for did, port, vid, _ in places:
                if did == c["device_id"] and vid:
                    c["vlan"] = vid
            continue
        edge = [p for p in places if not p[3]]
        pick = None
        if edge:
            pick = min(edge, key=lambda p: port_count[(p[0], p[1])])
            c["candidates"] = len(edge)
        else:
            # MAC je vidět jen na portech mezi spravovanými zařízeními (např. klient právě
            # odpojený nebo chybí informace o sousedech) – nejpravděpodobnější je port s nejméně MAC
            pick = min(places, key=lambda p: port_count[(p[0], p[1])])
            c["uncertain"] = True
        did, port, vid, _ = pick
        c.update(device_id=did, device=names[did], port=port, vlan=vid)

    return sorted(clients.values(), key=lambda c: (c["device"] or "~", c["port"] or "", c["ip"] or "", c["mac"]))
