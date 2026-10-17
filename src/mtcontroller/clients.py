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

from .db import Database, operator_scope, tenant_scope

# sekce inventury, které korelace potřebuje (worker nenačítá zbytek)
SECTIONS = ["interfaces", "neighbors", "dhcp_leases", "arp", "bridge_hosts", "wifi_clients", "wireless_clients"]
RETENTION_DAYS = 90


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


def present(c: dict) -> bool:
    """Je klient opravdu připojený? (vidět na portu / ve wifi nebo platný DHCP lease)"""
    return bool(c.get("port")) or c.get("dhcp_status") == "bound"


_UPSERT = """
INSERT INTO client (tenant_id, site_id, mac, ip, hostname, device_id, device, port, vlan, ssid, signal)
SELECT %(t)s, %(s)s, x.mac, x.ip, x.hostname, x.device_id::uuid, x.device, x.port, x.vlan, x.ssid, x.signal
FROM jsonb_to_recordset(%(rows)s) AS x(mac text, ip text, hostname text, device_id text, device text,
                                       port text, vlan text, ssid text, signal text)
ON CONFLICT (site_id, mac) DO UPDATE SET
  last_seen = now(),
  ip = coalesce(EXCLUDED.ip, client.ip), hostname = coalesce(EXCLUDED.hostname, client.hostname),
  device_id = coalesce(EXCLUDED.device_id, client.device_id), device = coalesce(EXCLUDED.device, client.device),
  port = coalesce(EXCLUDED.port, client.port), vlan = coalesce(EXCLUDED.vlan, client.vlan),
  ssid = EXCLUDED.ssid, signal = EXCLUDED.signal
"""


def _row(c: dict) -> dict:
    def t(v: Any, n: int) -> str | None:
        return str(v)[:n] if v not in (None, "") else None

    w = c.get("wifi") or {}
    return {"mac": c["mac"], "ip": t(c.get("ip"), 45), "hostname": t(c.get("hostname"), 255),
            "device_id": c.get("device_id"), "device": t(c.get("device"), 200), "port": t(c.get("port"), 100),
            "vlan": t(c.get("vlan"), 20), "ssid": t(w.get("ssid"), 64), "signal": t(w.get("signal"), 20)}


def record(db: Database) -> int:
    """Krok workeru: aktuální klienty všech lokalit uloží do historie. Vrací počet."""
    import re

    from . import inventory

    with operator_scope(db) as tx:
        sites = tx.fetchall("""SELECT DISTINCT tenant_id, site_id FROM device
                               WHERE state = 'adopted' AND site_id IS NOT NULL""")
    n = 0
    for s in sites:
        with tenant_scope(db, s["tenant_id"]) as tx:
            devs = tx.fetchall("""SELECT id, serial, identity FROM device
                                  WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""",
                               {"s": str(s["site_id"])})
            invs = {str(d["id"]): inventory.load(tx, d["id"], SECTIONS) for d in devs}
            rows = [_row(c) for c in correlate(devs, invs)
                    if present(c) and re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", c["mac"])]
            if rows:
                tx.execute(_UPSERT, {"t": str(s["tenant_id"]), "s": str(s["site_id"]), "rows": rows})
            n += len(rows)
    return n


def purge(db: Database, days: int = RETENTION_DAYS) -> int:
    with operator_scope(db) as tx:
        return tx.execute("DELETE FROM client WHERE last_seen < now() - make_interval(days => %(d)s)", {"d": days})


def history(tx, site_id: Any, days: int = 30) -> list[dict]:
    return tx.fetchall(
        """SELECT mac, ip, hostname, device_id, device, port, vlan, ssid, signal, first_seen, last_seen,
                  last_seen > now() - interval '10 minutes' AS online
           FROM client WHERE site_id = %(s)s AND last_seen > now() - make_interval(days => %(d)s)
           ORDER BY last_seen DESC LIMIT 5000""", {"s": str(site_id), "d": days})


def search(tx, q: str, limit: int = 100) -> list[dict]:
    """Hledání klienta ve všech lokalitách tenanta (MAC, IP, jméno)."""
    q = q.strip()
    if len(q) < 2:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return tx.fetchall(
        """SELECT c.mac, c.ip, c.hostname, c.device, c.port, c.vlan, c.ssid, c.first_seen, c.last_seen,
                  c.last_seen > now() - interval '10 minutes' AS online, s.name AS site, c.site_id
           FROM client c JOIN site s ON s.id = c.site_id
           WHERE c.mac ILIKE %(l)s OR c.ip ILIKE %(l)s OR c.hostname ILIKE %(l)s
           ORDER BY c.last_seen DESC LIMIT %(n)s""", {"l": like, "n": limit})
