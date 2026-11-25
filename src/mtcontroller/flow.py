"""Provoz podle klientů: brána posílá NetFlow v9 / IPFIX (UDP) přes tunel, controller z toků skládá hodinové
součty „místní klient × protistrana × protokol × port služby“.

- Zapnutí po bráně (`device.flow_enabled`): na zařízení jen cíl v `/ip traffic-flow target` a `enabled=yes`.
  Cizí cíle (vlastní kolektor správce) zůstávají; při vypnutí se traffic-flow vypne, jen když žádný jiný cíl není.
- Zdroj = tunelová IP zařízení; datagramy odjinud se zahodí. Příjem běží ve službě mtc-syslog (viz devlog.serve).
- Obsah provozu se nevidí – jen kdo s kým, kterým protokolem a portem a kolik. Jména protistran se nepřekládají.
- Brána vidí jen provoz, který jde přes její CPU: provoz přepínaný čipem (v rámci jedné VLAN) v tocích není.
- Paměť a disk: klíče se drží v paměti do zápisu (po 5 minutách), na bránu a hodinu zůstane nejvýš
  `flow.max_rows_per_hour` řádků, zbytek se sečte do řádku „ostatní“ (peer 0.0.0.0, proto 0, port 0).
"""

from __future__ import annotations

import ipaddress
import logging
import struct
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .db import Database, Tx, operator_scope, tenant_scope
from .routeros import q
from .services import NotFound, ServiceError, _s, audit, enqueue

log = logging.getLogger("mtc.flow")

FLUSH_SECONDS = 300
OTHER = ("0.0.0.0", 0, 0)   # peer, proto, port řádku „ostatní“
MAX_TEMPLATES = 4096
MAX_KEYS = 200_000          # strop klíčů v paměti mezi zápisy (pak se nové klíče sčítají do „ostatní“)

# pole NetFlow v9 / IPFIX, která čteme (ostatní se přeskočí podle délky ze šablony)
F_BYTES, F_PKTS, F_PROTO, F_SPORT, F_SRC4, F_DPORT, F_DST4 = 1, 2, 4, 7, 8, 11, 12
F_SRC6, F_DST6, F_NAT_SRC4, F_NAT_DST4 = 27, 28, 225, 226
_WANT = {F_BYTES: "bytes", F_PKTS: "pkts", F_PROTO: "proto", F_SPORT: "sport", F_DPORT: "dport",
         F_SRC4: "src", F_DST4: "dst", F_SRC6: "src", F_DST6: "dst", F_NAT_SRC4: "nat_src", F_NAT_DST4: "nat_dst"}
_ADDR = {F_SRC4, F_DST4, F_SRC6, F_DST6, F_NAT_SRC4, F_NAT_DST4}

SERVICES = {(6, 443): "HTTPS", (17, 443): "QUIC", (6, 80): "HTTP", (17, 53): "DNS", (6, 53): "DNS", (6, 853): "DNS-TLS",
            (6, 22): "SSH", (6, 25): "SMTP", (6, 465): "SMTP", (6, 587): "SMTP", (6, 993): "IMAP", (6, 143): "IMAP",
            (6, 995): "POP3", (6, 110): "POP3", (17, 123): "NTP", (6, 3389): "RDP", (17, 3389): "RDP", (6, 445): "SMB",
            (6, 21): "FTP", (17, 500): "IPsec", (17, 4500): "IPsec", (17, 51820): "WireGuard", (17, 1194): "OpenVPN",
            (6, 1194): "OpenVPN", (6, 8291): "WinBox", (17, 5060): "SIP", (6, 5060): "SIP", (6, 1883): "MQTT",
            (6, 8883): "MQTT", (17, 3478): "STUN", (6, 5223): "Apple Push", (6, 5228): "Google Push",
            (6, 23): "Telnet", (6, 8080): "HTTP", (6, 8443): "HTTPS"}
PROTOS = {1: "ICMP", 6: "TCP", 17: "UDP", 47: "GRE", 50: "ESP", 58: "ICMPv6"}


def service_name(proto: int, port: int) -> str:
    if (proto, port) == (0, 0):
        return "ostatní"
    name = SERVICES.get((proto, port))
    p = PROTOS.get(proto, str(proto))
    return name or (f"{p}/{port}" if port else p)


# --- rozbor datagramů ------------------------------------------------------------------------------

