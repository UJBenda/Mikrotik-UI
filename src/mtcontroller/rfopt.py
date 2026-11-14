"""Optimalizace kanálů a výkonu Wi-Fi podle měření (sken okolí každým AP).

`channels.plan` rozkládá AP na kanály jen podle počtu AP (bez měření). Tady se pracuje s tím,
co AP skutečně slyší (rogue.scan_device → pozorování po AP v site_policy `wifi_scan`):

- cizí sítě (sousedé) na kanálu a v jeho okolí, vážené silou signálu,
- vlastní AP, která se navzájem slyší (silně slyšitelná dvojice nemá sdílet kanál).

Pro každé rádio se hledá kanál (v 5 GHz i šířka) s nejmenším rušením; stávající kanál má
přednost, když není znatelně horší (zbytečné přelaďování odpojuje klienty). Volitelně se
sníží výkon rádií, která se na stejném kanálu slyší příliš silně. Výsledek se uloží jako
připnutí (site_policy `wifi_rf`) a CAPsMAN ho použije místo plánu podle počtu AP.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any

from . import channels, inventory, txpower
from .db import Database, Tx, tenant_scope
from .services import ServiceError, audit

SCAN_MODULE = "wifi_scan"
RF_MODULE = "wifi_rf"
KEEP_BSS = 80           # nejsilnějších sítí na AP
STALE_DAYS = 7          # starší měření se nepoužije
HYSTERESIS = 0.3        # o kolik musí být nový kanál lepší, aby se přelaďovalo
WIDTH_STEP = 0.6        # „cena“ zúžení kanálu o jeden stupeň (80 → 40 → 20)
UNKNOWN_PAIR = 1.5      # dvojice vlastních AP bez měření: počítat se středním rušením
CO_CHANNEL_LIMIT = -70  # dBm: silněji slyšitelné vlastní AP na stejném kanálu → snížit výkon
MAX_DROP = 6


# --- měření ---------------------------------------------------------------------------------------

def save_scan(tx: Tx, tenant_id: Any, site_id: Any, device_id: Any, rows: list[dict]) -> None:
    """Pozorování jednoho AP (rows: [{bssid, ssid, frequency, signal, kind}]) – nahradí předchozí."""
    best = sorted((r for r in rows if r.get("frequency")), key=lambda r: -(r.get("signal") or -100))[:KEEP_BSS]
    cur = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s FOR UPDATE",
                      {"s": str(site_id), "m": SCAN_MODULE})
    data = dict(cur["data"]) if cur else {}
    data[str(device_id)] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "bss": [[r["bssid"], int(r["frequency"]), r.get("signal"), r.get("kind") or "neighbor",
                                     str(r.get("ssid") or "")[:32]] for r in best]}
    tx.execute("""INSERT INTO site_policy (tenant_id, site_id, module, data) VALUES (%(t)s, %(s)s, %(m)s, %(d)s)
                  ON CONFLICT (site_id, module) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
               {"t": str(tenant_id), "s": str(site_id), "m": SCAN_MODULE, "d": data})


def load_scan(tx: Tx, site_id: Any, now: dt.datetime | None = None) -> dict[str, dict]:
    """{device_id: {at, bss: [{bssid, frequency, signal, kind, ssid}]}} – jen čerstvá měření."""
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s",
                    {"s": str(site_id), "m": SCAN_MODULE})
    now = now or dt.datetime.now(dt.timezone.utc)
    out: dict[str, dict] = {}
    for did, v in ((r or {}).get("data") or {}).items():
        try:
            at = dt.datetime.fromisoformat(str(v.get("at")))
        except (TypeError, ValueError, AttributeError):
            continue
        if now - at > dt.timedelta(days=STALE_DAYS):
            continue
        out[did] = {"at": v["at"], "bss": [{"bssid": b[0], "frequency": b[1], "signal": b[2], "kind": b[3], "ssid": b[4]}
                                           for b in v.get("bss") or [] if isinstance(b, list) and len(b) >= 5]}
    return out


# --- model rušení ---------------------------------------------------------------------------------

def _band(freq: Any) -> str | None:
    try:
        f = int(freq)
    except (TypeError, ValueError):
        return None
    return "2.4" if 2400 <= f < 2500 else "5" if 5000 <= f < 5900 else None


def _weight(sig: Any) -> float:
    """Síla signálu → váha rušení (−92 dBm a slabší skoro nic, −44 dBm a silnější maximum)."""
    if not isinstance(sig, (int, float)):
        return 1.0
    return max(0.1, min(4.0, (sig + 92) / 12))


