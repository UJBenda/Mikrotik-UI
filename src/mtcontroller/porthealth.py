"""Zdraví ethernetových portů: chybové čítače podle druhu, chybovost vůči provozu, optický výkon SFP.

RouterOS vede čítače chyb od startu (jen rostou). Při každé inventuře se proto spočítá rozdíl proti
předchozímu stavu a přičte se do hodinového řádku `port_error_hourly`. Hodnotí se **poměr chyb k rámcům**,
ne absolutní číslo – vytížený uplink s pár chybami nevadí, tichý port, kde je vadný každý stý rámec, ano.

Podrobné čítače dává `/interface/ethernet` (sekce `ethernet_stats`); které z nich čip opravdu vede, se liší,
chybějící se prostě nepočítají. Když podrobnosti nejsou, zůstává součet rx-error + tx-error z `/interface`.
"""

from __future__ import annotations

import re
from typing import Any

from .db import Tx

PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp)")

# druh chyby → čítače RouterOS, které do něj patří
KINDS: dict[str, tuple[str, ...]] = {
    "fcs": ("rx-fcs-error", "tx-fcs-error", "rx-code-error"),
    "align": ("rx-align-error",),
    "fragment": ("rx-fragment",),
    "jabber": ("rx-jabber",),
    "size": ("rx-too-short", "rx-too-long", "rx-length-error"),
    "overflow": ("rx-overflow", "tx-underrun"),
    "late_collision": ("tx-late-collision", "tx-excessive-collision"),
    "other": ("rx-carrier-error", "tx-carrier-sense-error", "rx-error-events"),
    "collision": ("tx-collision",),
    "pause": ("rx-pause", "tx-pause"),
}
ERROR_KINDS = ("fcs", "align", "fragment", "jabber", "size", "overflow", "late_collision", "other")
COLS = ("rx_packets", "tx_packets", "generic", *KINDS, "drops")
STATS_PROPS = "name," + ",".join(c for v in KINDS.values() for c in v)

# co druh chyby nejčastěji znamená (pro text alertu)
HINTS = {
    "fcs": "kabel, konektor nebo rušení", "align": "kabel nebo neshoda duplexu", "fragment": "kabel, rušení nebo kolize",
    "jabber": "vadná síťová karta protistrany", "size": "neshoda MTU", "overflow": "port nestíhá – přetížení, flow control",
    "late_collision": "neshoda duplexu nebo příliš dlouhý kabel", "other": "linka (nosná)", "generic": "kabel, duplex, SFP?",
}
LABELS = {"fcs": "FCS", "align": "zarovnání", "fragment": "fragmenty", "jabber": "jabber", "size": "délka rámce",
          "overflow": "přetečení", "late_collision": "pozdní kolize", "other": "nosná", "generic": "chyby"}

WARN_MIN, WARN_RATIO = 20, 1e-4        # aspoň 20 chyb za hodinu a aspoň 0,01 % rámců
CRIT_MIN, CRIT_RATIO = 100, 1e-2       # aspoň 100 chyb a aspoň 1 % rámců
BLIND_MIN = 100                        # když počty rámců nejsou: absolutní práh
STALE_SECONDS = 2 * 3600               # starší předchozí stav = rozdíl by patřil jiné době
SFP_DROP_DB, SFP_MIN_HOURS, SFP_DAYS = 3.0, 24, 7
FLAP_DOWNS = 5


