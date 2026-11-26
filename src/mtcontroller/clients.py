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
SESSION_STALE_MIN = 20  # relace nepotvrzená déle (worker stál, lokalita offline) končí posledním potvrzením


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


def port_kinds(devices: list[dict], invs: dict[str, dict], owner: dict[str, str]) -> tuple[dict, dict]:
    """Z tabulek MAC adres: ({(zařízení, port): spravovaná zařízení viděná za portem}, {zařízení: port nahoru}).

    Port „nahoru“ je ten, kterým zařízení vidí bránu (kořen sítě). Takový port nikdy není portem klienta –
    zařízení přes něj vidí celou síť. Stejné pravidlo používá mapa; sousedé z MNDP/LLDP na to nestačí
    (AP za nespravovaným switchem nebo cizím bezdrátovým spojem žádné spravované zařízení „neslyší“).
    Bez známé brány se bere port, za kterým je vidět nejvíc spravovaných zařízení."""
    infra_seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for d in devices:
        did = str(d["id"])
        for h in _rows(invs.get(did, {}), "bridge_hosts"):
            if _true(h.get("local")):
                continue
            b = owner.get(_mac(h.get("mac-address")))
            port = str(h.get("on-interface") or "")
            if b and b != did and port:
                infra_seen[(did, port)].add(b)
    root = next((str(d["id"]) for d in devices if d.get("role") == "gateway"), None)
    upstream: dict[str, str] = {}
    for d in devices:
        did = str(d["id"])
        mine = {p: bs for (a, p), bs in infra_seen.items() if a == did}
        if not mine or did == root:
            continue
        via_root = sorted((-len(bs), p) for p, bs in mine.items() if root in bs) if root else []
        if via_root:
            upstream[did] = via_root[0][1]
        elif not root:
            best = sorted((-len(bs), p) for p, bs in mine.items())
            if len(best) == 1 or best[0][0] < best[1][0]:
                upstream[did] = best[0][1]
    return infra_seen, upstream