def _overlap(band: str, fa: int, wa: int, fb: int, wb: int = 20) -> float:
    """Podíl překryvu dvou kanálů (0–1)."""
    if band == "2.4":
        d = abs(fa - fb)
        return max(0.0, 1 - d / 25)
    a0, b0 = channels._span(fa, wa), channels._span(fb, wb)
    return 1.0 if a0[0] < b0[1] and b0[0] < a0[1] else 0.0


def _candidates(band: str, max_width: int, dfs: bool) -> list[tuple[int, int]]:
    if band == "2.4":
        return [(f, 20) for f in channels.CH24]
    out = []
    for w in channels.WIDTHS:
        if w > max_width:
            continue
        for f in channels.CH5[w]:
            if dfs or not any(x in channels.DFS for x in range(f, f + w, 20)):
                out.append((f, w))
    return out


def _suffix(mac: Any) -> str:
    return str(mac or "").upper()[-8:]


def plan(radios: list[dict], obs: dict[str, dict], *, max_width: int = 80, dfs: bool = True,
         lower_tx: bool = True, tx_mode: str = "balanced") -> dict:
    """radios: wificfg.site_radios; obs: load_scan. Vrací {items, before, after, changes, missing, notes}."""
    max_width = max_width if max_width in channels.CH5 else 80
    own = {_suffix(r.get("mac")): r["device_id"] for r in radios if r.get("mac")}
    names = {r["device_id"]: r.get("device") or r["device_id"] for r in radios}
    hear: dict[tuple[str, str], dict[str, int]] = {}
    foreign: dict[tuple[str, str], list[dict]] = {}
    for did, o in obs.items():
        for b in o["bss"]:
            band = _band(b["frequency"])
            if not band:
                continue
            other = own.get(_suffix(b["bssid"]))
            if other and other != did:
                cur = hear.setdefault((did, band), {})
                s = b["signal"] if isinstance(b["signal"], (int, float)) else -80
                cur[other] = max(cur.get(other, -200), int(s))
            elif not other and b["kind"] != "own":
                foreign.setdefault((did, band), []).append(b)

    def pair(a: str, b: str, band: str) -> float:
        """Váha rušení mezi dvěma vlastními AP v pásmu."""
        s = max(hear.get((a, band), {}).get(b, -200), hear.get((b, band), {}).get(a, -200))
        if s > -200:
            return _weight(s) * 1.5
        return 0.0 if a in obs and b in obs else UNKNOWN_PAIR

    def cost(r: dict, band: str, f: int, w: int, assign: dict[int, tuple[int, int]], group: list[dict]) -> float:
        c = sum(_weight(b["signal"]) * _overlap(band, f, w, int(b["frequency"]))
                for b in foreign.get((r["device_id"], band), []))
        for k, o in enumerate(group):
            if o is r or k not in assign or o["device_id"] == r["device_id"]:
                continue
            ov = _overlap(band, f, w, *assign[k])
            if ov:
                c += ov * pair(r["device_id"], o["device_id"], band)
        return c

    base_tx = txpower.plan(radios, tx_mode)
    items: list[dict] = []
    notes: list[str] = []
    before = after = 0.0
    for band in ("2.4", "5"):
        group = [r for r in radios if ("2.4" if str(r.get("band")).startswith("2") else "5") == band]
        if not group:
            continue
        cands = _candidates(band, max_width, dfs)
        now: dict[int, tuple[int, int]] = {k: (int(r["freq_now"]), int(r.get("width_now") or 20))
                                           for k, r in enumerate(group) if r.get("freq_now")}
        c_now = {k: cost(r, band, *now[k], now, group) for k, r in enumerate(group) if k in now}
        before += sum(c_now.values())
        order = sorted(range(len(group)), key=lambda k: (-sum(_weight(b["signal"]) for b in
                                                               foreign.get((group[k]["device_id"], band), [])),
                                                         str(group[k]["device_id"]), str(group[k]["interface"])))

        def pen(w: int) -> float:
            return WIDTH_STEP * math.log2(max_width / w) if band == "5" else 0.0

        def search(cap: int) -> tuple[float, dict[int, tuple[int, int]]]:
            """Přiřazení s kanály nejvýš `cap` MHz. Začíná od současného stavu (když je kanál v plánu),
            nejzatíženější rádia první; stávající kanál má přednost, není-li znatelně horší."""
            cs = [c for c in cands if c[1] <= cap]
            assign: dict[int, tuple[int, int]] = {}
            for k, (f, w) in now.items():
                if band == "2.4" and f in channels.CH24:
                    assign[k] = (f, 20)
                elif band == "5":
                    w2 = min(w if w in channels.CH5 else 20, cap)
                    blk = channels._block(f, w2)
                    if blk and (blk, w2) in cs:
                        assign[k] = (blk, w2)
            for _round in range(4):
                moved = False
                for k in order:
                    r = group[k]
                    best, best_c = None, math.inf
                    for f, w in cs:
                        c = cost(r, band, f, w, assign, group) + pen(w)
                        if c < best_c - 1e-9:
                            best, best_c = (f, w), c
                    cur = assign.get(k)
                    if cur is not None and cost(r, band, *cur, assign, group) + pen(cur[1]) <= best_c + HYSTERESIS:
                        continue
                    if best is not None and best != cur:
                        assign[k] = best
                        moved = True
                if not moved:
                    break
            return sum(cost(group[k], band, *assign[k], assign, group) + pen(assign[k][1]) for k in assign), assign

        # jedno rádio samo zúžením nepomůže, když sousední zůstane široké → zkusit i užší strop pro celé pásmo
        total, assign = math.inf, {}
        for cap in ([20] if band == "2.4" else [w for w in channels.WIDTHS if w <= max_width]):
            t, a = search(cap)
            if t < total - HYSTERESIS:
                total, assign = t, a
        for k, r in enumerate(group):
            f_new, w_new = assign[k]
            c_new = cost(r, band, f_new, w_new, assign, group)
            after += c_new
            f_now, w_now = now.get(k, (None, None))
            same = f_now is not None and w_now == w_new and (
                f_now == f_new if band == "2.4" else channels._block(f_now, w_new) == f_new)
            if same and band == "5":
                f_new = f_now  # řídicí kanál uvnitř bloku neměnit
            fr = foreign.get((r["device_id"], band), [])
            worst = max(fr, key=lambda b: b["signal"] if isinstance(b["signal"], (int, float)) else -200, default=None)
            key = (r["device_id"], r["interface"])
            tp = (base_tx.get(key) or {}).get("tx_power")
            why = (base_tx.get(key) or {}).get("reason") or ""
            if lower_tx:
                co = [(hear.get((r["device_id"], band), {}).get(o["device_id"], -200), o) for j, o in enumerate(group)
                      if o["device_id"] != r["device_id"] and _overlap(band, f_new, w_new, *assign[j])]
                s, o = max(co, key=lambda x: x[0], default=(-200, None))
                sig = [int(x) for x in r.get("signals") or [] if isinstance(x, (int, float))]
                far = len(sig) >= 3 and statistics.median(sig) < -70
                if s > CO_CHANNEL_LIMIT and o is not None and not far:
                    drop = min(MAX_DROP, math.ceil((s - CO_CHANNEL_LIMIT) / 3))
                    lo, hi = txpower.LIMITS[band]
                    tp = max(lo, (tp if tp is not None else hi) - drop)
                    why = f"na stejném kanálu slyší {names[o['device_id']]} ({s} dBm) – výkon −{drop} dB"
                elif s > CO_CHANNEL_LIMIT and far:
                    why += "; výkon nesnížen – klienti jsou daleko"
            items.append({
                "device_id": r["device_id"], "device": r.get("device"), "interface": r["interface"], "band": band,
                "scanned": r["device_id"] in obs,
                "now": {"freq": f_now, "channel": channels.chan(f_now) if f_now else None, "width": w_now,
                        "cost": round(c_now[k], 1) if k in c_now else None},
                "new": {"freq": f_new, "channel": channels.chan(f_new), "width": w_new, "cost": round(c_new, 1),
                        "dfs": f_new in channels.DFS},
                "change": not same, "foreign": len(fr),
                "strongest": ({"ssid": worst["ssid"], "signal": worst["signal"], "channel": channels.chan(int(worst["frequency"]))}
                              if worst else None),
                "tx_power": tp, "tx_reason": why,
            })
    missing = sorted({names[r["device_id"]] for r in radios if r["device_id"] not in obs})
    if missing:
        notes.append("Bez měření: " + ", ".join(missing) + " – u nich se počítá jen s odhadem.")
    if any(i["new"]["dfs"] for i in items):
        notes.append("DFS kanály (52–64, 100+): při detekci radaru rádio na chvíli přejde jinam.")
    return {"items": items, "before": round(before, 1), "after": round(after, 1),
            "changes": sum(1 for i in items if i["change"]), "missing": missing, "notes": notes}


