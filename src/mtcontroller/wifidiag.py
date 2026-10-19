"""Wi-Fi diagnostika klientů: časová osa (u kterého AP, signál, roaming, odpojení s důvodem)
a automatické nálezy, proč si klient stěžuje.

Zdroje:
- události z logů zařízení (téma `wireless`, služba mtc-syslog): připojení, odpojení s důvodem,
  roaming (FT / plná re-autentizace), selhání přihlášení – přesný čas,
- vzorky z registrační tabulky při každé inventuře: AP/rozhraní, SSID, signál, rychlost – i bez logů;
  změna AP mezi vzorky = roaming „odvozený“ (čas jen s přesností inventury).
Rozhraní CAP na CAPsMANu (cap-wifiN) se přeloží na AP a rádio podle MAC.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta
from typing import Any

from .db import Tx

GAP = timedelta(minutes=11)        # mezi vzorky (inventura 5 min) – delší mezera = klient pryč
PINGPONG_WINDOW = timedelta(minutes=10)
WEAK = -75
_MAC = r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
_EV = re.compile(_MAC + r"@([\w.-]+):?\s+(connected|disconnected|roamed|reassociating|failed to connect|"
                 r"rejected|auth(?:entication)? failed)(.*)$", re.I)
_SIG = re.compile(r"signal(?:[- ]strength)?[:= ]\s*(-\d{2,3})", re.I)


def parse_event(msg: str) -> dict | None:
    """Událost klienta z logu RouterOS (wifi i starý wireless), jinak None."""
    m = _EV.search(msg or "")
    if not m:
        return None
    mac, iface, verb, rest = m.group(1).upper(), m.group(2), m.group(3).lower(), m.group(4)
    sig = _SIG.search(rest)
    kind = {"connected": "connected", "disconnected": "disconnected", "roamed": "roamed",
            "reassociating": "reassoc"}.get(verb, "auth_fail")
    reason = re.sub(r"(,\s*)?signal(?:[- ]strength)?[:= ]\s*-\d+.*$", "", rest, flags=re.I).strip(" ,:")
    if kind == "auth_fail":
        reason = (verb + " " + reason).strip()
    ft = bool(re.search(r"\bft\b|fast.?transition|over.?ds", rest, re.I))
    if kind == "connected" and re.search(r"roam", rest, re.I):
        kind = "roamed"
    return {"mac": mac, "iface": iface[:64], "kind": kind, "signal": int(sig.group(1)) if sig else None,
            "ft": ft, "reason": reason[:200]}


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def iface_map(devices: list[dict], invs: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """(device_id, rozhraní) → {ap, ap_id, radio, band}. Rozhraní CAP na CAPsMANu podle MAC na AP."""
    names = {str(d["id"]): d.get("identity") or d.get("serial") for d in devices}
    by_mac: dict[str, dict] = {}
    out: dict[tuple[str, str], dict] = {}
    for did, inv in invs.items():
        bands = {}
        for r in _rows(inv, "wifi_radios"):
            b = str(r.get("bands") or "")
            bands[str(r.get("interface"))] = "5" if "5ghz" in b else "2.4" if "2ghz" in b else ""
        rows = _rows(inv, "wifi")
        masters = {str(w.get("name")): str(w.get("master-interface") or "") for w in rows}
        for w in rows:
            name = str(w.get("name") or "")
            root = masters.get(name) or name
            info = {"ap": names.get(did), "ap_id": did, "radio": name, "band": bands.get(root, "")}
            out[(did, name)] = info
            mac = str(w.get("mac-address") or "").upper()
            if mac and not str(name).startswith("cap"):
                by_mac[mac] = info
    # CAPsMAN: rozhraní cap-wifiN na hostiteli → AP podle MAC
    for did, inv in invs.items():
        for w in _rows(inv, "wifi"):
            mac = str(w.get("mac-address") or "").upper()
            if mac in by_mac and by_mac[mac]["ap_id"] != did:
                out[(did, str(w.get("name")))] = by_mac[mac]
    return out


def _lane(m: dict, did: str, iface: str, names: dict) -> dict:
    i = m.get((did, iface))
    if i:
        return {"key": f"{i['ap_id']}|{i['radio']}", "ap": i["ap"], "ap_id": i["ap_id"], "radio": i["radio"], "band": i["band"]}
    return {"key": f"{did}|{iface}", "ap": names.get(did) or "?", "ap_id": did, "radio": iface, "band": ""}


def _rate(v: Any) -> float | None:
    m = re.match(r"\s*([\d.]+)\s*([GMk]?)bps", str(v or ""))
    if not m:
        return None
    return float(m.group(1)) * {"G": 1000, "M": 1, "k": 0.001, "": 1e-6}[m.group(2)]


def journey(events: list[dict], samples: list[dict], m: dict, names: dict, until: datetime) -> dict:
    """Časová osa jednoho klienta: dráhy (AP/rádio), úseky připojení, body signálu a události."""
    pts = [dict(e, src="event") for e in events] + [dict(s, src="sample", kind="sample") for s in samples]
    pts.sort(key=lambda p: (p["at"], p["src"] == "sample"))
    lanes: dict[str, dict] = {}
    segs: list[dict] = []
    roams: list[dict] = []
    cur = None
    last_t = None

    def close(t):
        nonlocal cur
        if cur:
            cur["end"] = t
            segs.append(cur)
            cur = None

    def open_(lane, t, how):
        nonlocal cur
        prev = cur
        close(t)
        if prev is None and segs and t - segs[-1]["end"] <= timedelta(seconds=30):
            prev, how = segs[-1], ("ft" if how == "ft" else "reconnect")   # odpojení a hned připojení jinde
        if prev and prev["lane"] != lane["key"]:
            roams.append({"at": t, "from": prev["lane"], "to": lane["key"], "how": how})
        cur = {"lane": lane["key"], "start": t, "end": None, "signals": []}

    for p in pts:
        t = p["at"]
        lane = _lane(m, str(p["device_id"]), p.get("iface") or "", names)
        lanes.setdefault(lane["key"], lane)
        if cur and last_t and t - last_t > GAP and p["kind"] in ("sample", "connected", "roamed"):
            close(last_t + timedelta(minutes=5))
        k = p["kind"]
        if k in ("connected", "roamed", "reassoc"):
            if not cur or cur["lane"] != lane["key"]:
                how = "ft" if p.get("ft") else ("roam" if k == "roamed" else "reconnect")
                open_(lane, t, how)
        elif k == "disconnected":
            if cur and cur["lane"] == lane["key"]:
                close(t)
        elif k == "sample":
            if not cur or cur["lane"] != lane["key"]:
                open_(lane, t, "inferred")
        if cur and p.get("signal") is not None and cur["lane"] == lane["key"]:
            cur["signals"].append({"at": t, "signal": p["signal"]})
        last_t = t
    if cur:
        close(min(until, (last_t or until) + timedelta(minutes=5)))
    ev = [{k: e.get(k) for k in ("at", "kind", "signal", "ft", "reason")} | {"lane": _lane(m, str(e["device_id"]), e.get("iface") or "", names)["key"]}
          for e in events]
    return {"lanes": list(lanes.values()), "segments": segs, "roams": roams, "events": ev,
            "samples": [{"at": s["at"], "signal": s.get("signal"), "tx_rate": s.get("tx_rate"), "rx_rate": s.get("rx_rate"),
                         "ssid": s.get("ssid"), "lane": _lane(m, str(s["device_id"]), s.get("iface") or "", names)["key"]}
                        for s in samples]}


def diagnose(j: dict) -> list[dict]:
    """Nálezy z časové osy: co klientovi vadí a co s tím."""
    out: list[dict] = []
    lanes = {l["key"]: l for l in j["lanes"]}
    lab = lambda k: f"{lanes[k]['ap']} {lanes[k]['radio']}" if k in lanes else k  # noqa: E731
    disc = [e for e in j["events"] if e["kind"] == "disconnected"]
    if len(disc) >= 3:
        reasons: dict[str, int] = {}
        for e in disc:
            r = e["reason"] or "bez důvodu"
            reasons[r] = reasons.get(r, 0) + 1
        top = sorted(reasons.items(), key=lambda x: -x[1])[:3]
        out.append({"severity": "warning", "code": "disconnects",
                    "text": f"{len(disc)}× odpojení – nejčastěji: " + ", ".join(f"{r} ({n}×)" for r, n in top),
                    "fix": "„connection lost“ / „extensive data loss“ = slabý signál nebo rušení; „SA Query timeout“ = "
                           "problém s ochranou rámců (zkus management-protection=allowed); „4-way handshake timeout“ = heslo nebo klient."})
    fails = [e for e in j["events"] if e["kind"] == "auth_fail"]
    if fails:
        out.append({"severity": "warning", "code": "auth_fail", "text": f"{len(fails)}× neúspěšné přihlášení ({fails[-1]['reason']})",
                    "fix": "Klient má nejspíš staré heslo uložené v profilu – smazat síť v klientovi a připojit znovu."})
    roams = j["roams"]
    # ping-pong: rychlé přeskakování mezi dvěma AP
    for i in range(len(roams) - 3):
        w = roams[i:i + 4]
        if w[-1]["at"] - w[0]["at"] <= PINGPONG_WINDOW and len({r["from"] for r in w} | {r["to"] for r in w}) == 2:
            a, b = sorted({w[0]["from"], w[0]["to"]})
            out.append({"severity": "warning", "code": "pingpong",
                        "text": f"přeskakuje mezi {lab(a)} a {lab(b)} ({len(w)}× za {int((w[-1]['at'] - w[0]['at']).total_seconds() // 60) or 1} min)",
                        "fix": "Překrytí AP se stejně silným signálem – snížit vysílací výkon jednoho z AP nebo je posunout; "
                               "zkontrolovat, že obě AP mají různé kanály."})
            break
    slow = [r for r in roams if r["how"] == "reconnect"]
    if slow:
        out.append({"severity": "info", "code": "no_ft",
                    "text": f"{len(slow)}× přechod mezi AP s plným přihlášením (bez FT) – krátký výpadek spojení",
                    "fix": "Zkontrolovat, že SSID má zapnutý roaming (FT) a že klient FT podporuje (starší Android/IoT ne)."})
    # „lepkavý“ klient: dlouho slabý signál na jednom AP, přitom existuje jiné AP
    for s in j["segments"]:
        weak = [x for x in s["signals"] if x["signal"] is not None and x["signal"] <= WEAK]
        if len(weak) >= 3 and len(lanes) > 1 and (weak[-1]["at"] - weak[0]["at"]) >= timedelta(minutes=10):
            out.append({"severity": "warning", "code": "sticky",
                        "text": f"drží se slabého AP {lab(s['lane'])} (signál až {min(x['signal'] for x in weak)} dBm po "
                                f"{int((weak[-1]['at'] - weak[0]['at']).total_seconds() // 60)} min)",
                        "fix": "Klient sám nepřejde: zapnout 802.11k/v (steering je v našem CAPsMANu), případně snížit výkon "
                               "vzdáleného AP, aby klient dřív hledal lepší."})
            break
    sig = [x["signal"] for x in j["samples"] if x["signal"] is not None]
    if len(sig) >= 3 and statistics.median(sig) <= -72:
        out.append({"severity": "warning", "code": "weak", "text": f"slabý signál – medián {int(statistics.median(sig))} dBm",
                    "fix": "Klient je na okraji pokrytí – přidat nebo přesunout AP blíž, u 5 GHz zkusit 2,4 GHz."})
    rates = [r for r in (_rate(x["tx_rate"]) for x in j["samples"]) if r]
    if len(rates) >= 3 and statistics.median(rates) < 30:
        out.append({"severity": "info", "code": "low_rate", "text": f"nízká rychlost spojení – medián {statistics.median(rates):.0f} Mb/s",
                    "fix": "Slabý signál, rušení nebo starý klient (802.11g/n 1×1); zkontrolovat kanál a šířku."})
    if not j["events"] and j["samples"]:
        out.append({"severity": "info", "code": "no_logs", "text": "bez událostí z logů – časy roamingu jen s přesností inventury",
                    "fix": "Zapni „Wi-Fi události klientů“ v Logy u AP (nebo u CAPsMANu) – uvidíš přesné časy a důvody odpojení."})
    if not out and (j["samples"] or j["events"]):
        out.append({"severity": "ok", "code": "ok", "text": "bez zjevného problému v tomto období", "fix": ""})
    return out


def _site_inputs(tx: Tx, site_id: Any):
    from . import inventory

    devs = tx.fetchall("""SELECT id, identity, serial FROM device WHERE site_id = %(s)s
                          AND state IN ('adopted', 'offline')""", {"s": str(site_id)})
    invs = {str(d["id"]): inventory.load(tx, d["id"], ["wifi", "wifi_radios"]) for d in devs}
    return iface_map(devs, invs), {str(d["id"]): d["identity"] or d["serial"] for d in devs}


def client_journey(tx: Tx, site_id: Any, mac: str, hours: int) -> dict:
    m, names = _site_inputs(tx, site_id)
    p = {"s": str(site_id), "m": mac, "h": int(hours)}
    events = tx.fetchall("""SELECT at, device_id, iface, kind, signal, ft, reason FROM wifi_event
                            WHERE site_id = %(s)s AND mac = %(m)s AND at > now() - make_interval(hours => %(h)s::int)
                            ORDER BY at LIMIT 5000""", p)
    samples = tx.fetchall("""SELECT at, device_id, iface, ssid, signal, tx_rate, rx_rate FROM wifi_sample
                             WHERE site_id = %(s)s AND mac = %(m)s AND at > now() - make_interval(hours => %(h)s::int)
                             ORDER BY at LIMIT 20000""", p)
    now = tx.fetchone("SELECT now() AS n")["n"]
    j = journey(events, samples, m, names, now)
    j["findings"] = diagnose(j)
    j["from"] = now - timedelta(hours=int(hours))
    j["until"] = now
    return j


def site_roaming(tx: Tx, site_id: Any, hours: int) -> dict:
    """Přehled lokality: klienti seřazení podle problémů, matice přechodů mezi AP."""
    m, names = _site_inputs(tx, site_id)
    p = {"s": str(site_id), "h": int(hours)}
    events = tx.fetchall("""SELECT mac, at, device_id, iface, kind, signal, ft, reason FROM wifi_event
                            WHERE site_id = %(s)s AND at > now() - make_interval(hours => %(h)s::int) ORDER BY at""", p)
    samples = tx.fetchall("""SELECT mac, at, device_id, iface, ssid, signal, tx_rate, rx_rate FROM wifi_sample
                             WHERE site_id = %(s)s AND at > now() - make_interval(hours => %(h)s::int) ORDER BY at""", p)
    now = tx.fetchone("SELECT now() AS n")["n"]
    by: dict[str, tuple[list, list]] = {}
    for e in events:
        by.setdefault(e["mac"], ([], []))[0].append(e)
    for s in samples:
        by.setdefault(s["mac"], ([], []))[1].append(s)
    clients, matrix, lanes = [], {}, {}
    for mac, (ev, sm) in by.items():
        j = journey(ev, sm, m, names, now)
        f = diagnose(j)
        for ln in j["lanes"]:
            lanes[ln["key"]] = ln
        for r in j["roams"]:
            k = f"{r['from']}>{r['to']}"
            matrix[k] = matrix.get(k, 0) + 1
        sig = [x["signal"] for x in sm if x["signal"] is not None]
        score = sum({"warning": 3, "info": 1}.get(x["severity"], 0) for x in f)
        clients.append({"mac": mac, "roams": len(j["roams"]), "disconnects": sum(1 for e in ev if e["kind"] == "disconnected"),
                        "min_signal": min(sig) if sig else None, "last_ap": (j["segments"][-1]["lane"] if j["segments"] else None),
                        "findings": [x for x in f if x["severity"] in ("warning", "info") and x["code"] != "no_logs"], "score": score})
    clients.sort(key=lambda c: (-c["score"], -c["disconnects"], -c["roams"]))
    return {"clients": clients[:200], "lanes": list(lanes.values()),
            "matrix": [{"from": k.split(">")[0], "to": k.split(">")[1], "n": n} for k, n in sorted(matrix.items(), key=lambda x: -x[1])],
            "has_events": bool(events), "hours": hours}


def record_samples(tx: Tx, tenant_id: Any, site_id: Any, device_id: Any, data: dict) -> int:
    """Vzorky klientů z registrační tabulky (volá inventory.store)."""
    n = 0
    for key in ("wifi_clients", "wireless_clients"):
        for r in _rows(data, key):
            mac = str(r.get("mac-address") or "").upper()
            if not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", mac):
                continue
            sig = re.match(r"\s*(-\d{2,3})", str(r.get("signal") or r.get("signal-strength") or ""))
            tx.execute("""INSERT INTO wifi_sample (tenant_id, site_id, device_id, mac, iface, ssid, signal, tx_rate, rx_rate, uptime)
                          VALUES (%(t)s, %(s)s, %(d)s, %(m)s, %(i)s, %(ss)s, %(sg)s, %(tr)s, %(rr)s, %(u)s)""",
                       {"t": str(tenant_id), "s": str(site_id) if site_id else None, "d": str(device_id), "m": mac,
                        "i": str(r.get("interface") or "")[:64], "ss": str(r.get("ssid") or "")[:64],
                        "sg": int(sig.group(1)) if sig else None, "tr": str(r.get("tx-rate") or "")[:40],
                        "rr": str(r.get("rx-rate") or "")[:40], "u": str(r.get("uptime") or "")[:40]})
            n += 1
    return n
