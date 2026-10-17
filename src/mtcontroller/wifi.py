"""Wi-Fi lokality (jen čtení): rádia, SSID, kanály, klienti a signál, CAPsMAN.

Podporuje nový balík `wifi` (/interface/wifi, RouterOS 7.13+) i starý
`wireless` (/interface/wireless). Hodnoty v REST výpisu /interface/wifi jsou
ploché klíče s tečkou ("configuration.ssid", "channel.frequency").
Konfigurované hodnoty mohou chybět (auto, profil) – pak se zobrazí „auto“.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

WEAK_SIGNAL = -75  # dBm; slabší klienti zpomalují celé rádio


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _dbm(v: Any) -> int | None:
    """"-63", "-63dBm@6Mbps" (wireless), "-63 dBm" → -63."""
    m = re.match(r"\s*(-?\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def _first_freq(v: Any) -> int | None:
    m = re.match(r"\s*(\d{4})", str(v or ""))
    return int(m.group(1)) if m else None


def _radio(d: dict, w: dict, driver: str, regs: list[dict]) -> dict:
    name = str(w.get("name") or "")
    if driver == "wifi":
        ssid = w.get("configuration.ssid") or w.get("ssid")
        mode = w.get("configuration.mode") or w.get("mode")
        band = w.get("channel.band") or w.get("band")
        freq = w.get("channel.frequency") or w.get("frequency")
        width = w.get("channel.width") or w.get("channel-width")
        capsman = str(w.get("configuration.manager") or "").startswith("capsman") or _true(w.get("bound"))
    else:
        ssid, mode, band = w.get("ssid"), w.get("mode"), w.get("band")
        freq, width = w.get("frequency"), w.get("channel-width")
        capsman = False
    signals = [s for s in (_dbm(r.get("signal") if driver == "wifi" else r.get("signal-strength")) for r in regs)
               if s is not None]
    return {
        "device_id": str(d["id"]), "device": d.get("identity") or d.get("serial"),
        "interface": name, "driver": driver, "master": w.get("master-interface") or None,
        "ssid": ssid, "mode": mode or None, "band": band or None,
        "frequency": str(freq) if freq not in (None, "") else "auto", "width": width or None,
        "running": _true(w.get("running")), "disabled": _true(w.get("disabled")), "capsman": capsman,
        "clients": len(regs), "signal_avg": round(sum(signals) / len(signals)) if signals else None,
        "signal_min": min(signals) if signals else None,
        "weak_clients": sum(1 for s in signals if s < WEAK_SIGNAL),
    }


def site_wifi(devices: list[dict], invs: dict[str, dict]) -> dict:
    radios: list[dict] = []
    caps: list[dict] = []
    for d in devices:
        inv = invs.get(str(d["id"]), {})
        by_iface: dict[str, list[dict]] = defaultdict(list)
        for r in _rows(inv, "wifi_clients") + _rows(inv, "wireless_clients"):
            by_iface[str(r.get("interface") or "")].append(r)
        for w in _rows(inv, "wifi"):
            radios.append(_radio(d, w, "wifi", by_iface.get(str(w.get("name")), [])))
        for w in _rows(inv, "wireless"):
            radios.append(_radio(d, w, "wireless", by_iface.get(str(w.get("name")), [])))
        for section, legacy in (("capsman_caps", False), ("capsman_caps_legacy", True)):
            for c in _rows(inv, section):
                caps.append({"controller_id": str(d["id"]), "controller": d.get("identity") or d.get("serial"),
                             "name": c.get("name"), "identity": c.get("identity"), "address": c.get("address"),
                             "board": c.get("board"), "version": c.get("version"), "state": c.get("state"),
                             "legacy": legacy})

    warnings: list[dict] = []
    for r in radios:
        label = f"{r['device']} {r['interface']}"
        if r["disabled"]:
            continue
        if not r["running"] and not r["master"]:
            warnings.append({"severity": "warning", "text": f"{label}: rádio neběží"})
        if r["weak_clients"]:
            warnings.append({"severity": "info",
                             "text": f"{label}: {r['weak_clients']} klientů se signálem pod {WEAK_SIGNAL} dBm"})
        f = _first_freq(r["frequency"])
        if f and f < 3000 and r["width"] and "40" in str(r["width"]):
            warnings.append({"severity": "info", "text": f"{label}: 40 MHz v pásmu 2,4 GHz ruší sousední sítě"})
    # stejný pevný kanál na více AP lokality (jen přístupové body, ne stanice/PtP klienti)
    same: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for r in radios:
        f = _first_freq(r["frequency"])
        if f and not r["disabled"] and not r["master"] and "station" not in str(r["mode"] or ""):
            same[f].append((r["device_id"], f"{r['device']} {r['interface']}"))
    for f, where in sorted(same.items()):
        if len({did for did, _ in where}) > 1:
            warnings.append({"severity": "info",
                             "text": f"kanál {f} MHz nastaven pevně na více AP: {', '.join(w for _, w in where)}"})

    ssids = sorted({str(r["ssid"]) for r in radios if r["ssid"] and not r["disabled"]})
    return {
        "radios": sorted(radios, key=lambda r: (r["device"] or "", r["interface"])),
        "caps": caps,
        "warnings": warnings,
        "summary": {"radios": len(radios), "aps": len({r["device_id"] for r in radios}),
                    "clients": sum(r["clients"] for r in radios), "ssids": ssids},
    }