# --- lokalita -------------------------------------------------------------------------------------

def _site(tx: Tx, site_id: Any, lower_tx: bool) -> tuple[dict, list[dict]]:
    from . import takeover, wificfg
    from . import intent as intentmod

    cur = takeover.load_intent(tx, site_id)
    if not cur:
        raise ServiceError("lokalita nemá sítě")
    pol = cur.get("wifi") or intentmod.validate_wifi({})
    aps = takeover._aps(tx, site_id)
    if not aps:
        raise ServiceError("lokalita nemá řízená AP")
    invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in aps}
    radios = wificfg.site_radios(aps, invs)
    obs = load_scan(tx, site_id)
    p = plan(radios, {k: v for k, v in obs.items() if k in invs}, max_width=int(pol.get("max_width") or 80),
             dfs=bool(pol.get("dfs", True)), lower_tx=lower_tx, tx_mode=pol.get("tx_power") or "balanced")
    p["aps"] = [{"device_id": str(d["id"]), "device": d.get("identity") or d.get("serial"), "online": d.get("state") == "adopted",
                 "scanned_at": (obs.get(str(d["id"])) or {}).get("at")} for d in aps]
    return p, aps


def get_pins(tx: Tx, site_id: Any) -> dict | None:
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s",
                    {"s": str(site_id), "m": RF_MODULE})
    return r["data"] if r else None