class Parser:
    """NetFlow v5, v9 a IPFIX → seznam toků. Šablony se pamatují po zdroji (adresa, source id / observation domain)."""

    def __init__(self) -> None:
        self.templates: dict[tuple, list[tuple[int, int]] | None] = {}

    def parse(self, data: bytes, source: str) -> list[dict]:
        if len(data) < 4:
            return []
        ver = struct.unpack_from("!H", data)[0]
        try:
            if ver == 9:
                return self._v9(data, source)
            if ver == 10:
                return self._ipfix(data, source)
            if ver == 5:
                return self._v5(data)
        except (struct.error, IndexError, ValueError):
            pass
        return []

    @staticmethod
    def _v5(data: bytes) -> list[dict]:
        count = struct.unpack_from("!H", data, 2)[0]
        out = []
        for i in range(min(count, 30)):
            o = 24 + i * 48
            if o + 48 > len(data):
                break
            src, dst, _nh, _in, _out, pkts, octets, _f, _l, sport, dport, _pad, _fl, proto = struct.unpack_from(
                "!4s4s4sHHIIIIHHBBB", data, o)
            out.append({"src": str(ipaddress.ip_address(src)), "dst": str(ipaddress.ip_address(dst)),
                        "sport": sport, "dport": dport, "proto": proto, "bytes": octets, "pkts": pkts})
        return out

    def _remember(self, key: tuple, fields: list[tuple[int, int]] | None) -> None:
        if len(self.templates) >= MAX_TEMPLATES and key not in self.templates:
            self.templates.clear()   # obrana proti zahlcení paměti; šablony přijdou znovu
        self.templates[key] = fields

    def _v9(self, data: bytes, source: str) -> list[dict]:
        sid = struct.unpack_from("!I", data, 16)[0]
        out: list[dict] = []
        o = 20
        while o + 4 <= len(data):
            fsid, flen = struct.unpack_from("!HH", data, o)
            if flen < 4 or o + flen > len(data):
                break
            body = data[o + 4:o + flen]
            if fsid == 0:
                p = 0
                while p + 4 <= len(body):
                    tid, n = struct.unpack_from("!HH", body, p)
                    p += 4
                    if p + 4 * n > len(body):
                        break
                    self._remember((source, sid, tid), [struct.unpack_from("!HH", body, p + 4 * i) for i in range(n)])
                    p += 4 * n
            elif fsid == 1:
                p = 0
                while p + 6 <= len(body):
                    tid, slen, olen = struct.unpack_from("!HHH", body, p)
                    self._remember((source, sid, tid), None)   # data možností (statistiky exportéru) nečteme
                    p += 6 + slen + olen
                    if slen + olen == 0:
                        break
            elif fsid >= 256:
                out += self._records(body, self.templates.get((source, sid, fsid)))
            o += flen
        return out

    def _ipfix(self, data: bytes, source: str) -> list[dict]:
        total, _t, _seq, dom = struct.unpack_from("!HIII", data, 2)
        end = min(total, len(data))
        out: list[dict] = []
        o = 16
        while o + 4 <= end:
            sid, slen = struct.unpack_from("!HH", data, o)
            if slen < 4 or o + slen > end:
                break
            body = data[o + 4:o + slen]
            if sid in (2, 3):
                p = 0
                while p + 4 <= len(body):
                    tid, n = struct.unpack_from("!HH", body, p)
                    p += 4
                    if sid == 3:
                        p += 2   # počet polí rozsahu
                    fields: list[tuple[int, int]] = []
                    ok = True
                    for _ in range(n):
                        if p + 4 > len(body):
                            ok = False
                            break
                        fid, fl = struct.unpack_from("!HH", body, p)
                        p += 4
                        if fid & 0x8000:
                            p += 4            # číslo výrobce; pole výrobců nečteme (id 0 = přeskočit)
                            fid = 0
                        fields.append((fid, fl))
                    if not ok or n == 0:
                        break
                    self._remember((source, dom, tid), None if sid == 3 else fields)
            elif sid >= 256:
                out += self._records(body, self.templates.get((source, dom, sid)))
            o += slen
        return out

    @staticmethod
    def _records(body: bytes, fields: list[tuple[int, int]] | None) -> list[dict]:
        if not fields:
            return []
        fixed = sum(fl for _f, fl in fields if fl != 0xFFFF)
        out: list[dict] = []
        p = 0
        # zbytek kratší než nejmenší záznam je výplň
        while p + max(fixed, 1) <= len(body) and len(out) < 2000:
            rec: dict[str, Any] = {}
            for fid, fl in fields:
                if fl == 0xFFFF:              # proměnná délka (IPFIX)
                    fl = body[p]
                    p += 1
                    if fl == 255:
                        fl = struct.unpack_from("!H", body, p)[0]
                        p += 2
                if p + fl > len(body):
                    return out
                name = _WANT.get(fid)
                if name:
                    raw = body[p:p + fl]
                    if fid in _ADDR:
                        if fl in (4, 16):
                            rec[name] = str(ipaddress.ip_address(raw))
                    elif fl <= 8:
                        rec[name] = int.from_bytes(raw, "big")
                p += fl
            if "src" in rec and "dst" in rec:
                out.append(rec)
            if fixed == 0:
                break
        return out


