"""Přidělování tunelových adres.

Pool (např. 10.200.0.0/16) se dělí na bloky pro tenanty (výchozí /24).
Zařízení dostane první volnou adresu z bloků svého tenanta. První adresa
celého poolu je rezervovaná pro koncentrátor (local-address PPP).
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

Net = ipaddress.IPv4Network
Addr = ipaddress.IPv4Address


class PoolExhausted(RuntimeError):
    pass


def concentrator_address(pool: str) -> Addr:
    return next(ipaddress.ip_network(pool).hosts())


def next_tenant_block(pool: str, prefix: int, used: Iterable[str]) -> Net:
    net = ipaddress.ip_network(pool)
    taken = {ipaddress.ip_network(b) for b in used}
    for block in net.subnets(new_prefix=prefix):
        if any(block.overlaps(t) for t in taken):
            continue
        # první blok obsahuje adresu koncentrátoru – přeskočit
        if concentrator_address(pool) in block:
            continue
        return block
    raise PoolExhausted(f"v poolu {pool} už není volný blok /{prefix}")


def next_device_address(blocks: Iterable[str], used: Iterable[str]) -> Addr:
    taken = {ipaddress.ip_address(a.split("/")[0]) for a in used}
    for b in blocks:
        for host in ipaddress.ip_network(b).hosts():
            if host not in taken:
                return host
    raise PoolExhausted("tenant nemá volnou tunelovou adresu, přidělte další blok")
