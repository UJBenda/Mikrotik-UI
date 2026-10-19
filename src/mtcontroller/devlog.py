"""Logy ze zařízení: RouterOS posílá syslog (UDP) přes tunel na controller, služba mtc-syslog
(`python3 -m mtcontroller.devlog`) je ukládá k zařízení.

- Zapnutí po zařízení: off | errors (critical, error, warning) | info (+ info bez debug).
  Na zařízení jen `/system logging action` „mtc“ a pravidla s touto akcí – nic jiného se nemění.
- Zdroj = tunelová IP zařízení (src-address v akci); jiné adresy se zahodí.
- Pojistka: víc než syslog.max_lines_per_minute řádků za minutu od zařízení se zahodí a započítá.
- Objem po dnech (řádky, bajty, zahozené) – kolik logy stojí dat.
- Volitelně přeposlání do SIEM (syslog.forward) s identitou zařízení.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .db import Database, Tx, operator_scope, tenant_scope
from .routeros import q
from .services import NotFound, ServiceError, _s, audit, enqueue

log = logging.getLogger("mtc.devlog")

LEVELS = ("off", "errors", "info")
LEVEL_TOPICS = {"errors": ["critical", "error", "warning"], "info": ["critical", "error", "warning", "info,!debug"]}
ACTION = "mtc"
# odhad na drátě: UDP/IP + SSTP/TLS režie na řádek
WIRE_OVERHEAD = 90
_PRI = re.compile(rb"^<(\d{1,3})>")
_BSD_TS = re.compile(r"^[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d ")
_TOPICS = re.compile(r"^([a-z0-9-]+(?:,[a-z0-9-]+)+) (.*)$", re.S)  # RouterOS: vždy téma,závažnost
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def config_body(level: str, controller_ip: str, port: int, src_ip: str | None, wifi: bool = False) -> str:
    """Skript pro zařízení: akce „mtc“ (remote syslog na controller přes tunel) a pravidla podle úrovně.
    wifi = navíc události klientů Wi-Fi (téma wireless: připojení, odpojení s důvodem, roaming)."""
    if level not in LEVELS:
        raise ValueError("úroveň logů: off | errors | info")
    if wifi and level == "off":
        level = "errors"  # Wi-Fi události potřebují akci mtc
    lines = [f"/system logging remove [find action={q(ACTION)}]",
             f":do {{ /system logging action remove [find name={q(ACTION)}] }} on-error={{}}"]
    if level != "off":
        src = f" src-address={src_ip}" if src_ip else ""
        lines.append(f"/system logging action add name={q(ACTION)} target=remote remote={controller_ip} "
                     f"remote-port={int(port)}{src}")
        for t in LEVEL_TOPICS[level]:
            lines.append(f"/system logging add topics={q(t)} action={q(ACTION)}")
        if wifi and level != "info":
            lines.append(f"/system logging add topics={q('wireless,!debug')} action={q(ACTION)}")
    return "\n".join(lines) + "\n"


def parse(data: bytes) -> tuple[int, str, str]:
    """(závažnost 0–7, témata, zpráva) z jednoho syslog datagramu RouterOS (BSD i bez hlavičky)."""
    sev = 6
    m = _PRI.match(data)
    if m:
        sev = int(m.group(1)) & 7
        data = data[m.end():]
    text = _CTRL.sub(" ", data.decode("utf-8", "replace")).strip()
    if _BSD_TS.match(text):
        text = text[16:]
        # BSD hlavička: „Oct 19 20:53:00 host témata zpráva“ – host přeskočit, jen když za ním jsou témata
        parts = text.split(" ", 1)
        if len(parts) == 2 and _TOPICS.match(parts[1]):
            text = parts[1]
    topics = ""
    t = _TOPICS.match(text)
    if t and (":" not in t.group(1)) and len(t.group(1)) <= 100:
        topics, text = t.group(1), t.group(2)
    return sev, topics, text[:1000]


def set_level(db: Database, settings: Settings, tenant_id: Any, device_id: Any, level: str | None, user_id: Any = None,
              ip: str | None = None, wifi: bool | None = None) -> dict:
    """Úroveň logů a/nebo Wi-Fi události klientů (None = beze změny)."""
    with tenant_scope(db, tenant_id) as tx:
        d = tx.fetchone("SELECT state, syslog_level, syslog_wifi, host(tunnel_ip) AS tip FROM device WHERE id = %(d)s",
                        {"d": str(device_id)})
        if not d:
            raise NotFound("zařízení neexistuje")
        level = d["syslog_level"] if level is None else level
        wifi = d["syslog_wifi"] if wifi is None else wifi
        if level not in LEVELS:
            raise ServiceError("úroveň logů: off | errors | info")
        if d["state"] not in ("adopted", "offline", "provisioning"):
            raise ServiceError("logy lze nastavit jen spravovanému zařízení")
        tx.execute("""UPDATE device_command SET status = 'cancelled', finished_at = now() WHERE device_id = %(d)s
                      AND kind = 'syslog' AND status = 'queued'""", {"d": str(device_id)})
        tx.execute("UPDATE device SET syslog_level = %(l)s, syslog_wifi = %(w)s WHERE id = %(d)s",
                   {"l": level, "w": bool(wifi), "d": str(device_id)})
        cid = enqueue(tx, tenant_id, device_id, "syslog", {"level": level, "wifi": bool(wifi)}, user_id)
        audit(tx, "device.syslog", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              before={"level": d["syslog_level"], "wifi": d["syslog_wifi"]},
              after={"level": level, "wifi": bool(wifi), "command_id": cid}, ip=ip)
    return {"level": level, "wifi": bool(wifi), "command_id": cid}


def render(settings: Settings, cmd: dict, dev: dict) -> str:
    """Cíl = tunelová adresa koncentrátoru (zařízení IP controlleru nevidí); koncentrátor ho dst-natuje
    na controller a zdroj (tunelová IP zařízení) zůstane – podle něj se log přiřadí."""
    from .ipam import concentrator_address

    return config_body(str(cmd["params"].get("level") or "off"), str(concentrator_address(settings.tunnel.pool)),
                       settings.syslog.port, dev.get("tunnel_ip"), bool(cmd["params"].get("wifi")))


def query(tx: Tx, device_id: Any, text: str = "", min_severity: int = 7, limit: int = 300,
          before_id: int | None = None) -> list[dict]:
    """Logy zařízení od nejnovějších; text hledá v tématech i zprávě."""
    like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if text else None
    return tx.fetchall(
        """SELECT id, at, severity, topics, message FROM device_log
           WHERE device_id = %(d)s AND severity <= %(s)s
             AND (%(b)s::bigint IS NULL OR id < %(b)s::bigint)
             AND (%(l)s::text IS NULL OR message ILIKE %(l)s::text OR topics ILIKE %(l)s::text)
           ORDER BY id DESC LIMIT %(n)s""",
        {"d": str(device_id), "s": int(min_severity), "b": before_id, "l": like, "n": int(limit)})


def volume(tx: Tx, device_id: Any, days: int = 7) -> dict:
    rows = tx.fetchall(
        """SELECT day::text AS day, lines, bytes, dropped FROM device_log_day WHERE device_id = %(d)s
           AND day > current_date - %(n)s::int ORDER BY day""", {"d": str(device_id), "n": int(days)})
    n = len(rows) or 1
    total = sum(r["bytes"] + r["lines"] * WIRE_OVERHEAD for r in rows)
    return {"days": rows, "avg_bytes_per_day": round(total / n), "avg_lines_per_day": round(sum(r["lines"] for r in rows) / n),
            "dropped": sum(r["dropped"] for r in rows)}


def retention(db: Database, days: int) -> int:
    with operator_scope(db) as tx:
        r = tx.fetchone("""WITH d AS (DELETE FROM device_log WHERE at < now() - make_interval(days => %(n)s::int)
                           RETURNING 1) SELECT count(*) AS n FROM d""", {"n": int(days)})
        tx.execute("DELETE FROM device_log_day WHERE day < current_date - 400")
        tx.execute("DELETE FROM wifi_event WHERE at < now() - make_interval(days => %(n)s::int)", {"n": int(days)})
        tx.execute("DELETE FROM wifi_sample WHERE at < now() - make_interval(days => %(n)s::int)", {"n": min(int(days), 7)})
    return r["n"]


# --- příjem ---------------------------------------------------------------------------------------

class Collector:
    """Datagramy → buffer → zápis po tenantech. Zdroj se ověřuje proti tunelovým IP zařízení."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings
        self.devices: dict[str, dict] = {}
        self.loaded = 0.0
        self.buf: list[tuple] = []
        self.wifi: list[tuple] = []   # (tenant, site, device, čas, událost)
        self.rate: dict[str, list] = defaultdict(lambda: [0, 0])   # device → [minuta, počet]
        self.stats: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])  # (tenant, device, den) → [řádky, bajty, zahozené]
        self.fwd = None
        if settings.syslog.forward:
            host, _, port = settings.syslog.forward.rpartition(":")
            self.fwd = (host.strip("[]"), int(port))
            self.fwd_sock = socket.socket(socket.AF_INET6 if ":" in host.strip("[]") else socket.AF_INET, socket.SOCK_DGRAM)

    def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.loaded < 60:
            return
        with operator_scope(self.db) as tx:
            rows = tx.fetchall("""SELECT id, tenant_id, site_id, identity, serial, host(tunnel_ip) AS ip FROM device
                                  WHERE tunnel_ip IS NOT NULL AND (syslog_level <> 'off' OR syslog_wifi) AND state <> 'removed'""")
        self.devices = {r["ip"]: {"id": str(r["id"]), "tenant": str(r["tenant_id"]), "name": r["identity"] or r["serial"],
                                  "site": str(r["site_id"]) if r["site_id"] else None} for r in rows}
        self.loaded = time.monotonic()

    def receive(self, data: bytes, addr: str, now: float | None = None) -> bool:
        now = now or time.time()
        d = self.devices.get(addr)
        if not d:
            return False
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        st = self.stats[(d["tenant"], d["id"], day)]
        minute = int(now // 60)
        r = self.rate[d["id"]]
        if r[0] != minute:
            r[0], r[1] = minute, 0
        r[1] += 1
        if r[1] > self.settings.syslog.max_lines_per_minute:
            st[2] += 1
            return False
        sev, topics, msg = parse(data[:2048])
        st[0] += 1
        st[1] += len(data)
        at = datetime.fromtimestamp(now, timezone.utc)
        self.buf.append((d["tenant"], d["id"], at, sev, topics, msg))
        if "wireless" in topics.split(",") or "wifi" in topics.split(","):
            from .wifidiag import parse_event

            ev = parse_event(msg)
            if ev:
                self.wifi.append((d["tenant"], d.get("site"), d["id"], at, ev))
        if self.fwd:
            try:
                self.fwd_sock.sendto(f"<{8 + sev}>{d['name']} {topics} {msg}".encode()[:2048], self.fwd)
            except OSError:
                pass
        return True

    def flush(self) -> int:
        buf, self.buf = self.buf, []
        wifi, self.wifi = self.wifi, []
        wifi_by: dict[str, list] = defaultdict(list)
        for w in wifi:
            wifi_by[w[0]].append(w)
        stats, self.stats = self.stats, defaultdict(lambda: [0, 0, 0])
        by_tenant: dict[str, list] = defaultdict(list)
        for row in buf:
            by_tenant[row[0]].append(row)
        tenants = set(by_tenant) | {k[0] for k in stats} | set(wifi_by)
        for t in tenants:
            with tenant_scope(self.db, t) as tx:
                for _t, did, at, sev, topics, msg in by_tenant.get(t, []):
                    tx.execute("""INSERT INTO device_log (tenant_id, device_id, at, severity, topics, message)
                                  VALUES (%(t)s, %(d)s, %(a)s, %(s)s, %(tp)s, %(m)s)""",
                               {"t": t, "d": did, "a": at, "s": sev, "tp": topics, "m": msg})
                for _t, site, did, at, ev in wifi_by.get(t, []):
                    tx.execute("""INSERT INTO wifi_event (tenant_id, site_id, device_id, mac, at, kind, iface, signal, ft, reason)
                                  VALUES (%(t)s, %(s)s, %(d)s, %(m)s, %(a)s, %(k)s, %(i)s, %(sg)s, %(ft)s, %(r)s)""",
                               {"t": t, "s": site, "d": did, "m": ev["mac"], "a": at, "k": ev["kind"], "i": ev["iface"],
                                "sg": ev["signal"], "ft": ev["ft"], "r": ev["reason"]})
                for (tt, did, day), (lines, nbytes, dropped) in stats.items():
                    if tt != t:
                        continue
                    tx.execute("""INSERT INTO device_log_day (tenant_id, device_id, day, lines, bytes, dropped)
                                  VALUES (%(t)s, %(d)s, %(day)s::date, %(l)s, %(b)s, %(x)s)
                                  ON CONFLICT (device_id, day) DO UPDATE SET lines = device_log_day.lines + EXCLUDED.lines,
                                      bytes = device_log_day.bytes + EXCLUDED.bytes,
                                      dropped = device_log_day.dropped + EXCLUDED.dropped""",
                               {"t": t, "d": did, "day": day, "l": lines, "b": nbytes, "x": dropped})
        return len(buf)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, col: Collector) -> None:
        self.col = col

    def datagram_received(self, data: bytes, addr) -> None:
        self.col.receive(data, addr[0])


async def serve(db: Database, settings: Settings) -> None:
    col = Collector(db, settings)
    col.refresh(force=True)
    host = settings.syslog.listen or settings.tunnel.controller_ip
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(col), local_addr=(host, settings.syslog.port))
    log.info("syslog poslouchá na %s:%d", host, settings.syslog.port)
    last_ret = 0.0
    try:
        while True:
            await asyncio.sleep(2)
            try:
                col.refresh()
                col.flush()
                if time.monotonic() - last_ret > 3600:
                    n = retention(db, settings.syslog.retention_days)
                    last_ret = time.monotonic()
                    if n:
                        log.info("smazáno %d starých logů", n)
            except Exception:
                log.exception("zápis logů selhal")
    finally:
        transport.close()


def main() -> None:
    from .config import get_settings
    from .db import get_db

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(get_db(), get_settings()))


if __name__ == "__main__":
    main()