def _local(ip: str | None) -> bool:
    """Adresa z vnitřní sítě (neveřejná)? Nulová adresa (nevyplněné pole) se nepočítá."""
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not a.is_global and not a.is_unspecified and not a.is_multicast


def classify(rec: dict) -> tuple[str, str, int, int, int, int] | None:
    """Tok → (klient, protistrana, protokol, port služby, bajty ven, bajty dovnitř).

    Klient = strana ve vnitřní síti. Odchozí tok má místní zdroj; u příchozího je cílem veřejná adresa brány
    a místní klient je v poli adresy po překladu (NAT). Když místní není žádná strana, bere se zdroj jako klient."""
    src, dst = rec.get("src"), rec.get("dst")
    if not src or not dst:
        return None
    proto = int(rec.get("proto") or 0) & 0xFF
    nbytes = int(rec.get("bytes") or 0)
    ports = proto in (6, 17, 132)
    sport = int(rec.get("sport") or 0) & 0xFFFF if ports else 0
    dport = int(rec.get("dport") or 0) & 0xFFFF if ports else 0
    nat_dst = rec.get("nat_dst")
    if _local(src):
        return src, dst, proto, dport, nbytes, 0
    if _local(nat_dst) and nat_dst != dst:
        return str(nat_dst), src, proto, sport, 0, nbytes
    if _local(dst):
        return dst, src, proto, sport, 0, nbytes
    return src, dst, proto, dport, nbytes, 0


# --- zapnutí na zařízení ---------------------------------------------------------------------------

def config_body(enabled: bool, controller_ip: str, port: int, src_ip: str | None) -> str:
    """Skript pro bránu: náš cíl toků (pozná se podle adresy a portu) a zapnutí traffic-flow.
    Cizí cíle nechává být; při vypnutí vypne traffic-flow jen tehdy, když žádný jiný cíl nezbyl."""
    ipaddress.ip_address(controller_ip)
    find = f"[find dst-address={controller_ip} port={int(port)}]"
    lines = [f"/ip traffic-flow target remove {find}"]
    if enabled:
        src = ""
        if src_ip:
            ipaddress.ip_address(src_ip)
            src = f" src-address={src_ip}"
        lines.append(f"/ip traffic-flow target add dst-address={controller_ip} port={int(port)} version=9{src}")
        # aktivní tok hlásit po minutě (výchozí je 30 minut – graf by se zpožďoval)
        lines.append("/ip traffic-flow set enabled=yes active-flow-timeout=1m")
        lines.append(f":log info {q('mtc: netflow zapnut')}")
    else:
        lines.append(":if ([:len [/ip traffic-flow target find]] = 0) do={ /ip traffic-flow set enabled=no }")
        lines.append(f":log info {q('mtc: netflow vypnut')}")
    return "\n".join(lines) + "\n"


def render(settings: Settings, cmd: dict, dev: dict) -> str:
    """Cíl = tunelová adresa koncentrátoru (dst-nat na controller, zdroj zůstane tunelová IP brány)."""
    from .ipam import concentrator_address

    dst = settings.tunnel.controller_ip if settings.tunnel.local else str(concentrator_address(settings.tunnel.pool))
    return config_body(bool(cmd["params"].get("enabled")), dst, settings.flow.port, dev.get("tunnel_ip"))


