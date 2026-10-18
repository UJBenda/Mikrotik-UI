"""Plán kanálů Wi-Fi lokality (obdoba UniFi Channel AI, zjednodušeně).

UniFi plánuje z RF skenů. Sken na RouterOS (`/interface wifi scan`) na chvíli
přeruší provoz rádia, proto ho controller nespouští a vychází z toho, co ví
bez rušení: AP jedné lokality se mohou navzájem slyšet (konzervativní
předpoklad), ostatní lokality ne. Cíl je co nejméně AP na stejném kanálu,
přednost mají kanály bez DFS a současný kanál AP (méně změn).

Kanály jsou pro EU (ETSI): 2,4 GHz 1/6/11, 5 GHz 36–64 a 100–140, bez
kanálů 120–128 (meteoradary, 10 min čekání na volný kanál) a bez 149+.
Výsledek je jen doporučení – nic se nemění.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

CH24 = [2412, 2437, 2462]
# řídicí kanál bloku podle šířky; pořadí = preference (bez DFS první)
CH5 = {
    20: [5180, 5200, 5220, 5240, 5260, 5280, 5300, 5320, 5500, 5520, 5540, 5560, 5580, 5660, 5680, 5700],
    40: [5180, 5220, 5260, 5300, 5500, 5540, 5660],
    80: [5180, 5260, 5500],
}
DFS = range(5250, 5730)
WIDTHS = (80, 40, 20)


def chan(freq: int) -> int:
    """Frekvence řídicího kanálu → číslo kanálu."""
    return (freq - 2407) // 5 if freq < 3000 else (freq - 5000) // 5


def parse_channel(v: Any) -> tuple[int | None, int | None]:
    """"5180/ax/Ceee" → (5180, 80); "2437/n/Ce" → (2437, 40); "5500" → (5500, None)."""
    s = str(v or "")
    m = re.match(r"\s*(\d{4})", s)
    if not m:
        return None, None
    freq = int(m.group(1))
    tail = s.rsplit("/", 1)[-1] if "/" in s else ""
    width = None
    if re.fullmatch(r"[CcEe]+", tail):
        width = 20 * len(tail)
    elif re.search(r"(\d+)\s*mhz", s, re.I):
        width = int(re.search(r"(\d+)\s*mhz", s, re.I).group(1))
    else:
        mw = re.search(r"/(20|40|80|160)(?:mhz|-|/|$)", s, re.I)
        width = int(mw.group(1)) if mw else None
    return freq, width


def width_mhz(v: Any) -> int | None:
    """Šířka z konfigurace: "20/40/80mhz" → 80, "20mhz" → 20, "20/40mhz-Ce" → 40."""
    nums = [int(x) for x in re.findall(r"(20|40|80|160)", str(v or ""))]
    return max(nums) if nums else None


def _block(freq: int, width: int) -> int | None:
    """Řídicí kanál bloku dané šířky, ve kterém frekvence leží."""
    for f in CH5.get(width, []):
        if f <= freq < f + width:
            return f
    return None


def band_of(r: dict) -> str | None:
    f = r.get("freq_now")
    if f:
        return "2.4" if f < 3000 else "5" if f < 5900 else "6"
    b = str(r.get("band") or "")
    if b.startswith("2"):
        return "2.4"
    if b.startswith("5"):
        return "5"
    if b.startswith("6"):
        return "6"
    return None


def site_width(n: int) -> int:
    """Šířka v 5 GHz podle počtu AP: aby se AP vešly vedle sebe bez sdílení kanálu."""
    for w in WIDTHS:
        if n <= len(CH5[w]):
            return w
    return 20


def _assign(radios: list[dict], channels: list[int], cur_key) -> dict[int, int]:
    """Vyrovnané přiřazení: nejdřív ponechá současné kanály do limitu, zbytek dostane nejméně použitý."""
    cap = max(1, math.ceil(len(radios) / len(channels)))
    use: Counter = Counter()
    out: dict[int, int] = {}
    # kanál si ponechávají přednostně běžící AP s nejvíc klienty (změna kanálu je odpojí)
    order = sorted(range(len(radios)), key=lambda k: (not radios[k].get("running", True), -(radios[k].get("clients") or 0), k))
    for k in order:
        r = radios[k]
        c = cur_key(r)
        if c in channels and use[c] < cap:
            out[k] = c
            use[c] += 1
    for k, r in enumerate(radios):
        if k in out:
            continue
        best = min(channels, key=lambda c: (use[c], channels.index(c)))
        out[k] = best
        use[best] += 1
    return out


def _clash(freqs: list[tuple[int, int]]) -> int:
    """Počet dvojic AP, jejichž kanály se překrývají."""
    n = 0
    for i, (fa, wa) in enumerate(freqs):
        for fb, wb in freqs[i + 1:]:
            if fa < 3000 and fb < 3000:
                # 2,4 GHz: kanál zabírá ~22 MHz, 40 MHz šířka sahá o 20 MHz výš (1/6/11 se nepřekrývají)
                a0, b0 = (fa - 11, fa + 11 + (20 if wa >= 40 else 0)), (fb - 11, fb + 11 + (20 if wb >= 40 else 0))
                n += a0[0] < b0[1] and b0[0] < a0[1]
            elif fa > 5000 and fb > 5000:
                a0, b0 = _span(fa, wa), _span(fb, wb)
                n += a0[0] < b0[1] and b0[0] < a0[1]
    return n


def _span(f: int, w: int) -> tuple[int, int]:
    start = _block(f, w) or f
    return start - 10, start - 10 + w


def plan(radios: list[dict], max_width: int = 80, dfs: bool = True) -> dict:
    """radios: výstup wifi._radio (+ freq_now, width_now). Vrací {items, before, after, notes}.
    `max_width` a `dfs` = Wi-Fi politika lokality (řízený režim)."""
    aps = [r for r in radios if not r.get("disabled") and not r.get("master")
           and "station" not in str(r.get("mode") or "") and band_of(r) in ("2.4", "5")]
    items: list[dict] = []
    notes: list[str] = []
    before_all: list[tuple[int, int]] = []
    after_all: list[tuple[int, int]] = []
    for band in ("2.4", "5"):
        group = [r for r in aps if band_of(r) == band]
        if not group:
            continue
        if band == "2.4":
            width = 20
            channels = CH24
        else:
            width = min(site_width(len(group)), max_width if max_width in CH5 else 80)
            channels = CH5[width]
            if not dfs:
                # bez DFS je v 80 MHz jen jeden blok: zúžit, dokud se AP nevejdou vedle sebe
                for w in [x for x in WIDTHS if x <= width]:
                    width, channels = w, [c for c in CH5[w] if c not in DFS]
                    if len(channels) >= len(group):
                        break

        def cur(r: dict) -> int | None:
            f = r.get("freq_now")
            if not f:
                return None
            if band == "2.4":
                return f if f in CH24 else None
            return _block(f, width)

        assign = _assign(group, channels, cur)
        for k, r in enumerate(group):
            f_now = r.get("freq_now")
            w_now = r.get("width_now") or width_mhz(r.get("width")) or 20
            same_block = f_now is not None and cur(r) == assign[k]
            # užší rádio může zůstat na svém kanálu uvnitř bloku; širší se jen zúží (rozšiřovat neradíme)
            f_new = f_now if same_block else assign[k]
            w_new = min(w_now, width)
            if f_now:
                before_all.append((f_now, w_now))
            after_all.append((f_new, w_new))
            reasons = []
            if band == "2.4" and f_now and f_now not in CH24:
                reasons.append(f"kanál {chan(f_now)} se překrývá s 1/6/11")
            if band == "2.4" and w_now > 20:
                reasons.append("v 2,4 GHz jen 20 MHz")
            if band == "5" and w_new < w_now:
                reasons.append(f"šířka {w_now} → {w_new} MHz pro {len(group)} AP v lokalitě")
            if band == "5" and f_now and f_now not in CH5[20]:
                reasons.append(f"kanál {chan(f_now)} není v plánu (mimo EU pásmo nebo meteoradary 120–128)")
            elif f_now and not same_block and not (band == "2.4" and f_now not in CH24):
                reasons.append("rozložení AP na různé kanály")
            if not f_now:
                reasons.append("kanál zatím neznámý (inventura bez monitoru)")
            change = not same_block or w_new != w_now
            items.append({
                "device_id": r["device_id"], "device": r["device"], "interface": r["interface"], "band": band,
                "now": {"freq": f_now, "channel": chan(f_now) if f_now else None, "width": w_now if f_now else None,
                        "auto": str(r.get("frequency") or "auto") == "auto"},
                "new": {"freq": f_new, "channel": chan(f_new), "width": w_new, "dfs": f_new in DFS},
                "change": bool(change), "reasons": reasons if change else [],
                "command": (f"/interface wifi set [find name=\"{r['interface']}\"] channel.frequency={f_new} "
                            f"channel.width={_width_arg(w_new)}") if r.get("driver") == "wifi" else None,
            })
        if band == "5" and any(i["new"]["dfs"] for i in items if i["band"] == "5"):
            notes.append("DFS kanály (52–64, 100+): při detekci radaru rádio na chvíli přejde jinam – "
                         "pro kritické AP preferuj 36–48.")
        if band == "2.4" and len(group) > 3:
            notes.append(f"{len(group)} AP v 2,4 GHz na 3 kanálech – zvaž vypnout 2,4 GHz na části AP.")
    if any(r.get("capsman") for r in aps):
        notes.append("Rádia řízená CAPsMANem nastav v konfiguraci CAPsMANu, ne přímo na AP.")
    return {"items": items, "before": _clash(before_all) if before_all else None, "after": _clash(after_all),
            "changes": sum(1 for i in items if i["change"]), "notes": notes}


def _width_arg(w: int) -> str:
    return {20: "20mhz", 40: "20/40mhz", 80: "20/40/80mhz"}[w]
