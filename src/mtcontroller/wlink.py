"""Bezdrátové spoje mezi spravovanými zařízeními: nalezení, měření kvality, hodnocení a odchylky.

**Co je spoj:** registrace v tabulce bezdrátových klientů, jejíž protistrana (MAC) je bezdrátové rozhraní
jiného spravovaného zařízení téže lokality. Strana A je AP/bridge, strana B klient (station).

**Měření (worker, ~po 5 minutách):** z obou stran čerstvý řádek registrace (signál, vyjednaná rychlost,
SNR a CCQ u starého ovladače), šum z monitoru rozhraní, a z A krátký ping na adresu B – ztrátovost a odezva.
Ping je jediný údaj, který řekne, že spoj zahazuje, i když signál vypadá dobře.

**Hodnocení:** proti vlastnímu normálu spoje (medián za 7 dní) – propad signálu, pád rychlosti, ztrátovost,
nesouměrnost stran, časté přepojování. Prahy jsou odhad a na skutečném zařízení zatím neověřené.
U 60 GHz (ovladač w60g) se místo registrace čte monitor rozhraní: RSSI, rychlost PHY a chybovost přenosu
(uložená jako „kvalita“ = 100 − chybovost do sloupců ccq).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from typing import Any

from .db import Database, Tx, operator_scope, tenant_scope

log = logging.getLogger("mtc.wlink")

SAMPLE_EVERY_MIN = 5
DISCOVER_EVERY_S = 600
PING_COUNT = 10
SAMPLE_DAYS = 45                  # jak dlouho se drží měření
USUAL_DAYS, USUAL_MIN = 7, 36     # normál = medián za týden, aspoň ~3 hodiny měření
SIGNAL_DROP_DB = 6                # propad signálu proti normálu
RATE_DROP = 0.5                   # rychlost spoje pod polovinou normálu
LOSS_WARN, LOSS_CRIT = 2.0, 10.0  # % ztracených pingů
ASYM_DB = 10                      # rozdíl signálu stran
WEAK_DBM = -78                    # slabý signál i bez známého normálu
FLAPS_24H = 3
NOISE_RISE_DB = 6
REG = {"wifi": "/interface/wifi/registration-table", "wireless": "/interface/wireless/registration-table"}
MONITOR = {"wifi": "/interface/wifi/monitor", "wireless": "/interface/wireless/monitor"}
SECTIONS = ["interfaces", "wifi", "wireless", "wgig", "wgig_monitor", "wifi_clients", "wireless_clients", "addresses"]
# co navíc potřebuje strom topologie (infra.site_infra) pro hledání cizích úseků
FOREIGN_SECTIONS = ["neighbors", "ethernet_monitor", "stp_bridges", "stp_ports", "bridges", "bridge_ports", "routes_default",
                    "dhcp_client", "bridge_hosts"]
FOREIGN_FRESH_H = 2               # cizí úsek se měří, jen dokud ho topologie pořád potvrzuje
_UNITS = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}
_last_discover = 0.0


# --- čtení hodnot RouterOS -----------------------------------------------------------------------------------

def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _mac(v: Any) -> str:
    return str(v or "").strip().upper()


def _num(v: Any) -> int | None:
    """První celé číslo v hodnotě: '-58', '-58dBm@6Mbps', '87%' → -58, -58, 87."""
    m = re.search(r"-?\d+", str(v if v is not None else ""))
    return int(m.group(0)) if m else None


def parse_rate(v: Any) -> int | None:
    """Vyjednaná rychlost v bit/s: '433300000', '433.3Mbps', '130Mbps-20MHz/2S' → bit/s."""
    m = re.match(r"\s*([\d.]+)\s*([kKmMgG]?)(?:bps|b/s)?", str(v if v is not None else ""))
    if not m or not m.group(1).strip("."):
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def parse_duration(v: Any) -> int | None:
    """Doba RouterOS na sekundy: '1d2h3m4s', '00:11:42', '11h42m' → s."""
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?\s*(\d+):(\d+):(\d+)(?:\.\d+)?", s)
    if m:
        w, d, h, mi, se = (int(x or 0) for x in m.groups())
        return w * 604800 + d * 86400 + h * 3600 + mi * 60 + se
    parts = re.findall(r"(\d+)(w|d|h|m|s)(?![a-z])", s)
    if not parts:
        return None
    return sum(int(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


def side(row: dict | None) -> dict:
    """Řádek registrace (wifi i wireless) → {signal, snr, rate_tx, rate_rx, ccq, uptime_s}."""
    if not row:
        return {}
    sig = _num(row.get("signal")) if row.get("signal") not in (None, "") else _num(row.get("signal-strength"))
    return {"signal": sig if sig is not None and -110 <= sig <= 0 else None,
            "snr": _num(row.get("signal-to-noise")), "ccq": _num(row.get("tx-ccq")),
            "rate_tx": parse_rate(row.get("tx-rate")), "rate_rx": parse_rate(row.get("rx-rate")),
            "uptime_s": parse_duration(row.get("uptime"))}


def ping_summary(rows: Any) -> dict:
    """Výstup pingu (řádek na paket, souhrn v posledním) → {loss, rtt_avg, rtt_min, rtt_max}."""
    from .porttools import _res_ping

    r = _res_ping(rows)
    if not r.get("sent"):
        return {}
    return {"loss": round(100.0 * (r["sent"] - r["received"]) / r["sent"], 1), "rtt_avg": r["avg_ms"],
            "rtt_min": r["min_ms"], "rtt_max": r["max_ms"]}


# --- nalezení spojů --------------------------------------------------------------------------------------------

def _radio_macs(inv: dict) -> dict[str, tuple[str, str, bool]]:
    """{MAC: (rozhraní, ovladač, je_AP)} bezdrátových rozhraní zařízení."""
    out: dict[str, tuple[str, str, bool]] = {}
    for kind, sec in (("wifi", "wifi"), ("wireless", "wireless"), ("w60g", "wgig")):
        for w in _rows(inv, sec):
            mac, name = _mac(w.get("mac-address")), str(w.get("name") or "")
            mode = str(w.get("configuration.mode") or w.get("mode") or "")
            if mac and name:
                out[mac] = (name, kind, not mode.startswith("station"))
    for i in _rows(inv, "interfaces"):
        mac = _mac(i.get("mac-address"))
        if mac and mac not in out and i.get("type") in ("wlan", "wifi"):
            out[mac] = (str(i.get("name") or ""), "wifi" if i.get("type") == "wifi" else "wireless", True)
    return out


def _ping_ip(inv_a: dict, inv_b: dict) -> str | None:
    """Adresa B ve stejné síti, jakou má A (ping tak jde přes spoj). Tunel controlleru se nepočítá."""
    def nets(inv: dict) -> list:
        out = []
        for a in _rows(inv, "addresses"):
            if str(a.get("disabled")).lower() in ("true", "yes") or str(a.get("interface") or "").startswith("mtc"):
                continue
            try:
                i = ipaddress.ip_interface(str(a.get("address")))
            except ValueError:
                continue
            if i.version == 4 and not i.ip.is_loopback and not i.ip.is_link_local:
                out.append(i)
        return out

    mine = nets(inv_a)
    for b in nets(inv_b):
        if any(b.ip in a.network and b.ip != a.ip for a in mine):
            return str(b.ip)
    return None


def find_links(devices: list[dict], invs: dict[str, dict]) -> list[dict]:
    """Spoje mezi zařízeními jedné lokality. devices: [{id, …}], invs: {id: inventura}."""
    owner: dict[str, tuple[str, str, str, bool]] = {}
    for d in devices:
        for mac, (iface, kind, ap) in _radio_macs(invs.get(str(d["id"])) or {}).items():
            owner[mac] = (str(d["id"]), iface, kind, ap)
    found: dict[frozenset, dict] = {}
    for d in devices:
        me, inv = str(d["id"]), invs.get(str(d["id"])) or {}
        mine = _radio_macs(inv)
        by_iface = {v[0]: (mac, v[1], v[2]) for mac, v in mine.items()}
        # 60 GHz: protistranu hlásí monitor rozhraní (remote-address), ne registrační tabulka
        w60 = [{"mac-address": m.get("remote-address"), "interface": m.get("name")} for m in _rows(inv, "wgig_monitor")
               if str(m.get("connected")).lower() in ("true", "yes")]
        for kind, regs in (("wifi", _rows(inv, "wifi_clients")), ("wireless", _rows(inv, "wireless_clients")), ("w60g", w60)):
            for r in regs:
                peer = owner.get(_mac(r.get("mac-address")))
                iface = str(r.get("interface") or "")
                if not peer or peer[0] == me or iface not in by_iface:
                    continue
                my_mac, my_kind, my_ap = by_iface[iface]
                key = frozenset(((me, iface), (peer[0], peer[1])))
                if key in found:
                    continue
                a = {"device": me, "iface": iface, "mac": my_mac, "kind": my_kind, "ap": my_ap}
                b = {"device": peer[0], "iface": peer[1], "mac": _mac(r.get("mac-address")), "kind": peer[2], "ap": peer[3]}
                if b["ap"] and not a["ap"]:
                    a, b = b, a
                found[key] = {"kind": a["kind"], "a": a, "b": b,
                              "ping_ip": _ping_ip(invs.get(a["device"]) or {}, invs.get(b["device"]) or {})}
    return list(found.values())


def find_foreign(devices: list[dict], invs: dict[str, dict], shared: set[tuple[str, str]],
                 wireless: set[tuple[str, str]]) -> list[dict]:
    """Cizí úseky: dvě spravovaná zařízení, mezi kterými je něco nespravovaného (switch, cizí bezdrátový spoj).
    Pozná se to tak, že na portu rodiče směrem k potomkovi visí i klienti (`shared` z evidence nespravovaných
    portů), že za jedním portem je víc spravovaných zařízení, nebo že se zařízení vidí jen přes společnou síť.
    Signál cizího rádia neznáme – měří se jen ping přes úsek."""
    from . import infra

    nodes = infra.site_infra(devices, invs).get("nodes") or []
    by_id = {n["id"]: n for n in nodes}
    real = lambda n: n is not None and n.get("kind") != "hub" and n["id"] in invs   # noqa: E731
    groups: dict[tuple[str, str], list[dict]] = {}
    sure: set[tuple[str, str]] = set()
    for n in nodes:
        if not real(n):
            continue
        par = by_id.get(n.get("parent") or "")
        port = n.get("parent_port")
        if par is not None and par.get("kind") == "hub":   # společná síť: úsek vede od zařízení nad ní
            par, port = by_id.get(par.get("parent") or ""), par.get("parent_port")
            if real(par) and par["id"] != n["id"]:
                sure.add((par["id"], str(port or "?")))
        if real(par) and par["id"] != n["id"]:
            groups.setdefault((par["id"], str(port or "?")), []).append(n)
    out = []
    for (a, p), kids in groups.items():
        if (a, p) in wireless or not ((a, p) in sure or (a, p) in shared or len(kids) >= 2):
            continue
        for k in kids:
            out.append({"kind": "foreign", "a": {"device": a, "iface": p, "mac": "-"},
                        "b": {"device": k["id"], "iface": str(k.get("uplink_port") or "?"), "mac": "-"},
                        "ping_ip": _ping_ip(invs.get(a) or {}, invs.get(k["id"]) or {})})
    return out


def discover(db: Database) -> int:
    """Projde inventury a založí/obnoví spoje. Zmizelý spoj se nemaže (historie a alert „spoj je dole“)."""
    from . import inventory

    n = 0
    with operator_scope(db) as tx:
        sites = [str(r["site_id"]) for r in tx.fetchall(
            """SELECT site_id FROM device WHERE state IN ('adopted', 'offline') AND site_id IS NOT NULL
               GROUP BY site_id HAVING count(*) >= 2""")]
        for sid in sites:
            members = tx.fetchall("""SELECT id, tenant_id, serial, identity, model, role::text AS role, state::text AS state,
                                            ros_version
                                     FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""", {"s": sid})
            if len(members) < 2:
                continue
            invs = {str(m["id"]): inventory.load(tx, m["id"], SECTIONS + FOREIGN_SECTIONS) for m in members}
            links = find_links(members, invs)
            wireless = {(x["device"], x["iface"]) for ln in links for x in (ln["a"], ln["b"])}
            shared = {(str(r["device_id"]), r["port"]) for r in tx.fetchall(
                """SELECT device_id, port FROM unmanaged_port WHERE site_id = %(s)s AND shared
                   AND last_seen > now() - interval '2 hours'""", {"s": sid})}
            try:
                links += find_foreign(members, invs, shared, wireless)
            except Exception:   # topologie je odhad – chyba v ní nesmí zastavit bezdrátové spoje
                log.exception("lokalita %s: hledání cizích úseků selhalo", sid)
            for ln in links:
                tx.execute(
                    """INSERT INTO wlink (tenant_id, site_id, kind, a_device, a_iface, a_mac, b_device, b_iface, b_mac, ping_ip)
                       VALUES (%(t)s, %(s)s, %(k)s, %(ad)s, %(ai)s, %(am)s, %(bd)s, %(bi)s, %(bm)s, %(ip)s)
                       ON CONFLICT (a_device, a_iface, b_device, b_iface) DO UPDATE SET
                           last_seen = now(), site_id = EXCLUDED.site_id, kind = EXCLUDED.kind, a_mac = EXCLUDED.a_mac,
                           b_mac = EXCLUDED.b_mac, ping_ip = coalesce(EXCLUDED.ping_ip, wlink.ping_ip)""",
                    {"t": str(members[0]["tenant_id"]), "s": sid, "k": ln["kind"], "ad": ln["a"]["device"],
                     "ai": ln["a"]["iface"][:100], "am": ln["a"]["mac"][:17], "bd": ln["b"]["device"],
                     "bi": ln["b"]["iface"][:100], "bm": ln["b"]["mac"][:17], "ip": ln["ping_ip"]})
                n += 1
    return n


# --- měření ----------------------------------------------------------------------------------------------------

def _reg_row(api: Any, kind: str, iface: str, peer_mac: str) -> dict | None:
    rows = api.request("GET", REG.get(kind, REG["wifi"]))
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and _mac(r.get("mac-address")) == peer_mac and str(r.get("interface") or "") == iface:
            return r
    return None


def _monitor(api: Any, path: str, iface: str) -> dict:
    mon = api.request("POST", path, {"numbers": iface, "once": ""})
    return mon[0] if isinstance(mon, list) and mon and isinstance(mon[0], dict) else {}


def side_w60g(m: dict | None, peer_mac: str) -> dict | None:
    """Monitor 60GHz rozhraní → strana spoje; None = není spojeno s touto protistranou."""
    if not m or str(m.get("connected")).lower() not in ("true", "yes") or _mac(m.get("remote-address")) != peer_mac:
        return None
    rssi, per = _num(m.get("rssi")), _num(m.get("tx-packet-error-rate"))
    return {"signal": rssi if rssi is not None and -110 <= rssi <= 0 else None, "snr": None,
            "ccq": max(0, 100 - per) if per is not None else None, "rate_tx": parse_rate(m.get("tx-phy-rate")),
            "rate_rx": None, "uptime_s": None}


def _side_now(api: Any, kind: str, iface: str, peer_mac: str) -> dict | None:
    """Čerstvý stav jedné strany; None = protistrana není připojená."""
    if kind == "w60g":
        return side_w60g(_monitor(api, "/interface/w60g/monitor", iface), peer_mac)
    row = _reg_row(api, kind, iface, peer_mac)
    return side(row) if row is not None else None


def measure(api_a: Any, api_b: Any, link: dict) -> dict | None:
    """Jedno měření spoje. None = strana A nedostupná (to řeší alert offline, ne spoj)."""
    from .rosapi import RosApiError

    kind = link["kind"]
    if kind == "foreign":
        # cizí úsek: rádia nevidíme, zbývá ping přes něj. Bez adresy ve společné síti není co měřit.
        if not link.get("ping_ip"):
            return None
        out = {c: None for c in _SAMPLE_COLS}
        try:
            out.update(ping_summary(api_a.request(
                "POST", "/ping", {"address": str(link["ping_ip"]), "count": str(PING_COUNT), "interval": "200ms"})))
        except RosApiError:
            return None
        if out["loss"] is None:
            return None
        out["up"] = out["loss"] < 100
        return out
    try:
        ra = _side_now(api_a, kind, link["a_iface"], link["b_mac"])
    except RosApiError:
        return None
    a = ra or {}
    b: dict = {}
    if api_b is not None:
        try:
            b = _side_now(api_b, kind, link["b_iface"], link["a_mac"]) or {}
        except RosApiError:
            b = {}
    out: dict[str, Any] = {
        "up": ra is not None, "signal_a": a.get("signal"), "signal_b": b.get("signal"), "snr_a": a.get("snr"),
        "snr_b": b.get("snr"), "rate_ab": a.get("rate_tx") or b.get("rate_rx"), "rate_ba": a.get("rate_rx") or b.get("rate_tx"),
        "ccq_a": a.get("ccq"), "ccq_b": b.get("ccq"), "uptime_s": a.get("uptime_s"), "noise": None,
        "loss": None, "rtt_avg": None, "rtt_min": None, "rtt_max": None}
    if not out["up"]:
        return out
    if kind in MONITOR:
        try:
            nf = _num(_monitor(api_a, MONITOR[kind], link["a_iface"]).get("noise-floor"))
            out["noise"] = nf if nf is not None and -120 <= nf <= -40 else None
        except RosApiError:
            pass
    if link.get("ping_ip"):
        try:
            out.update(ping_summary(api_a.request(
                "POST", "/ping", {"address": str(link["ping_ip"]), "count": str(PING_COUNT), "interval": "200ms"})))
        except RosApiError:
            pass
    return out


_SAMPLE_COLS = ("up", "signal_a", "signal_b", "snr_a", "snr_b", "rate_ab", "rate_ba", "ccq_a", "ccq_b", "noise",
                "loss", "rtt_avg", "rtt_min", "rtt_max", "uptime_s")


def store_sample(tx: Tx, link: dict, s: dict) -> None:
    prev = tx.fetchone("SELECT up, uptime_s FROM wlink_sample WHERE link_id = %(l)s ORDER BY at DESC LIMIT 1",
                       {"l": str(link["id"])})
    # přepojení: spoj byl nahoře a doba spojení klesla, nebo se vrátil po výpadku
    rec = bool(prev and s["up"] and ((prev["up"] and prev["uptime_s"] is not None and s.get("uptime_s") is not None
                                      and s["uptime_s"] < prev["uptime_s"]) or not prev["up"]))
    tx.execute(
        f"""INSERT INTO wlink_sample (tenant_id, link_id, {", ".join(_SAMPLE_COLS)}, reconnect)
            VALUES (%(t)s, %(l)s, {", ".join(f"%({c})s" for c in _SAMPLE_COLS)}, %(rec)s)
            ON CONFLICT (link_id, at) DO NOTHING""",
        {"t": str(link["tenant_id"]), "l": str(link["id"]), "rec": rec, **{c: s.get(c) for c in _SAMPLE_COLS}})
    tx.execute("UPDATE wlink SET sampled_at = now() WHERE id = %(l)s", {"l": str(link["id"])})


def sample(db: Database, box: Any, limit: int = 100, force: bool = False) -> int:
    """Krok workeru: občas znovu najde spoje a změří ty, které jsou na řadě."""
    from concurrent.futures import ThreadPoolExecutor

    from .services import device_api

    global _last_discover
    if force or time.monotonic() - _last_discover > DISCOVER_EVERY_S:
        _last_discover = time.monotonic()
        discover(db)
    with operator_scope(db) as tx:
        links = tx.fetchall(
            """SELECT l.id, l.tenant_id, l.kind, l.a_device, l.a_iface, l.a_mac, l.b_device, l.b_iface, l.b_mac,
                      host(l.ping_ip) AS ping_ip,
                      a.state AS a_state, host(a.tunnel_ip) AS a_ip, a.api_cert_fingerprint AS a_fp,
                      b.state AS b_state, host(b.tunnel_ip) AS b_ip, b.api_cert_fingerprint AS b_fp
               FROM wlink l JOIN device a ON a.id = l.a_device JOIN device b ON b.id = l.b_device
               WHERE a.state = 'adopted' AND a.tunnel_ip IS NOT NULL AND a.api_cert_fingerprint IS NOT NULL
                 AND (l.kind <> 'foreign' OR l.last_seen > now() - make_interval(hours => %(fh)s))
                 AND (%(force)s OR l.sampled_at IS NULL OR l.sampled_at < now() - make_interval(mins => %(m)s))
               ORDER BY l.sampled_at NULLS FIRST LIMIT %(n)s""", {"m": SAMPLE_EVERY_MIN, "n": limit, "force": force, "fh": FOREIGN_FRESH_H})
        jobs = []
        for ln in links:
            try:
                api_a = device_api(tx, box, {"id": ln["a_device"], "tunnel_ip": ln["a_ip"], "fp": ln["a_fp"]}, timeout=25.0)
                api_b = None
                if ln["b_state"] == "adopted" and ln["b_ip"] and ln["b_fp"]:
                    api_b = device_api(tx, box, {"id": ln["b_device"], "tunnel_ip": ln["b_ip"], "fp": ln["b_fp"]}, timeout=5.0)
                jobs.append((ln, api_a, api_b))
            except Exception:
                log.info("spoj %s: nejde sestavit REST klienta", ln["id"])

    def one(job) -> bool:
        ln, api_a, api_b = job
        try:
            s = measure(api_a, api_b, ln)
            if s is None:
                return False
            with tenant_scope(db, ln["tenant_id"]) as tx:
                store_sample(tx, ln, s)
            return True
        except Exception as e:
            log.info("měření spoje %s selhalo: %s", ln["id"], e)
            return False

    if not jobs:
        return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        return sum(pool.map(one, jobs))


def retention(tx: Tx) -> None:
    tx.execute("DELETE FROM wlink_sample WHERE at < now() - make_interval(days => %(d)s)", {"d": SAMPLE_DAYS})


# --- hodnocení -------------------------------------------------------------------------------------------------

def usual(tx: Tx, link_id: Any) -> dict:
    """Normál spoje: medián za posledních 7 dní (bez posledních 30 minut, ať se právě běžící propad nepočítá)."""
    r = tx.fetchone(
        """SELECT count(*) AS n,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY signal_a) AS signal_a,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY signal_b) AS signal_b,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY rate_ab) AS rate_ab,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY rate_ba) AS rate_ba,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY rtt_avg) AS rtt_avg,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY noise) AS noise
           FROM wlink_sample WHERE link_id = %(l)s AND up AND at > now() - make_interval(days => %(d)s)
             AND at < now() - interval '30 minutes'""", {"l": str(link_id), "d": USUAL_DAYS}) or {}
    if (r.get("n") or 0) < USUAL_MIN:
        return {"n": int(r.get("n") or 0)}
    return {k: (round(float(v), 1) if v is not None and k != "n" else v) for k, v in r.items()}


def deviations(s: dict, us: dict) -> list[dict]:
    """Čím jeden vzorek (nebo průměr několika) vybočuje. [{code, now, usual}], code: down|signal|rate|loss|asym|weak|noise."""
    if not s.get("up", True):
        return [{"code": "down"}]
    out = []
    for k in ("signal_a", "signal_b"):
        now, u = s.get(k), us.get(k)
        if now is not None and u is not None and now <= u - SIGNAL_DROP_DB:
            out.append({"code": "signal", "side": k[-1], "now": round(now), "usual": round(u)})
        elif now is not None and u is None and now <= WEAK_DBM:
            out.append({"code": "weak", "side": k[-1], "now": round(now)})
    for k in ("rate_ab", "rate_ba"):
        now, u = s.get(k), us.get(k)
        if now and u and now < u * RATE_DROP:
            out.append({"code": "rate", "side": k[-2:], "now": int(now), "usual": int(u)})
            break
    if s.get("loss") is not None and s["loss"] >= LOSS_WARN:
        out.append({"code": "loss", "now": round(s["loss"], 1)})
    if s.get("signal_a") is not None and s.get("signal_b") is not None and abs(s["signal_a"] - s["signal_b"]) >= ASYM_DB:
        out.append({"code": "asym", "now": round(abs(s["signal_a"] - s["signal_b"]))})
    if s.get("noise") is not None and us.get("noise") is not None and s["noise"] >= us["noise"] + NOISE_RISE_DB:
        out.append({"code": "noise", "now": round(s["noise"]), "usual": round(us["noise"])})
    return out


def score(s: dict, us: dict, flaps: int = 0) -> int:
    """0–100. Odhadnuté váhy: ztrátovost bolí nejvíc, pak propad signálu a rychlosti, přepojování."""
    if not s.get("up", True):
        return 0
    pts = 100.0
    if s.get("loss") is not None:
        pts -= min(40.0, s["loss"] * 5)
    drops = [us[k] - s[k] for k in ("signal_a", "signal_b") if s.get(k) is not None and us.get(k) is not None]
    if drops and max(drops) > 3:
        pts -= min(25.0, (max(drops) - 3) * 1.5)
    sig = [s[k] for k in ("signal_a", "signal_b") if s.get(k) is not None]
    if sig and min(sig) < -70:
        pts -= min(20.0, (-70 - min(sig)) * 2.0)
    ratios = [s[k] / us[k] for k in ("rate_ab", "rate_ba") if s.get(k) and us.get(k)]
    if ratios and min(ratios) < 0.9:
        pts -= min(20.0, (0.9 - min(ratios)) * 30)
    ccq = [s[k] for k in ("ccq_a", "ccq_b") if s.get(k) is not None]
    if ccq and min(ccq) < 90:
        pts -= min(15.0, (90 - min(ccq)) * 0.3)
    pts -= min(15.0, flaps * 4.0)
    return max(0, min(100, round(pts)))


def state_of(sc: int, devs: list[dict], flaps: int) -> str:
    codes = {d["code"] for d in devs}
    if "down" in codes:
        return "down"
    if any(d["code"] == "loss" and d["now"] >= LOSS_CRIT for d in devs) or sc < 40:
        return "bad"
    if codes - {"asym"} or flaps >= FLAPS_24H or sc < 85:
        return "degraded"
    return "ok"


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [float(r[key]) for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def current(tx: Tx, link_id: Any) -> dict | None:
    """Stav teď: průměr posledních 3 měření (jeden špatný ping nemá spustit alert), poslední rozhoduje o výpadku."""
    rows = tx.fetchall("""SELECT * FROM wlink_sample WHERE link_id = %(l)s AND at > now() - interval '40 minutes'
                          ORDER BY at DESC LIMIT 3""", {"l": str(link_id)})
    if not rows:
        return None
    last = rows[0]
    live = [r for r in rows if r["up"]] or rows
    now = {k: _avg(live, k) for k in ("signal_a", "signal_b", "snr_a", "snr_b", "rate_ab", "rate_ba", "ccq_a", "ccq_b",
                                      "noise", "loss", "rtt_avg", "rtt_min", "rtt_max")}
    now.update(up=bool(last["up"]), at=last["at"], uptime_s=last["uptime_s"])
    return now


def assess(tx: Tx, link_id: Any) -> dict:
    """{now, usual, why, score, state, flaps}."""
    now = current(tx, link_id)
    us = usual(tx, link_id)
    flaps = int((tx.fetchone("""SELECT count(*) AS n FROM wlink_sample WHERE link_id = %(l)s AND reconnect
                                AND at > now() - interval '24 hours'""", {"l": str(link_id)}) or {}).get("n") or 0)
    if not now:
        return {"now": None, "usual": us, "why": [], "score": None, "state": "unknown", "flaps": flaps}
    why = deviations(now, us)
    if flaps >= FLAPS_24H:
        why.append({"code": "flap", "now": flaps})
    sc = score(now, us, flaps)
    return {"now": now, "usual": us, "why": why, "score": sc, "state": state_of(sc, why, flaps), "flaps": flaps}


def _mbps(v: Any) -> str:
    return f"{float(v) / 1e6:.0f} Mb/s"


def why_text(why: list[dict]) -> str:
    out = []
    for w in why:
        c = w["code"]
        if c == "signal":
            out.append(f"signál {w['now']} dBm (obvykle {w['usual']})")
        elif c == "weak":
            out.append(f"slabý signál {w['now']} dBm")
        elif c == "rate":
            out.append(f"rychlost spoje {_mbps(w['now'])} (obvykle {_mbps(w['usual'])})")
        elif c == "loss":
            out.append(f"ztrátovost {str(w['now']).replace('.', ',')} %")
        elif c == "asym":
            out.append(f"strany se slyší o {w['now']} dB jinak")
        elif c == "noise":
            out.append(f"šum na kanálu {w['now']} dBm (obvykle {w['usual']})")
        elif c == "flap":
            out.append(f"{w['now']}× přepojení za 24 h")
    return ", ".join(out)


def alerts(tx: Tx) -> list[dict]:
    """[{tenant_id, device_id (strana A), kind link_down|link_degraded, subject, severity, message}]."""
    out = []
    for ln in tx.fetchall(
            """SELECT l.id, l.tenant_id, l.kind, l.a_device, l.a_iface, coalesce(nullif(a.identity, ''), a.serial) AS a_name,
                      coalesce(nullif(b.identity, ''), b.serial) AS b_name
               FROM wlink l JOIN device a ON a.id = l.a_device JOIN device b ON b.id = l.b_device
               WHERE NOT l.muted AND a.state = 'adopted' AND l.sampled_at > now() - interval '30 minutes'"""):
        a = assess(tx, ln["id"])
        name = f"{ln['a_name']} ↔ {ln['b_name']}"
        if ln["kind"] == "foreign":
            what = f"úsek přes nespravované zařízení {name}"
            if a["state"] == "down":
                out.append({"tenant_id": ln["tenant_id"], "device_id": ln["a_device"], "kind": "link_down",
                            "subject": ln["b_name"], "severity": "critical",
                            "message": f"{what} je dole (protistrana neodpovídá na ping)"})
            elif a["state"] in ("bad", "degraded") and a["why"]:
                out.append({"tenant_id": ln["tenant_id"], "device_id": ln["a_device"], "kind": "link_degraded",
                            "subject": ln["b_name"], "severity": "critical" if a["state"] == "bad" else "warning",
                            "message": f"{what}: {why_text(a['why'])}"})
            continue
        if a["state"] == "down":
            out.append({"tenant_id": ln["tenant_id"], "device_id": ln["a_device"], "kind": "link_down", "subject": ln["b_name"],
                        "severity": "critical", "message": f"bezdrátový spoj {name} je dole (protistrana není připojená)"})
        elif a["state"] in ("bad", "degraded") and a["why"]:
            out.append({"tenant_id": ln["tenant_id"], "device_id": ln["a_device"], "kind": "link_degraded",
                        "subject": ln["b_name"], "severity": "critical" if a["state"] == "bad" else "warning",
                        "message": f"bezdrátový spoj {name}: {why_text(a['why'])}"})
    return out


# --- čtení pro rozhraní ----------------------------------------------------------------------------------------

_LINK_SQL = """SELECT l.id, l.kind, l.a_device, l.a_iface, l.b_device, l.b_iface, host(l.ping_ip) AS ping_ip, l.muted,
                      l.first_seen, l.last_seen, l.sampled_at,
                      coalesce(nullif(a.identity, ''), a.serial) AS a_name, a.state AS a_state,
                      coalesce(nullif(b.identity, ''), b.serial) AS b_name, b.state AS b_state
               FROM wlink l JOIN device a ON a.id = l.a_device JOIN device b ON b.id = l.b_device"""


def _view(tx: Tx, ln: dict) -> dict:
    a = assess(tx, ln["id"])
    return {"id": str(ln["id"]), "kind": ln["kind"], "name": f"{ln['a_name']} ↔ {ln['b_name']}", "muted": ln["muted"],
            "a": {"device_id": str(ln["a_device"]), "device": ln["a_name"], "iface": ln["a_iface"], "state": ln["a_state"]},
            "b": {"device_id": str(ln["b_device"]), "device": ln["b_name"], "iface": ln["b_iface"], "state": ln["b_state"]},
            "ping_ip": ln["ping_ip"], "first_seen": ln["first_seen"], "sampled_at": ln["sampled_at"], **a}


def site_links(tx: Tx, site_id: Any) -> list[dict]:
    rows = tx.fetchall(_LINK_SQL + " WHERE l.site_id = %(s)s ORDER BY a_name, b_name", {"s": str(site_id)})
    return [_view(tx, r) for r in rows]


def episodes(points: list[dict], us: dict) -> list[dict]:
    """Souvislé úseky, kdy spoj vybočil: [{from, to, codes, worst: {signal, loss, rate}}]. Mezera 1 bodu se spojí."""
    out: list[dict] = []
    cur: dict | None = None
    gap = 0
    for p in points:
        devs = [d for d in deviations(p, us) if d["code"] != "asym"]
        if p.get("reconnect") and not devs:
            devs = [{"code": "reconnect"}]
        if devs:
            if cur is None:
                cur = {"from": p["t"], "to": p["t"], "codes": [], "worst": {}}
            cur["to"], gap = p["t"], 0
            for d in devs:
                if d["code"] not in cur["codes"]:
                    cur["codes"].append(d["code"])
            w = cur["worst"]
            sig = [p[k] for k in ("signal_a", "signal_b") if p.get(k) is not None]
            if sig:
                w["signal"] = min(w.get("signal", 0), round(min(sig)))
            if p.get("loss") is not None:
                w["loss"] = max(w.get("loss", 0), round(p["loss"], 1))
        elif cur is not None:
            gap += 1
            if gap > 1:
                out.append(cur)
                cur = None
    if cur is not None:
        out.append(cur)
    return out


def detail(tx: Tx, link_id: Any, hours: int = 24) -> dict | None:
    """Karta spoje: stav + průběh (nejvýš ~300 bodů, delší období se průměruje po úsecích) + úseky odchylek."""
    ln = tx.fetchone(_LINK_SQL + " WHERE l.id = %(l)s", {"l": str(link_id)})
    if not ln:
        return None
    bucket = max(SAMPLE_EVERY_MIN * 60, hours * 3600 // 288)
    rows = tx.fetchall(
        """SELECT to_timestamp(floor(extract(epoch FROM at) / %(b)s) * %(b)s) AS t,
                  bool_and(up) AS up, bool_or(reconnect) AS reconnect,
                  avg(signal_a) AS signal_a, avg(signal_b) AS signal_b, avg(snr_a) AS snr_a, avg(snr_b) AS snr_b,
                  avg(rate_ab) AS rate_ab, avg(rate_ba) AS rate_ba, avg(ccq_a) AS ccq_a, avg(ccq_b) AS ccq_b,
                  avg(noise) AS noise, max(loss) AS loss, avg(rtt_avg) AS rtt_avg, max(rtt_max) AS rtt_max
           FROM wlink_sample WHERE link_id = %(l)s AND at > now() - make_interval(hours => %(h)s)
           GROUP BY 1 ORDER BY 1""", {"l": str(link_id), "h": hours, "b": bucket})
    pts = []
    for r in rows:
        p: dict[str, Any] = {"t": r["t"], "up": r["up"], "reconnect": r["reconnect"]}
        for k in ("signal_a", "signal_b", "snr_a", "snr_b", "ccq_a", "ccq_b", "noise", "loss", "rtt_avg", "rtt_max"):
            p[k] = round(float(r[k]), 1) if r[k] is not None else None
        for k in ("rate_ab", "rate_ba"):
            p[k] = int(r[k]) if r[k] is not None else None
        pts.append(p)
    v = _view(tx, ln)
    v.update(hours=hours, bucket=bucket, points=pts, episodes=episodes(pts, v["usual"]))
    return v


def set_muted(tx: Tx, tenant_id: Any, link_id: Any, muted: bool, user_id: Any = None, ip: str | None = None) -> bool:
    from .services import audit

    ln = tx.fetchone("SELECT id, muted FROM wlink WHERE id = %(l)s", {"l": str(link_id)})
    if not ln:
        return False
    tx.execute("UPDATE wlink SET muted = %(m)s WHERE id = %(l)s", {"m": bool(muted), "l": str(link_id)})
    audit(tx, "wlink.mute" if muted else "wlink.unmute", tenant_id=tenant_id, user_id=user_id, target=str(link_id),
          before={"muted": ln["muted"]}, after={"muted": bool(muted)}, ip=ip)
    return True