def set_enabled(db: Database, tenant_id: Any, device_id: Any, enabled: bool, user_id: Any = None,
                ip: str | None = None) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        d = tx.fetchone("SELECT state, role, flow_enabled FROM device WHERE id = %(d)s", {"d": str(device_id)})
        if not d:
            raise NotFound("zařízení neexistuje")
        if d["state"] not in ("adopted", "offline"):
            raise ServiceError("sběr provozu lze zapnout jen spravovanému zařízení")
        if enabled and d["role"] != "gateway":
            raise ServiceError("sběr provozu se zapíná na bráně lokality (zařízení s rolí Brána)")
        tx.execute("""UPDATE device_command SET status = 'cancelled', finished_at = now() WHERE device_id = %(d)s
                      AND kind = 'flow' AND status = 'queued'""", {"d": str(device_id)})
        tx.execute("UPDATE device SET flow_enabled = %(e)s WHERE id = %(d)s", {"e": bool(enabled), "d": str(device_id)})
        cid = enqueue(tx, tenant_id, device_id, "flow", {"enabled": bool(enabled)}, user_id)
        audit(tx, "device.flow", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              before={"enabled": d["flow_enabled"]}, after={"enabled": bool(enabled), "command_id": cid}, ip=ip)
    return {"enabled": bool(enabled), "command_id": cid}


# --- příjem ----------------------------------------------------------------------------------------

