"""Výrobce podle MAC adresy (IEEE registry z Debian balíku `ieee-data`).

Soubory /usr/share/ieee-data/{oui,mam,oui36}.csv (MA-L 24 bitů, MA-M 28, MA-S 36).
Načtou se líně při prvním dotazu. Lokálně administrovaná MAC (druhý bit prvního
bajtu) je typicky náhodná „soukromá“ adresa telefonu/notebooku – výrobce nemá.
"""

from __future__ import annotations

import csv
import re
import threading
from pathlib import Path

DEFAULT_DIR = Path("/usr/share/ieee-data")
FILES = ("oui.csv", "mam.csv", "oui36.csv")
RANDOM = "náhodná MAC (soukromá adresa)"

_lock = threading.Lock()
_db: dict[str, str] | None = None
_dir = DEFAULT_DIR


def configure(directory: Path | str) -> None:
    """Jiný adresář s registry (testy, vlastní kopie)."""
    global _db, _dir
    with _lock:
        _dir, _db = Path(directory), None


def _load() -> dict[str, str]:
    global _db
    with _lock:
        if _db is None:
            db: dict[str, str] = {}
            for name in FILES:
                p = _dir / name
                if not p.exists():
                    continue
                with p.open(encoding="utf-8", errors="replace", newline="") as f:
                    for row in csv.reader(f):
                        if len(row) >= 3 and re.fullmatch(r"[0-9A-Fa-f]{6,9}", row[1] or ""):
                            db[row[1].upper()] = row[2].strip()
            _db = db
        return _db


def vendor(mac: str | None) -> str | None:
    h = re.sub(r"[^0-9A-Fa-f]", "", str(mac or "")).upper()
    if len(h) != 12:
        return None
    if int(h[1], 16) & 0x2:
        return RANDOM
    db = _load()
    for n in (9, 7, 6):
        v = db.get(h[:n])
        if v:
            return v
    return None


def available() -> bool:
    return bool(_load())
