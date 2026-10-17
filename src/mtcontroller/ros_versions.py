"""Známé problémy verzí RouterOS, které se týkají controlleru.

Udržuje se ručně podle changelogů a fóra MikroTiku. Controller zobrazuje
upozornění u zařízení a při adopci.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownIssue:
    first: str  # první postižená verze (včetně)
    fixed: str | None  # první opravená verze; None = zatím neopraveno
    severity: str  # "critical" | "warning" | "info"
    text: str
    source: str


KNOWN_ISSUES: list[KnownIssue] = [
    KnownIssue("7.24", "7.24.5", "critical",
               "Scheduler s výchozím datem/časem startu se nespouští. Controller to obchází "
               "explicitním startem, ale doporučena aktualizace.",
               "https://forum.mikrotik.com/t/7-24-5-stable-is-released/273494"),
    KnownIssue("7.24", None, "warning",
               "Hlášený únik paměti u skriptů ze scheduleru s velkým výstupem (SUP-222576). "
               "Check-in skript výstup potlačuje; sleduj volnou paměť.",
               "https://forum.mikrotik.com/t/7-24-stable-is-released/272381?page=8"),
]

MIN_SUPPORTED = "7.12"


def parse(version: str) -> tuple[int, ...]:
    """'7.20.1 (stable)' -> (7, 20, 1). Beta/rc se řadí jako nižší než finální verze."""
    m = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return (0,)
    return tuple(int(x) for x in m.groups(default="0"))


def channel(version: str) -> str:
    m = re.search(r"\(([a-z-]+)\)", version or "")
    return m.group(1) if m else ""


def issues_for(version: str) -> list[KnownIssue]:
    v = parse(version)
    out = []
    for i in KNOWN_ISSUES:
        if v >= parse(i.first) and (i.fixed is None or v < parse(i.fixed)):
            out.append(i)
    return out


def is_supported(version: str) -> bool:
    return parse(version) >= parse(MIN_SUPPORTED)