class Collector:
    """Datagramy → toky → součty v paměti → zápis po tenantech. Zdroj se ověřuje proti tunelovým IP bran."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings
        self.parser = Parser()
        self.devices: dict[str, dict] = {}
        self.loaded = 0.0
        self.agg: dict[tuple, list[int]] = {}       # (zařízení, hodina, klient, peer, proto, port) → [ven, dovnitř, pakety, toky]
        self.rate: dict[str, list] = defaultdict(lambda: [0, 0])
        self.dropped = 0
        self.last_flush = time.monotonic()

    def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.loaded < 60:
            return
        with operator_scope(self.db) as tx:
            rows = tx.fetchall("""SELECT id, tenant_id, site_id, host(tunnel_ip) AS ip FROM device
                                  WHERE tunnel_ip IS NOT NULL AND flow_enabled AND site_id IS NOT NULL
                                    AND state <> 'removed'""")
        self.devices = {r["ip"]: {"id": str(r["id"]), "tenant": str(r["tenant_id"]), "site": str(r["site_id"])}
                        for r in rows}
        self.loaded = time.monotonic()

    def receive(self, data: bytes, addr: str, now: float | None = None) -> int:
        """Vrátí počet započtených toků (0 = neznámý zdroj, limit nebo nic k přečtení)."""
        now = now or time.time()
        d = self.devices.get(addr)
        if not d:
            return 0
        minute = int(now // 60)
        r = self.rate[d["id"]]
        if r[0] != minute:
            r[0], r[1] = minute, 0
        r[1] += 1
        if r[1] > self.settings.flow.max_packets_per_minute:
            self.dropped += 1
            return 0
        hour = datetime.fromtimestamp(now - now % 3600, timezone.utc)
        n = 0
        for rec in self.parser.parse(data, addr):
            c = classify(rec)
            if not c:
                continue
            client, peer, proto, port, up, down = c
            key = (d["id"], hour, client, peer, proto, port)
            a = self.agg.get(key)
            if a is None:
                if len(self.agg) >= MAX_KEYS:
                    key = (d["id"], hour, client, *OTHER)
                a = self.agg.setdefault(key, [0, 0, 0, 0])
            a[0] += up
            a[1] += down
            a[2] += int(rec.get("pkts") or 0)
            a[3] += 1
            n += 1
        return n

    def due(self) -> bool:
        return time.monotonic() - self.last_flush >= FLUSH_SECONDS

    def flush(self) -> int:
        """Zapíše součty. Na bránu a hodinu jde do DB nejvýš max_rows_per_hour největších řádků z této dávky,
        zbytek se sečte po klientech do „ostatní“."""
        agg, self.agg = self.agg, {}
        self.last_flush = time.monotonic()
        if not agg:
            return 0
        dev = {d["id"]: d for d in self.devices.values()}
        groups: dict[tuple, list] = defaultdict(list)
        for key, val in agg.items():
            groups[key[:2]].append((key, val))
        limit = self.settings.flow.max_rows_per_hour
        rows: dict[str, dict[tuple, list[int]]] = defaultdict(dict)
        for (did, _hour), items in groups.items():
            d = dev.get(did)
            if not d:
                continue   # sběr mezitím vypnutý
            items.sort(key=lambda kv: kv[1][0] + kv[1][1], reverse=True)
            out = rows[d["tenant"]]
            for i, (key, val) in enumerate(items):
                if i >= limit and key[3:] != OTHER:
                    key = (*key[:3], *OTHER)
                cur = out.get(key)
                if cur is None:
                    out[key] = list(val)
                else:
                    for j in range(4):
                        cur[j] += val[j]
        n = 0
        for tenant, items2 in rows.items():
            with tenant_scope(self.db, tenant) as tx:
                for (did, hour, client, peer, proto, port), (up, down, pkts, flows) in items2.items():
                    tx.execute(
                        """INSERT INTO flow_hourly (tenant_id, site_id, device_id, hour, client, peer, proto, port,
                                                    bytes_up, bytes_down, packets, flows)
                           VALUES (%(t)s, %(s)s, %(d)s, %(h)s, %(c)s::inet, %(p)s::inet, %(pr)s, %(po)s,
                                   %(u)s, %(dn)s, %(k)s, %(f)s)
                           ON CONFLICT (device_id, hour, client, peer, proto, port) DO UPDATE SET
                               bytes_up = flow_hourly.bytes_up + EXCLUDED.bytes_up,
                               bytes_down = flow_hourly.bytes_down + EXCLUDED.bytes_down,
                               packets = flow_hourly.packets + EXCLUDED.packets,
                               flows = flow_hourly.flows + EXCLUDED.flows""",
                        {"t": tenant, "s": dev[did]["site"], "d": did, "h": hour, "c": client, "p": peer, "pr": proto,
                         "po": port, "u": up, "dn": down, "k": pkts, "f": flows})
                    n += 1
        return n


def compact(db: Database, keep: int) -> int:
    """Uzavřené hodiny (poslední tři): nad `keep` největších řádků na bránu a hodinu se zbytek sečte do „ostatní“."""
    with operator_scope(db) as tx:
        r = tx.fetchone(
            """WITH r AS (
                   SELECT ctid AS c, row_number() OVER (PARTITION BY device_id, hour
                                                        ORDER BY bytes_up + bytes_down DESC) AS rn
                   FROM flow_hourly
                   WHERE hour < date_trunc('hour', now()) AND hour >= date_trunc('hour', now()) - interval '3 hours'
                     AND NOT (peer = '0.0.0.0'::inet AND proto = 0 AND port = 0)),
               del AS (DELETE FROM flow_hourly f USING r WHERE f.ctid = r.c AND r.rn > %(k)s::int
                       RETURNING f.tenant_id, f.site_id, f.device_id, f.hour, f.client, f.bytes_up, f.bytes_down,
                                 f.packets, f.flows),
               ins AS (INSERT INTO flow_hourly (tenant_id, site_id, device_id, hour, client, peer, proto, port,
                                                bytes_up, bytes_down, packets, flows)
                       SELECT tenant_id, site_id, device_id, hour, client, '0.0.0.0'::inet, 0, 0, sum(bytes_up),
                              sum(bytes_down), sum(packets), sum(flows)::int FROM del
                       GROUP BY tenant_id, site_id, device_id, hour, client
                       ON CONFLICT (device_id, hour, client, peer, proto, port) DO UPDATE SET
                           bytes_up = flow_hourly.bytes_up + EXCLUDED.bytes_up,
                           bytes_down = flow_hourly.bytes_down + EXCLUDED.bytes_down,
                           packets = flow_hourly.packets + EXCLUDED.packets,
                           flows = flow_hourly.flows + EXCLUDED.flows
                       RETURNING 1)
               SELECT (SELECT count(*) FROM del) AS n, (SELECT count(*) FROM ins) AS m""", {"k": int(keep)})
    return int(r["n"])


def retention(db: Database, days: int) -> int:
    with operator_scope(db) as tx:
        r = tx.fetchone("""WITH d AS (DELETE FROM flow_hourly WHERE hour < now() - make_interval(days => %(n)s::int)
                           RETURNING 1) SELECT count(*) AS n FROM d""", {"n": int(days)})
    return int(r["n"])


# --- čtení pro UI ----------------------------------------------------------------------------------

def site_traffic(tx: Tx, site_id: Any, hours: int = 24, client: str | None = None, top: int = 15) -> dict:
    """Provoz lokality za posledních `hours` hodin: brány a stav sběru, součty, hodinová řada,
    nejaktivnější klienti, protistrany a služby. S `client` (IP) jen pro jednoho klienta."""
    if client:
        try:
            client = str(ipaddress.ip_address(client))
        except ValueError:
            raise ServiceError("neplatná adresa klienta")
    gws = tx.fetchall(
        """SELECT d.id, coalesce(nullif(d.identity, ''), d.serial) AS name, d.state, d.flow_enabled,
                  (d.tunnel_ip IS NOT NULL) AS tunnel,
                  (SELECT max(hour) FROM flow_hourly f WHERE f.device_id = d.id) AS last_hour
           FROM device d WHERE d.site_id = %(s)s AND d.state IN ('adopted', 'offline')
             AND (d.role = 'gateway' OR d.flow_enabled) ORDER BY name""", {"s": str(site_id)})
    p = {"s": str(site_id), "h": int(hours), "c": client, "n": int(top)}
    where = """f.site_id = %(s)s AND f.hour > now() - make_interval(hours => %(h)s::int)
               AND (%(c)s::text IS NULL OR f.client = %(c)s::inet)"""
    tot = tx.fetchone(f"""SELECT coalesce(sum(bytes_up), 0) AS up, coalesce(sum(bytes_down), 0) AS down,
                                 coalesce(sum(flows), 0) AS flows, count(DISTINCT client) AS clients
                          FROM flow_hourly f WHERE {where}""", p)
    series = tx.fetchall(f"""SELECT f.hour, sum(bytes_up) AS up, sum(bytes_down) AS down FROM flow_hourly f
                             WHERE {where} GROUP BY f.hour ORDER BY f.hour""", p)
    clients = tx.fetchall(
        f"""SELECT host(f.client) AS ip, sum(f.bytes_up) AS up, sum(f.bytes_down) AS down, sum(f.flows) AS flows,
                   (SELECT c.mac FROM client c WHERE c.site_id = %(s)s AND c.ip = host(f.client)
                    ORDER BY c.last_seen DESC LIMIT 1) AS mac,
                   (SELECT c.hostname FROM client c WHERE c.site_id = %(s)s AND c.ip = host(f.client)
                    ORDER BY c.last_seen DESC LIMIT 1) AS hostname
            FROM flow_hourly f WHERE {where}
            GROUP BY f.client ORDER BY sum(f.bytes_up) + sum(f.bytes_down) DESC LIMIT %(n)s""", p)
    peers = tx.fetchall(
        f"""SELECT host(peer) AS ip, sum(bytes_up) AS up, sum(bytes_down) AS down, count(DISTINCT client) AS clients,
                   (array_agg(port ORDER BY bytes_up + bytes_down DESC))[1] AS port,
                   (array_agg(proto ORDER BY bytes_up + bytes_down DESC))[1] AS proto
            FROM flow_hourly f WHERE {where} AND NOT (peer = '0.0.0.0'::inet AND proto = 0 AND port = 0)
            GROUP BY peer ORDER BY sum(bytes_up) + sum(bytes_down) DESC LIMIT %(n)s""", p)
    services = tx.fetchall(
        f"""SELECT proto, port, sum(bytes_up) AS up, sum(bytes_down) AS down, count(DISTINCT client) AS clients
            FROM flow_hourly f WHERE {where}
            GROUP BY proto, port ORDER BY sum(bytes_up) + sum(bytes_down) DESC LIMIT %(n)s""", p)

    def num(r: dict, *keys: str) -> dict:
        return {**r, **{k: int(r[k] or 0) for k in keys}}

    return {
        "hours": int(hours), "client": client,
        "gateways": [{"id": str(g["id"]), "name": g["name"], "state": g["state"], "enabled": g["flow_enabled"],
                      "tunnel": bool(g["tunnel"]), "last_hour": g["last_hour"]} for g in gws],
        "total": num(tot, "up", "down", "flows", "clients"),
        "series": [num(r, "up", "down") for r in series],
        "clients": [num(r, "up", "down", "flows") for r in clients],
        "peers": [dict(num(r, "up", "down", "clients"), service=service_name(int(r["proto"]), int(r["port"])))
                  for r in peers],
        "services": [dict(num(r, "up", "down", "clients"), name=service_name(int(r["proto"]), int(r["port"])))
                     for r in services],
    }
