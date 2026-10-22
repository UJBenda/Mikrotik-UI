"""Automatický výkon vysílání AP (jako Auto TX power v UniFi/Meraki a vyrovnání pásem v Aruba AI Insights).

RF sken by přerušil klienty, proto plán vychází z dat, která controller má: počet AP lokality
v pásmu, kombinace pásem na AP a signál klientů na rádiu. Pravidla:

- jediné AP v pásmu: nechat výchozí maximum RouterOS (pokrytí je důležitější než rušení),
- hustota AP v pásmu 5 GHz: 2–3 AP → 20 dBm, 4–7 → 17 dBm, 8 a víc → 14 dBm,
- 2,4 GHz na dvoupásmovém AP o 6 dB níž než 5 GHz: dvoupásmoví klienti pak volí 5 GHz
  (vyrovnání pásem) a 2,4 GHz se méně překrývá,
- korekce podle klientů rádia (aspoň 3): medián signálu silnější než −50 dBm → o 3 dB níž
  (AP „křičí“ a klienti se drží), slabší než −72 dBm → o 3 dB výš,
- meze 2,4 GHz 8–20 dBm, 5 GHz 10–23 dBm; zákonný strop (EIRP) dál hlídá RouterOS podle země.
"""

from __future__ import annotations

import statistics
from typing import Any

MODES = ("balanced", "auto")
LIMITS = {"2.4": (8, 20), "5": (10, 23)}


def _density_5(n: int) -> int | None:
    if n <= 1:
        return None
    if n <= 3:
        return 20
    if n <= 7:
        return 17
    return 14


def _band(r: dict) -> str:
    return "2.4" if str(r.get("band", "")).startswith("2") else "5"


def plan(radios: list[dict], mode: str = "balanced") -> dict[tuple[str, str], dict]:
    """radios: [{device_id, interface, band ('2ghz'/'5ghz' nebo '2.4'/'5'), signals: [dBm, …]}].
    Vrací {(device_id, interface): {tx_power: int | None, reason}} – None = výchozí maximum."""
    out: dict[tuple[str, str], dict] = {}
    if mode not in MODES or mode == "auto":
        for r in radios:
            out[(r["device_id"], r["interface"])] = {"tx_power": None, "reason": "výchozí (maximum povolené v zemi)"}
        return out
    aps = {b: {r["device_id"] for r in radios if _band(r) == b} for b in ("2.4", "5")}
    dual = {d for d in aps["2.4"] if d in aps["5"]}
    base5 = _density_5(len(aps["5"]))
    for r in radios:
        band, key = _band(r), (r["device_id"], r["interface"])
        if band == "5":
            p, why = base5, f"{len(aps['5'])} AP v 5 GHz"
        elif r["device_id"] in dual:
            p, why = (base5 if base5 is not None else 23) - 6, "2,4 GHz o 6 dB níž než 5 GHz (vyrovnání pásem)"
        else:
            n = len(aps["2.4"])
            p, why = ((_density_5(n) or 0) - 3 if n > 1 else None), f"{n} AP v 2,4 GHz"
        if p is None:
            out[key] = {"tx_power": None, "reason": "jediné AP v pásmu – maximum"}
            continue
        sig = [int(s) for s in r.get("signals") or [] if isinstance(s, (int, float))]
        if len(sig) >= 3:
            med = statistics.median(sig)
            if med > -50:
                p -= 3
                why += f"; klienti velmi blízko (medián {med:.0f} dBm) −3 dB"
            elif med < -72:
                p += 3
                why += f"; klienti daleko (medián {med:.0f} dBm) +3 dB"
        lo, hi = LIMITS[band]
        out[key] = {"tx_power": max(lo, min(hi, int(p))), "reason": why}
    return out


def signals(inv: dict) -> dict[str, list[int]]:
    """Signál klientů podle rozhraní z registrační tabulky (např. '-61' nebo '-61dBm')."""
    out: dict[str, list[int]] = {}
    for c in inv.get("wifi_clients") or []:
        if not isinstance(c, dict):
            continue
        try:
            s = int(str(c.get("signal") or "").replace("dBm", "").strip())
        except ValueError:
            continue
        out.setdefault(str(c.get("interface")), []).append(s)
    return out


def apply_to(rows: list[dict], tp: dict[tuple[str, str], Any]) -> None:
    for r in rows:
        x = tp.get((r["device_id"], r["interface"])) or {}
        r["tx_power"], r["tx_reason"] = x.get("tx_power"), x.get("reason")