def correlate(devices: list[dict], invs: dict[str, dict]) -> list[dict]:
    infra_macs: set[str] = set()
    owner: dict[str, str] = {}
    for d in devices:
        for i in _rows(invs.get(str(d["id"]), {}), "interfaces"):
            if i.get("mac-address"):
                infra_macs.add(_mac(i["mac-address"]))
                owner.setdefault(_mac(i["mac-address"]), str(d["id"]))
    infra_seen, upstream = port_kinds(devices, invs, owner)
    # bezdrátový spoj mezi spravovanými zařízeními: (zařízení, rozhraní) → protistrana. Za takovým „portem“
    # nemůže být switch – MAC viděná jen na spoji patří někam za jeho vzdálenější konec.
    link_peer: dict[tuple[str, str], str] = {}
    for d in devices:
        did = str(d["id"])
        for r in _rows(invs.get(did, {}), "wifi_clients") + _rows(invs.get(did, {}), "wireless_clients"):
            peer = owner.get(_mac(r.get("mac-address")))
            if peer and peer != did and r.get("interface"):
                link_peer[(did, str(r["interface"]))] = peer
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

    # 3) poloha: drát z bridge host tabulek. Druh portu, kde je MAC vidět:
    #    edge   – za portem není žádné spravované zařízení → tady klient opravdu je,
    #    shared – za portem je i spravované zařízení (klient visí na nespravovaném switchi mezi nimi),
    #    up     – port nahoru k bráně; přes něj zařízení vidí celou síť, klient tam nikdy není.
    port_count: dict[tuple[str, str], int] = defaultdict(int)
    seen: dict[str, list[tuple[str, str, Any, str]]] = defaultdict(list)
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
            kind = ("up" if upstream.get(did) == port else
                    "shared" if port in uplinks or infra_seen.get((did, port)) else "edge")
            seen[mac].append((did, port, h.get("vid"), kind))
    for mac, places in seen.items():
        c = get(mac)
        if mac in wifi_seen:
            # VLAN wifi klienta doplníme z bridge host tabulky AP
            for did, port, vid, _ in places:
                if did == c["device_id"] and vid:
                    c["vlan"] = vid
            continue
        edge = [p for p in places if p[3] == "edge"]
        shared = [p for p in places if p[3] == "shared"]
        if edge:
            pick = min(edge, key=lambda p: port_count[(p[0], p[1])])
            c["candidates"] = len(edge)
            if len({p[0] for p in edge}) > 1:
                # koncový port na dvou zařízeních zároveň: klient se stěhuje, smyčka nebo vadný switch
                c["uncertain"] = True
        elif shared:
            # nejhlouběji = port, za kterým je nejméně spravovaných zařízení (a nejméně MAC)
            pick = min(shared, key=lambda p: (len(infra_seen.get((p[0], p[1]), ())), port_count[(p[0], p[1])]))
            c["shared"] = True
        else:
            # MAC je vidět jen na portech nahoru (klient právě odpojený, neúplné tabulky) – nejméně špatný odhad
            pick = min(places, key=lambda p: port_count[(p[0], p[1])])
            c["uncertain"] = True
        did, port, vid, _ = pick
        far = link_peer.get((did, port))
        if far and pick[3] != "edge":
            # jen na bezdrátovém spoji: klient je někde za protistranou, přesný port neznáme
            c.pop("shared", None)
            c.update(device_id=far, device=names[far], port="?", vlan=vid, uncertain=True, via_link=True)
            continue
        c.update(device_id=did, device=names[did], port=port, vlan=vid)
    # kolik drátových klientů sdílí port: víc než jeden (nebo port sdílený se spravovaným zařízením)
    # znamená nespravovaný switch za portem
    per_port: dict[tuple[str, str], int] = defaultdict(int)
    for c in clients.values():
        if c["device_id"] and not c["wifi"] and not c.get("via_link"):
            per_port[(c["device_id"], c["port"])] += 1
    for c in clients.values():
        if c["device_id"] and not c["wifi"]:
            c["port_clients"] = per_port.get((c["device_id"], c["port"]), 1)
            c["unmanaged"] = bool(c.get("shared")) or c["port_clients"] > 1

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
            "vlan": t(c.get("vlan"), 20), "ssid": t(w.get("ssid"), 64), "signal": t(w.get("signal"), 20),
            # jistá poloha = koncový port; odhad na sdíleném / páteřním portu se do přeskakování MAC nepočítá
            "firm": not (c.get("shared") or c.get("uncertain") or c.get("via_link"))}


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
            devs = tx.fetchall("""SELECT id, serial, identity, role::text AS role FROM device
                                  WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""",
                               {"s": str(s["site_id"])})
            invs = {str(d["id"]): inventory.load(tx, d["id"], SECTIONS) for d in devs}
            found = correlate(devs, invs)
            rows = [_row(c) for c in found
                    if present(c) and re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", c["mac"])]
            # nespravované switche: evidence portů, kde něco větví (nové hlásí alert)
            from . import unmanaged

            unmanaged.record(tx, s["tenant_id"], s["site_id"], unmanaged.detect(found), have_clients=bool(rows))
            if rows:
                tx.execute(_UPSERT, {"t": str(s["tenant_id"]), "s": str(s["site_id"]), "rows": rows})
            _sessions(tx, s["tenant_id"], s["site_id"], rows)
            # přesuny klientů mezi porty / AP: poloha v čase (cestování v čase) a přeskakování MAC
            from . import timemachine

            timemachine.record_moves(tx, s["tenant_id"], s["site_id"], rows)
            n += len(rows)
    with operator_scope(db) as tx:
        # lokality, které se přestaly hlásit: relace končí, kdy byly naposledy potvrzené
        tx.execute("""UPDATE client_session SET ended_at = seen_at
                      WHERE ended_at IS NULL AND seen_at < now() - make_interval(mins => %(m)s)""", {"m": SESSION_STALE_MIN})
    return n


def _sessions(tx, tenant_id: Any, site_id: Any, rows: list[dict]) -> None:
    """Relace klientů lokality: přítomným otevřít / potvrdit, ostatním ukončit."""
    macs = ",".join(r["mac"] for r in rows)
    # worker stál déle než SESSION_STALE_MIN → staré relace skončily tehdy, ne teď
    tx.execute("""UPDATE client_session SET ended_at = seen_at WHERE site_id = %(s)s AND ended_at IS NULL
                  AND seen_at < now() - make_interval(mins => %(m)s)""", {"s": str(site_id), "m": SESSION_STALE_MIN})
    tx.execute("""UPDATE client_session SET ended_at = now() WHERE site_id = %(s)s AND ended_at IS NULL
                  AND NOT (mac = ANY(string_to_array(%(macs)s, ',')))""", {"s": str(site_id), "macs": macs})
    if not rows:
        return
    tx.execute("""UPDATE client_session SET seen_at = now() WHERE site_id = %(s)s AND ended_at IS NULL
                  AND mac = ANY(string_to_array(%(macs)s, ','))""", {"s": str(site_id), "macs": macs})
    tx.execute(
        """INSERT INTO client_session (tenant_id, site_id, mac, wifi, device, port, ssid, vlan)
           SELECT %(t)s, %(s)s, x.mac, x.ssid IS NOT NULL OR x.signal IS NOT NULL, x.device, x.port, x.ssid, x.vlan
           FROM jsonb_to_recordset(%(rows)s) AS x(mac text, device text, port text, vlan text, ssid text, signal text)
           WHERE NOT EXISTS (SELECT 1 FROM client_session o WHERE o.site_id = %(s)s AND o.mac = x.mac AND o.ended_at IS NULL)""",
        {"t": str(tenant_id), "s": str(site_id), "rows": rows})


def timeline(tx, site_id: Any, hours: int = 24, now: Any = None) -> dict:
    """Počet připojených klientů v čase: {from, to, step_min, points: [{t, total, wifi}]}."""
    import datetime as dt

    hours = max(1, min(24 * RETENTION_DAYS, int(hours)))
    step = 5 if hours <= 6 else 15 if hours <= 24 else 60 if hours <= 24 * 7 else 240
    now = now or dt.datetime.now(dt.timezone.utc)
    end = now.replace(second=0, microsecond=0)
    end -= dt.timedelta(minutes=end.minute % (step if step < 60 else 60))
    if step > 60:
        end -= dt.timedelta(hours=end.hour % (step // 60))
    start = end - dt.timedelta(hours=hours)
    rows = tx.fetchall(
        """SELECT g AS t, count(s.mac) AS total, count(s.mac) FILTER (WHERE s.wifi) AS wifi
           FROM generate_series(%(a)s::timestamptz, %(b)s::timestamptz, make_interval(mins => %(step)s)) g
           LEFT JOIN client_session s ON s.site_id = %(s)s AND s.started_at <= g
                AND (s.ended_at IS NULL OR s.ended_at > g)
           GROUP BY g ORDER BY g""", {"a": start, "b": end, "step": step, "s": str(site_id)})
    first = tx.fetchone("SELECT min(started_at) AS t FROM client_session WHERE site_id = %(s)s", {"s": str(site_id)})
    return {"from": start, "to": end, "step_min": step, "since": first["t"] if first else None,
            "points": [{"t": r["t"], "total": r["total"], "wifi": r["wifi"]} for r in rows]}


def at(tx, site_id: Any, when: Any) -> list[dict]:
    """Klienti připojení v daný okamžik (podle relací), s údaji z historie klientů."""
    return tx.fetchall(
        """SELECT s.mac, c.ip, c.hostname, c.device_id, s.device, s.port, s.vlan, s.ssid, NULL AS signal,
                  c.first_seen, c.last_seen, s.wifi AS was_wifi, s.started_at, s.ended_at,
                  s.ended_at IS NULL AS online
           FROM client_session s LEFT JOIN client c ON c.site_id = s.site_id AND c.mac = s.mac
           WHERE s.site_id = %(s)s AND s.started_at <= %(w)s AND (s.ended_at IS NULL OR s.ended_at > %(w)s)
           ORDER BY s.device NULLS LAST, s.port, s.mac LIMIT 5000""", {"s": str(site_id), "w": when})


def purge(db: Database, days: int = RETENTION_DAYS) -> int:
    with operator_scope(db) as tx:
        tx.execute("DELETE FROM client_session WHERE ended_at < now() - make_interval(days => %(d)s)", {"d": days})
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