def preview(db: Database, tenant_id: Any, site_id: Any, lower_tx: bool = True) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        p, _aps = _site(tx, site_id, lower_tx)
        pins = get_pins(tx, site_id)
        p["pinned"] = {"at": pins.get("at"), "radios": len(pins.get("radios") or {})} if pins else None
    return p


def apply(db: Database, tenant_id: Any, site_id: Any, lower_tx: bool = True, user_id: Any = None,
          ip: str | None = None) -> dict:
    """Spočítá plán a připne ho (CAPsMAN ho použije při dalším nasazení Wi-Fi)."""
    with tenant_scope(db, tenant_id) as tx:
        p, _aps = _site(tx, site_id, lower_tx)
        if len(p["missing"]) == len(p["aps"]):
            raise ServiceError("žádné AP zatím nemá měření okolí – nejdřív spusť měření")
        pins = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "lower_tx": bool(lower_tx),
                "before": p["before"], "after": p["after"],
                "radios": {f"{i['device_id']}/{i['interface']}": {"freq": i["new"]["freq"], "width": i["new"]["width"],
                                                                  **({"tx": i["tx_power"]} if lower_tx else {})}
                           for i in p["items"]}}
        old = get_pins(tx, site_id)
        tx.execute("""INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
                      VALUES (%(t)s, %(s)s, %(m)s, %(d)s, %(u)s)
                      ON CONFLICT (site_id, module) DO UPDATE SET data = EXCLUDED.data, updated_by = EXCLUDED.updated_by,
                          updated_at = now()""",
                   {"t": str(tenant_id), "s": str(site_id), "m": RF_MODULE, "d": pins, "u": str(user_id) if user_id else None})
        audit(tx, "wifi.optimize", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              before={"radios": (old or {}).get("radios")},
              after={"radios": pins["radios"], "before": p["before"], "after": p["after"]}, ip=ip)
    return p


def reset(db: Database, tenant_id: Any, site_id: Any, user_id: Any = None, ip: str | None = None) -> bool:
    """Zruší připnutí – kanály se vrátí k plánu podle počtu AP."""
    with tenant_scope(db, tenant_id) as tx:
        old = get_pins(tx, site_id)
        if not old:
            return False
        tx.execute("DELETE FROM site_policy WHERE site_id = %(s)s AND module = %(m)s", {"s": str(site_id), "m": RF_MODULE})
        audit(tx, "wifi.optimize_reset", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              before={"radios": old.get("radios")}, ip=ip)
    return True


def pin_for(rf: dict | None, device_id: Any, interface: str, band: str, policy: dict) -> dict | None:
    """Připnutý kanál rádia, pokud pořád vyhovuje politice lokality (šířka, DFS)."""
    p = ((rf or {}).get("radios") or {}).get(f"{device_id}/{interface}")
    if not isinstance(p, dict):
        return None
    try:
        f, w = int(p["freq"]), int(p["width"])
    except (KeyError, TypeError, ValueError):
        return None
    if _band(f) != band or w not in (20, 40, 80):
        return None
    if band == "2.4":
        w = 20
    else:
        w = min(w, int(policy.get("max_width") or 80))
        if not policy.get("dfs", True) and any(x in channels.DFS for x in range(channels._block(f, w) or f, (channels._block(f, w) or f) + w, 20)):
            return None
    out: dict[str, Any] = {"freq": f, "width": w}
    if "tx" in p and (p["tx"] is None or isinstance(p["tx"], int)):
        out["tx"] = p["tx"]
    return out
