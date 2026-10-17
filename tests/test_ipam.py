import ipaddress

import pytest

from mtcontroller.ipam import (
    PoolExhausted, concentrator_address, next_device_address, next_tenant_block,
)

POOL = "10.200.0.0/16"


def test_concentrator_is_first_host():
    assert str(concentrator_address(POOL)) == "10.200.0.1"


def test_first_block_skips_concentrator():
    assert str(next_tenant_block(POOL, 24, [])) == "10.200.1.0/24"


def test_blocks_do_not_overlap():
    used = ["10.200.1.0/24", "10.200.2.0/23"]
    assert str(next_tenant_block(POOL, 24, used)) == "10.200.4.0/24"


def test_pool_exhausted():
    with pytest.raises(PoolExhausted):
        next_tenant_block("10.200.0.0/24", 24, [])


def test_device_address_fills_gaps_and_spills_to_next_block():
    blocks = ["10.200.1.0/30", "10.200.5.0/30"]  # /30 = 2 hosty
    assert str(next_device_address(blocks, [])) == "10.200.1.1"
    assert str(next_device_address(blocks, ["10.200.1.1/32"])) == "10.200.1.2"
    assert str(next_device_address(blocks, ["10.200.1.1", "10.200.1.2"])) == "10.200.5.1"
    with pytest.raises(PoolExhausted):
        next_device_address(blocks, ["10.200.1.1", "10.200.1.2", "10.200.5.1", "10.200.5.2"])


def test_hundreds_of_devices_fit():
    blocks = [str(next_tenant_block(POOL, 24, []))]
    used = []
    for _ in range(254):
        used.append(str(next_device_address(blocks, used)))
    assert len(set(used)) == 254
    assert all(ipaddress.ip_address(a) in ipaddress.ip_network(blocks[0]) for a in used)