def _int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        f = float(str(v))
        return f if -60 < f < 30 else None
    except (TypeError, ValueError):
        return None


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def counters(inv: dict) -> dict[str, dict[str, int]]:
    """{port: {sloupec: stav čítače}} – jen čítače, které zařízení opravdu vrátilo."""
    out: dict[str, dict[str, int]] = {}
    for i in _rows(inv, "interfaces"):
        name = str(i.get("name") or "")
        if i.get("type") != "ether" and not PHYS_RE.match(name):
            continue
        c = out.setdefault(name, {})
        for col, keys in (("rx_packets", ("rx-packet",)), ("tx_packets", ("tx-packet",)),
                          ("generic", ("rx-error", "tx-error")), ("drops", ("rx-drop", "tx-drop"))):
            vals = [x for x in (_int(i.get(k)) for k in keys) if x is not None]
            if vals:
                c[col] = sum(vals)
    for e in _rows(inv, "ethernet_stats"):
        name = str(e.get("name") or "")
        if not name:
            continue
        c = out.setdefault(name, {})
        for col, keys in KINDS.items():
            vals = [x for x in (_int(e.get(k)) for k in keys) if x is not None]
            if vals:
                c[col] = sum(vals)
    return {k: v for k, v in out.items() if k and v}


def deltas(prev: dict[str, dict[str, int]], cur: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Přírůstky čítačů. Pokles kteréhokoli čítače portu = restart/vynulování → port se v tomto kroku přeskočí."""
    out = {}
    for port, c in cur.items():
        p = prev.get(port)
        if not p or any(c[k] < p[k] for k in c if k in p):
            continue
        d = {k: c[k] - p[k] for k in c if k in p and c[k] > p[k]}
        if d:
            out[port] = d
    return out


def record(tx: Tx, tenant_id: Any, device_id: Any, data: dict[str, Any]) -> int:
    """Před přepsáním inventury: přičte přírůstky čítačů a poslední výkon SFP do aktuální hodiny."""
    if "interfaces" not in data and "ethernet_stats" not in data:
        return 0
    rows = tx.fetchall(
        """SELECT section, data, extract(epoch FROM now() - collected_at) AS age FROM device_inventory
           WHERE device_id = %(d)s AND section IN ('interfaces', 'ethernet_stats')""", {"d": str(device_id)})
    prev = counters({r["section"]: r["data"] for r in rows if (r["age"] or 0) < STALE_SECONDS})
    add = deltas(prev, counters(data))
    sfp: dict[str, tuple[float | None, float | None]] = {}
    for m in _rows(data, "ethernet_monitor"):
        rx, txp = _float(m.get("sfp-rx-power")), _float(m.get("sfp-tx-power"))
        if m.get("name") and m.get("status") == "link-ok" and (rx is not None or txp is not None):
            sfp[str(m["name"])] = (rx, txp)
    sets = ", ".join(f"{c} = port_error_hourly.{c} + EXCLUDED.{c}" for c in COLS)
    n = 0
    for port in set(add) | set(sfp):
        d = add.get(port, {})
        rx, txp = sfp.get(port, (None, None))
        tx.execute(
            f"""INSERT INTO port_error_hourly (tenant_id, device_id, name, hour, {", ".join(COLS)}, sfp_rx, sfp_tx)
                VALUES (%(t)s, %(d)s, %(n)s, date_trunc('hour', now()), {", ".join(f"%({c})s" for c in COLS)},
                        %(sfp_rx)s, %(sfp_tx)s)
                ON CONFLICT (device_id, name, hour) DO UPDATE SET {sets},
                    sfp_rx = coalesce(EXCLUDED.sfp_rx, port_error_hourly.sfp_rx),
                    sfp_tx = coalesce(EXCLUDED.sfp_tx, port_error_hourly.sfp_tx)""",
            {"t": str(tenant_id), "d": str(device_id), "n": port[:100], "sfp_rx": rx, "sfp_tx": txp,
             **{c: d.get(c, 0) for c in COLS}})
        n += 1
    return n


def evaluate(row: dict) -> dict:
    """Z hodinového součtu: {errors, packets, ratio, kinds, top, level}. level: ok | warning | critical."""
    kinds = {k: int(row.get(k) or 0) for k in ERROR_KINDS if int(row.get(k) or 0) > 0}
    detailed = sum(kinds.values())
    generic = int(row.get("generic") or 0)
    if generic > detailed:
        # souhrnný čítač vidí víc než podrobné (nebo podrobné nejsou vůbec)
        kinds["generic"] = generic - detailed
    errors = max(detailed, generic)
    packets = int(row.get("rx_packets") or 0) + int(row.get("tx_packets") or 0)
    ratio = errors / packets if packets > 0 and errors else None
    level = "ok"
    if errors:
        if ratio is None:
            level = "warning" if errors >= BLIND_MIN else "ok"
        elif errors >= CRIT_MIN and ratio >= CRIT_RATIO:
            level = "critical"
        elif errors >= WARN_MIN and ratio >= WARN_RATIO:
            level = "warning"
    return {"errors": errors, "packets": packets, "ratio": ratio, "kinds": kinds,
            "top": max(kinds, key=kinds.get) if kinds else None, "level": level}


_SUMS = ", ".join(f"sum({c}) AS {c}" for c in COLS)


def _pct(ratio: float | None) -> str:
    if ratio is None:
        return ""
    p = ratio * 100
    return f" ({p:.0f} % rámců)" if p >= 10 else f" ({p:.1f} % rámců)" if p >= 1 else f" ({p:.2f} % rámců)".replace(".", ",")


def message(port: str, ev: dict) -> str:
    top = ev["top"] or "generic"
    what = f"hlavně {LABELS[top]} – " if top != "generic" else ""
    return f"port {port} – {ev['errors']} chyb za poslední hodinu{_pct(ev['ratio'])}, {what}{HINTS[top]}"


def alerts(tx: Tx) -> list[dict]:
    """Porty s chybovostí nad prahem za poslední hodinu a SFP s poklesem výkonu proti vlastnímu normálu.
    Vrací [{tenant_id, device_id, kind, port, severity, message}] (bez jména zařízení)."""
    out = []
    for r in tx.fetchall(
            f"""SELECT h.tenant_id, h.device_id, h.name, {_SUMS} FROM port_error_hourly h
                JOIN device d ON d.id = h.device_id AND d.state = 'adopted'
                WHERE h.hour > now() - interval '1 hour' GROUP BY h.tenant_id, h.device_id, h.name"""):
        ev = evaluate(r)
        if ev["level"] != "ok":
            out.append({"tenant_id": r["tenant_id"], "device_id": r["device_id"], "kind": "port_errors", "port": r["name"],
                        "severity": ev["level"], "message": message(r["name"], ev)})
    for r in sfp_drops(tx):
        out.append({"tenant_id": r["tenant_id"], "device_id": r["device_id"], "kind": "sfp_power", "port": r["name"],
                    "severity": "warning",
                    "message": f"port {r['name']} – přijímaný optický výkon {r['now']:.1f} dBm, obvykle {r['usual']:.1f} dBm "
                               "(špinavý konektor, ohyb vlákna, stárnoucí modul?)"})
    return out


def sfp_drops(tx: Tx, site_id: Any = None) -> list[dict]:
    """SFP, jejichž poslední přijímaný výkon je aspoň o 3 dB pod průměrem předchozích 7 dní."""
    return [r for r in tx.fetchall(
        """WITH cur AS (SELECT DISTINCT ON (h.device_id, h.name) h.tenant_id, h.device_id, h.name, h.hour, h.sfp_rx
                        FROM port_error_hourly h JOIN device d ON d.id = h.device_id AND d.state = 'adopted'
                        WHERE h.sfp_rx IS NOT NULL AND h.hour > now() - interval '2 hours'
                          AND (%(s)s::uuid IS NULL OR d.site_id = %(s)s::uuid)
                        ORDER BY h.device_id, h.name, h.hour DESC)
           SELECT c.tenant_id, c.device_id, c.name, c.sfp_rx::float AS now, avg(p.sfp_rx)::float AS usual, count(*) AS n
           FROM cur c JOIN port_error_hourly p ON p.device_id = c.device_id AND p.name = c.name AND p.sfp_rx IS NOT NULL
                AND p.hour < c.hour - interval '2 hours' AND p.hour >= c.hour - make_interval(days => %(days)s)
           GROUP BY c.tenant_id, c.device_id, c.name, c.sfp_rx""",
        {"s": str(site_id) if site_id else None, "days": SFP_DAYS})
        if r["n"] >= SFP_MIN_HOURS and r["now"] <= r["usual"] - SFP_DROP_DB]


def site_health(tx: Tx, site_id: Any) -> dict[tuple[str, str], dict]:
    """{(device_id, port): zdraví} pro sekci Porty: chyby za hodinu a 24 h, výpadky linky, SFP.
    `why` jsou kódy důvodů (texty skládá rozhraní): errors, flap, sfp."""
    out: dict[tuple[str, str], dict] = {}

    def get(r: dict) -> dict:
        return out.setdefault((str(r["device_id"]), str(r["name"])), {
            "level": "ok", "why": [], "errors_1h": 0, "errors_24h": 0, "ratio": None, "top": None, "kinds": {},
            "drops": 0, "pause": 0, "collision": 0, "link_downs": 0, "sfp_rx": None, "sfp_usual": None})

    p = {"s": str(site_id)}
    for win, hours in (("24h", 24), ("1h", 1)):
        for r in tx.fetchall(
                f"""SELECT h.device_id, h.name, {_SUMS} FROM port_error_hourly h JOIN device d ON d.id = h.device_id
                    WHERE d.site_id = %(s)s AND h.hour > now() - make_interval(hours => %(h)s)
                    GROUP BY h.device_id, h.name""", dict(p, h=hours)):
            ev, h = evaluate(r), get(r)
            if win == "24h":
                h.update(errors_24h=ev["errors"], kinds=ev["kinds"], top=ev["top"], drops=int(r["drops"] or 0),
                         pause=int(r["pause"] or 0), collision=int(r["collision"] or 0))
                if ev["errors"]:
                    h["level"] = "note"
            else:
                h.update(errors_1h=ev["errors"], ratio=ev["ratio"])
                if ev["level"] != "ok":
                    h["level"] = ev["level"]
                    h["top"] = ev["top"]
                    h["why"].append("errors")
    for r in tx.fetchall(
            """SELECT h.device_id, h.name, sum(h.link_downs) AS downs FROM interface_hourly h
               JOIN device d ON d.id = h.device_id
               WHERE d.site_id = %(s)s AND h.hour > now() - interval '24 hours'
               GROUP BY h.device_id, h.name HAVING sum(h.link_downs) > 0""", p):
        if not PHYS_RE.match(str(r["name"])):
            continue
        h = get(r)
        h["link_downs"] = int(r["downs"])
        if h["link_downs"] >= FLAP_DOWNS:
            h["why"].append("flap")
            if h["level"] in ("ok", "note"):
                h["level"] = "warning"
        elif h["level"] == "ok":
            h["level"] = "note"
    for r in sfp_drops(tx, site_id):
        h = get(r)
        h.update(sfp_rx=round(r["now"], 1), sfp_usual=round(r["usual"], 1))
        h["why"].append("sfp")
        if h["level"] in ("ok", "note"):
            h["level"] = "warning"
    return out


def history(tx: Tx, device_id: Any, port: str, hours: int = 48) -> list[dict]:
    """Hodinová řada chyb jednoho portu (pro graf v sekci Porty)."""
    rows = tx.fetchall(
        f"""SELECT hour, {", ".join(COLS)}, sfp_rx FROM port_error_hourly
            WHERE device_id = %(d)s AND name = %(n)s AND hour > now() - make_interval(hours => %(h)s) ORDER BY hour""",
        {"d": str(device_id), "n": port, "h": hours})
    out = []
    for r in rows:
        ev = evaluate(r)
        out.append({"t": r["hour"], "errors": ev["errors"], "packets": ev["packets"], "kinds": ev["kinds"],
                    "drops": int(r["drops"] or 0), "level": ev["level"], "sfp_rx": r["sfp_rx"]})
    return out
