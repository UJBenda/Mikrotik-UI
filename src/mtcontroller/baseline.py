"""Dostupnost zařízení a odchylky od jeho vlastního normálu.

**Dostupnost** se počítá z alertů `offline` (vznik = zařízení se přestalo hlásit, pominutí = hlásí se
znovu) – nic nového se neukládá a historie je k dispozici hned. Okno začíná nejdřív adopcí zařízení.

**Normál** = hodinové průměry CPU a paměti (`device_hourly`, plní worker z inventury) a provozu
nejvytíženějšího rozhraní (`interface_hourly`). Poslední dokončená hodina se porovná se stejnou hodinou
dne za předchozích 14 dní; hlásí se jen výrazná odchylka (alert `anomaly`, varování), a to až když je
aspoň 5 dní historie. Práh je vždy relativní (3 směrodatné odchylky) i absolutní, aby tiché zařízení
nehlásilo skok z 2 na 9 %.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .db import Database, Tx, operator_scope

BASE_DAYS = 14
MIN_DAYS = 5
SIGMA = 3.0
CPU_STEP, CPU_FLOOR = 25, 50           # aspoň o 25 bodů nad normál a aspoň 50 %
MEM_STEP, MEM_FLOOR = 15, 70
TRAFFIC_FACTOR, TRAFFIC_FLOOR = 3.0, 20_000_000      # aspoň 3× normál a aspoň 20 Mb/s
QUIET_FACTOR, QUIET_BASE = 10.0, 5_000_000           # pod desetinou normálu, když normál je aspoň 5 Mb/s

SAMPLE_SQL = """
INSERT INTO device_hourly (tenant_id, device_id, hour, cpu_sum, cpu_max, mem_sum, samples, last_at)
SELECT tenant_id, id, date_trunc('hour', now()), cpu, cpu, mem, 1, inventory_at FROM (
  SELECT d.tenant_id, d.id, d.inventory_at, (i.data->>'cpu-load')::int AS cpu,
         round(100 - 100.0 * (i.data->>'free-memory')::numeric / (i.data->>'total-memory')::numeric)::int AS mem
  FROM device d JOIN device_inventory i ON i.device_id = d.id AND i.section = 'resource'
  WHERE d.state = 'adopted' AND d.inventory_at >= date_trunc('hour', now())
    AND i.data->>'cpu-load' ~ '^[0-9]{1,3}$' AND i.data->>'free-memory' ~ '^[0-9]{1,15}$'
    AND i.data->>'total-memory' ~ '^[1-9][0-9]{0,14}$'
) s
ON CONFLICT (device_id, hour) DO UPDATE SET
  cpu_sum = device_hourly.cpu_sum + EXCLUDED.cpu_sum, cpu_max = greatest(device_hourly.cpu_max, EXCLUDED.cpu_max),
  mem_sum = device_hourly.mem_sum + EXCLUDED.mem_sum, samples = device_hourly.samples + 1, last_at = EXCLUDED.last_at
