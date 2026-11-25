"""Odezva a ztrátovost internetu z brány lokality (ping na veřejné cíle), řada pro Přehled.

Brána = spravované zařízení s rolí gateway. Lokalita bez spravované brány se neměří – z jiného zařízení
by ping měřil i vnitřní síť. Měří se každých 5 minut, 5 paketů na cíl; řada se drží 30 dní.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .db import Database, Tx, operator_scope, tenant_scope
from .rosapi import RosApiError

log = logging.getLogger("mtcontroller.wanprobe")
TARGETS = ("1.1.1.1", "8.8.8.8")
EVERY_MIN = 5
PING_COUNT = 5
KEEP_DAYS = 30
_last_prune = 0.0


def measure(api: Any) -> list[dict]:
    """[{target, loss, rtt_avg}] – cíl, na který se ping nepodařilo ani spustit, se vynechá."""
    from .wlink import ping_summary

    out = []
    for t in TARGETS:
        try:
            s = ping_summary(api.request("POST", "/ping", {"address": t, "count": str(PING_COUNT), "interval": "200ms"}))
        except RosApiError:
            continue
        if s.get("loss") is not None:
            out.append({"target": t, "loss": s["loss"], "rtt_avg": s.get("rtt_avg")})
    return out


def sample(db: Database, box: Any, limit: int = 50, force: bool = False) -> int:
    """Krok workeru: změří brány lokalit, které jsou na řadě. Vrací počet změřených lokalit."""
    from concurrent.futures import ThreadPoolExecutor

    from .services import device_api

    global _last_prune
    with operator_scope(db) as tx:
        if time.monotonic() - _last_prune > 3600:
            _last_prune = time.monotonic()
            tx.execute("DELETE FROM wan_probe WHERE at < now() - make_interval(days => %(d)s)", {"d": KEEP_DAYS})
        gws = tx.fetchall(
            """SELECT DISTINCT ON (d.site_id) d.id, d.tenant_id, d.site_id, host(d.tunnel_ip) AS tunnel_ip,
                      d.api_cert_fingerprint AS fp
               FROM device d
               WHERE d.role = 'gateway' AND d.state = 'adopted' AND d.site_id IS NOT NULL AND d.tunnel_ip IS NOT NULL
                 AND d.api_cert_fingerprint IS NOT NULL
                 AND (%(force)s OR NOT EXISTS (SELECT 1 FROM wan_probe p WHERE p.site_id = d.site_id
                                               AND p.at > now() - make_interval(mins => %(m)s)))
               ORDER BY d.site_id, d.created_at LIMIT %(n)s""", {"m": EVERY_MIN, "n": limit, "force": force})
        jobs = []
        for g in gws:
            try:
                jobs.append((g, device_api(tx, box, {"id": g["id"], "tunnel_ip": g["tunnel_ip"], "fp": g["fp"]}, timeout=15.0)))
            except Exception:
                log.info("brána %s: nejde sestavit REST klienta", g["id"])

    def one(job) -> bool:
        g, api = job
        try:
            rows = measure(api)
            if not rows:
                return False
            with tenant_scope(db, g["tenant_id"]) as tx:
                for r in rows:
                    tx.execute(
                        """INSERT INTO wan_probe (tenant_id, site_id, device_id, target, loss, rtt_avg)
                           VALUES (%(t)s, %(s)s, %(d)s, %(g)s, %(l)s, %(r)s) ON CONFLICT DO NOTHING""",
                        {"t": str(g["tenant_id"]), "s": str(g["site_id"]), "d": str(g["id"]), "g": r["target"],
                         "l": r["loss"], "r": r["rtt_avg"]})
            return True
        except Exception as e:
            log.info("měření internetu z brány %s selhalo: %s", g["id"], e)
            return False

    if not jobs:
        return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        return sum(pool.map(one, jobs))


def series(tx: Tx, site_id: Any, hours: int = 24) -> dict:
    """Řada pro graf: po okamžicích měření nejlepší cíl (internet funguje, když odpoví aspoň jeden),
    poslední hodnoty po cílech a dostupnost za období."""
    p = {"s": str(site_id), "h": int(hours)}
    step = 300 if hours <= 48 else 3600
    pts = tx.fetchall(
        """SELECT to_timestamp(floor(extract(epoch FROM at) / %(st)s) * %(st)s) AS at,
                  round(avg(loss)::numeric, 1)::float AS loss, round(avg(rtt)::numeric, 1)::float AS rtt
           FROM (SELECT date_trunc('minute', at) AS at, min(loss) AS loss, min(rtt_avg) AS rtt FROM wan_probe
                 WHERE site_id = %(s)s AND at > now() - make_interval(hours => %(h)s) GROUP BY 1) m
           GROUP BY 1 ORDER BY 1""", dict(p, st=step))
    last = tx.fetchall(
        """SELECT DISTINCT ON (target) target, loss, rtt_avg, at FROM wan_probe
           WHERE site_id = %(s)s AND at > now() - interval '20 minutes' ORDER BY target, at DESC""", p)
    av = tx.fetchone(
        """SELECT count(*) AS n, count(*) FILTER (WHERE loss < 100) AS ok
           FROM (SELECT date_trunc('minute', at) AS at, min(loss) AS loss FROM wan_probe
                 WHERE site_id = %(s)s AND at > now() - make_interval(hours => %(h)s) GROUP BY 1) m""", p)
    n = int(av["n"] or 0)
    return {"points": pts, "targets": [{"target": r["target"], "loss": r["loss"], "rtt_avg": r["rtt_avg"]} for r in last],
            "availability": round(100.0 * int(av["ok"] or 0) / n, 2) if n else None, "samples": n}
