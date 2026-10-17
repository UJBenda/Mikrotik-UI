"""Abstrakce poskytovatele tunelů (koncentrátoru).

Dnes: fyzický MikroTik se SSTP serverem (`MikrotikProvider`), i víc kusů.
Později: accel-ppp + RADIUS, kde účty ověřuje přímo DB controlleru.
Na zařízeních v terénu se při výměně provideru nic nemění.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TunnelAccount:
    username: str  # mtc-<sériové číslo>
    password: str
    remote_address: str  # tunelová IP zařízení
    comment: str  # tenant/lokalita pro orientaci na koncentrátoru


class TunnelProvider(ABC):
    name: str

    @abstractmethod
    def ensure_account(self, account: TunnelAccount) -> None:
        """Založí nebo aktualizuje účet (idempotentně)."""

    @abstractmethod
    def disable_account(self, username: str) -> None:
        """Zakáže účet a odpojí aktivní relaci."""

    @abstractmethod
    def active_sessions(self) -> dict[str, str]:
        """username -> tunelová IP aktivních relací."""


def build_providers(settings) -> list[TunnelProvider]:
    from .mikrotik import MikrotikProvider

    if settings.tunnel.provider != "mikrotik":
        raise NotImplementedError("provider 'radius' zatím není implementovaný")
    return [MikrotikProvider.from_config(c) for c in settings.tunnel.concentrators]
