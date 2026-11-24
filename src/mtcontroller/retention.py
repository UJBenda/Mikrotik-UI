"""Stupňovaná retence záloh controlleru (lokálně i v cloudu).

Tři stupně, které se sčítají:
- `keep`    – posledních N záloh (krátká historie pro rychlý návrat),
- `weekly`  – po jedné záloze z každého z posledních W týdnů, kde nějaká je,
- `monthly` – po jedné záloze z každého z posledních M měsíců, kde nějaká je.

Z období se nechává **nejstarší** záloha (první v týdnu / měsíci): jakmile jednou vznikne, už se nemění,
takže se v cloudu zbytečně nepřepisuje. Příklad „14 záloh zpět a měsíční po dobu roku“ = keep 14, monthly 12.
Čas zálohy se bere z názvu souboru `mtc-<server>-YYYYmmdd-HHMMSS.mtcbak` (mtime se při kopírování mění).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from .services import ServiceError

LIMITS = {"keep": (1, 365), "weekly": (0, 104), "monthly": (0, 120)}
LABELS = {"keep": "počet posledních záloh", "weekly": "počet týdenních záloh", "monthly": "počet měsíčních záloh"}


def stamp(name: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(name[-22:-7], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def number(key: str, v: Any) -> int:
    lo, hi = LIMITS[key]
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ServiceError(f"{LABELS[key]} {lo}–{hi}") from None
    if not lo <= n <= hi:
        raise ServiceError(f"{LABELS[key]} {lo}–{hi}")
    return n


def select(names: Iterable[str], keep: int, weekly: int = 0, monthly: int = 0) -> set[str]:
    """Které zálohy zůstanou. Soubory bez času v názvu se nikdy nemažou (nejsou naše)."""
    dated = sorted(((stamp(n), n) for n in names if stamp(n)), reverse=True)   # nejnovější první
    kept = {n for n in names if not stamp(n)}
    kept.update(n for _, n in dated[:max(1, keep)])
    for count, key in ((weekly, lambda d: tuple(d.isocalendar()[:2])), (monthly, lambda d: (d.year, d.month))):
        if count <= 0:
            continue
        first: dict[tuple, str] = {}
        for d, n in dated:                 # jde od nejnovější → poslední zápis období je jeho nejstarší záloha
            first[key(d)] = n
        for period in sorted(first, reverse=True)[:count]:
            kept.add(first[period])
    return kept


def describe(keep: int, weekly: int = 0, monthly: int = 0) -> str:
    parts = [f"posledních {keep}"]
    if weekly:
        parts.append(f"týdenní {weekly}×")
    if monthly:
        parts.append(f"měsíční {monthly}×")
    return ", ".join(parts)
