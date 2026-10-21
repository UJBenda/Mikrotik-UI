"""Regionální výchozí hodnoty (časová zóna, země pro Wi-Fi, NTP) z `[site_defaults]`.

Nastaví se jednou při startu služby (`apply`); moduly je čtou místo natvrdo
zadaných hodnot. Bez `apply` platí výchozí hodnoty pro Česko.
"""

from __future__ import annotations

from .config import SiteDefaults

_current = SiteDefaults()


def apply(settings) -> None:
    global _current
    _current = settings.site_defaults


def timezone() -> str:
    return _current.timezone


def country() -> str:
    return _current.country


def ntp_servers() -> list[str]:
    return list(_current.ntp)
