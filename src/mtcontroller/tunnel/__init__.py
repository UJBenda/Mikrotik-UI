"""Abstrakce poskytovatele tunelů (koncentrátoru).

Dnes: fyzický MikroTik se SSTP serverem (`MikrotikProvider`).
Později: accel-ppp + RADIUS, kde účty ověřuje přímo DB controlleru.
Na zařízeních v terénu se při výměně provideru nic nemění.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TunnelAccount:
    username: str  # = sériové číslo zařízení
    password: str
    remote_address: str  # tunelová IP zařízení
    comment: str  # tenant/lokalita pro orientaci na koncentrátoru


class TunnelProvider(ABC):
    @abstractmethod
    async def ensure_account(self, account: TunnelAccount) -> None:
        """Založí nebo aktualizuje účet (idempotentně)."""

    @abstractmethod
    async def disable_account(self, username: str) -> None:
        """Zakáže účet a odpojí aktivní relaci."""

    @abstractmethod
    async def active_sessions(self) -> dict[str, str]:
        """username -> tunelová IP aktivních relací."""