WHERE device_hourly.last_at < EXCLUDED.last_at
"""


def sample(db: Database) -> int:
    """Worker: započte čerstvou inventuru (CPU, paměť) do hodinového průměru – každou inventuru jednou."""
    with operator_scope(db) as tx:
        return tx.execute(SAMPLE_SQL)


def _mbit(v: float) -> str:
    return f"{v / 1e6:.0f} Mb/s" if v >= 10e6 else f"{v / 1e6:.1f} Mb/s"


def anomalies(tx: Tx) -> list[dict]:
    """Odchylky poslední dokončené hodiny od stejné hodiny za posledních 14 dní.
    Vrací [{tenant_id, device_id, metric, value, usual, message}] (bez jména zařízení)."""
    out: list[dict] = []
    p = {"days": BASE_DAYS}
    for r in tx.fetchall(
            """WITH cur AS (SELECT * FROM device_hourly
                            WHERE hour = date_trunc('hour', now()) - interval '1 hour' AND samples >= 2)
               SELECT c.tenant_id, c.device_id, c.cpu_sum::float / c.samples AS cpu, c.mem_sum::float / c.samples AS mem,
                      count(h.*) AS n, avg(h.cpu_sum::float / h.samples) AS cpu_avg,
                      stddev_pop(h.cpu_sum::float / h.samples) AS cpu_sd, avg(h.mem_sum::float / h.samples) AS mem_avg,
                      stddev_pop(h.mem_sum::float / h.samples) AS mem_sd
               FROM cur c JOIN device_hourly h ON h.device_id = c.device_id AND h.samples > 0 AND h.hour < c.hour
                    AND h.hour >= c.hour - make_interval(days => %(days)s)
                    AND extract(hour FROM h.hour) = extract(hour FROM c.hour)
               GROUP BY c.tenant_id, c.device_id, c.cpu_sum, c.mem_sum, c.samples""", p):
        if r["n"] < MIN_DAYS:
            continue
        for metric, val, avg, sd, step, floor, label in (
                ("cpu", r["cpu"], r["cpu_avg"], r["cpu_sd"], CPU_STEP, CPU_FLOOR, "vytížení CPU"),
                ("memory", r["mem"], r["mem_avg"], r["mem_sd"], MEM_STEP, MEM_FLOOR, "obsazená paměť")):
            if val >= floor and val >= avg + max(SIGMA * (sd or 0), step):
                out.append({"tenant_id": r["tenant_id"], "device_id": r["device_id"], "metric": metric, "value": val,
                            "usual": avg, "message": f"{label} {val:.0f} % – v tuto hodinu bývá kolem {avg:.0f} %"})
    for r in tx.fetchall(
            """WITH per AS (SELECT tenant_id, device_id, hour, max(coalesce(rx_bps_avg, 0) + coalesce(tx_bps_avg, 0)) AS bps
                            FROM interface_hourly
                            WHERE hour >= date_trunc('hour', now()) - interval '1 hour' - make_interval(days => %(days)s)
                              AND hour < date_trunc('hour', now()) AND samples >= 2
                            GROUP BY tenant_id, device_id, hour),
                    cur AS (SELECT * FROM per WHERE hour = date_trunc('hour', now()) - interval '1 hour')
               SELECT c.tenant_id, c.device_id, c.bps::float AS bps, count(h.*) AS n, avg(h.bps)::float AS avg,
                      stddev_pop(h.bps)::float AS sd
               FROM cur c JOIN per h ON h.device_id = c.device_id AND h.hour < c.hour
                    AND extract(hour FROM h.hour) = extract(hour FROM c.hour)
               GROUP BY c.tenant_id, c.device_id, c.bps""", p):
        if r["n"] < MIN_DAYS:
            continue
        val, avg, sd = r["bps"], r["avg"] or 0.0, r["sd"] or 0.0
        if val >= TRAFFIC_FLOOR and val >= max(avg * TRAFFIC_FACTOR, avg + SIGMA * sd):
            out.append({"tenant_id": r["tenant_id"], "device_id": r["device_id"], "metric": "traffic", "value": val, "usual": avg,
                        "message": f"provoz {_mbit(val)} – v tuto hodinu bývá kolem {_mbit(avg)}"})
        elif avg >= QUIET_BASE and val <= avg / QUIET_FACTOR and val <= avg - SIGMA * sd:
            out.append({"tenant_id": r["tenant_id"], "device_id": r["device_id"], "metric": "traffic_low", "value": val,
                        "usual": avg, "message": f"provoz jen {_mbit(val)} – v tuto hodinu bývá kolem {_mbit(avg)}"})
    return out


# --- dostupnost ----------------------------------------------------------------------------------------

def availability(tx: Tx, site_id: Any, days: int = 30, now: dt.datetime | None = None) -> dict:
    """Dostupnost zařízení lokality za posledních `days` dní z alertů `offline`."""
    days = max(1, min(int(days), 90))
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    day0 = (now - dt.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    devs = tx.fetchall(
        """SELECT id, identity, serial, model, role::text AS role, state::text AS state, adopted_at
           FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline') ORDER BY lower(coalesce(identity, serial))""",
        {"s": str(site_id)})
    spans: dict[str, list[tuple[dt.datetime, dt.datetime, bool]]] = {}
    for r in tx.fetchall(
            """SELECT a.device_id, a.opened_at, a.resolved_at FROM alert a JOIN device d ON d.id = a.device_id
               WHERE d.site_id = %(s)s AND a.kind = 'offline' AND a.opened_at < %(b)s
                 AND (a.resolved_at IS NULL OR a.resolved_at > %(a)s) ORDER BY a.opened_at""",
            {"s": str(site_id), "a": since, "b": now}):
        spans.setdefault(str(r["device_id"]), []).append((r["opened_at"], r["resolved_at"] or now, r["resolved_at"] is None))
    out = []
    for d in devs:
        start = max(since, d["adopted_at"]) if d["adopted_at"] else since
        total = max(0.0, (now - start).total_seconds())
        outs = []
        for a, b, open_ in spans.get(str(d["id"]), []):
            a, b = max(a, start), min(b, now)
            if b > a:
                outs.append({"from": a.isoformat(), "to": None if open_ else b.isoformat(), "seconds": int((b - a).total_seconds()),
                             "_a": a, "_b": b})
        down = sum(o["seconds"] for o in outs)
        daily: list[float | None] = []
        for i in range(days):
            d0 = day0 + dt.timedelta(days=i)
            a, b = max(d0, start), min(d0 + dt.timedelta(days=1), now)
            if b <= a:
                daily.append(None)          # před adopcí zařízení (nebo budoucnost)
                continue
            off = sum(max(0.0, (min(o["_b"], b) - max(o["_a"], a)).total_seconds()) for o in outs)
            daily.append(round(100 * (1 - off / (b - a).total_seconds()), 2))
        out.append({
            "id": str(d["id"]), "name": d["identity"] or d["serial"], "model": d["model"], "role": d["role"], "state": d["state"],
            "since": start.isoformat(), "pct": round(100 * (1 - down / total), 3) if total else None,
            "down_seconds": down, "outages": len(outs), "longest_seconds": max((o["seconds"] for o in outs), default=0),
            "daily": daily,
            "last": [{k: v for k, v in o.items() if not k.startswith("_")} for o in reversed(outs[-10:])],
        })
    pcts = [x["pct"] for x in out if x["pct"] is not None]
    return {"days": days, "since": since.isoformat(), "until": now.isoformat(), "first_day": day0.date().isoformat(),
            "avg_pct": round(sum(pcts) / len(pcts), 3) if pcts else None, "devices": out}
