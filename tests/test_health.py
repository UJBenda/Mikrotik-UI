"""DHCP pooly a NTP: výpočty a alerty."""

from helpers import drop_db
from test_modules import _adopted, _env

from mtcontroller import alerts, health, inventory
from mtcontroller.db import operator_scope


def _lease(i, status="bound", server="dhcp1", net="192.168.88"):
    return {"address": f"{net}.{i}", "server": server, "status": status}


def test_pool_ranges_and_usage():
    assert health.pool_ranges("10.0.0.10-10.0.0.19, 10.0.1.0/30,10.0.2.5,garbage") == [
        (167772170, 167772179), (167772416, 167772419), (167772677, 167772677)]
    inv = {
        "dhcp_servers": [{"name": "dhcp1", "address-pool": "lan"}, {"name": "vpn", "address-pool": "tiny"},
                         {"name": "off", "address-pool": "lan", "disabled": "true"}],
        "ip_pools": [{"name": "lan", "ranges": "192.168.88.10-192.168.88.19", "next-pool": "lan2"},
                     {"name": "lan2", "ranges": "192.168.88.100-192.168.88.109", "next-pool": "lan"},  # cyklus
                     {"name": "tiny", "ranges": "10.9.9.1-10.9.9.2"}],
        "dhcp_leases": [_lease(i) for i in range(10, 20)] + [_lease(i) for i in range(100, 108)]
                       + [_lease(50), _lease(101, status="waiting"), _lease(15, server="jiny")],
    }
    p = health.dhcp_pools(inv)
    assert p == [{"server": "dhcp1", "pool": "lan", "size": 20, "used": 18, "pct": 90}]  # .50 mimo pool


def test_uptime_and_ntp():
    assert health.uptime_seconds("2w3d04:05:06") == 2 * 604800 + 3 * 86400 + 4 * 3600 + 5 * 60 + 6
    assert health.uptime_seconds("5d") == 5 * 86400 and health.uptime_seconds("00:10:00") == 600
    inv = {"ntp_client": {"enabled": "true", "status": "waiting"}, "resource": {"uptime": "3h"}}
    assert health.ntp_unsynced(inv) == "waiting"
    assert health.ntp_unsynced({**inv, "resource": {"uptime": "00:20:00"}}) is None  # těsně po startu
    assert health.ntp_unsynced({**inv, "ntp_client": {"enabled": "true", "status": "synchronized"}}) is None
    assert health.ntp_unsynced({**inv, "ntp_client": {"enabled": "false"}}) is None


def test_alerts_pool_full_and_ntp():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        inv = {"dhcp_servers": [{"name": "dhcp1", "address-pool": "lan"}],
               "ip_pools": [{"name": "lan", "ranges": "192.168.88.10-192.168.88.19"}],
               "dhcp_leases": [_lease(i) for i in range(10, 19)],
               "ntp_client": {"enabled": "true", "status": "stopped"}, "resource": {"uptime": "1d"}}
        inventory.store(db, tid, gw, inv, [])
        with operator_scope(db) as tx:
            kinds = {(a.kind, a.subject): a.message for a in alerts.conditions(tx)}
        assert "90 %" in kinds[("dhcp_pool_full", "dhcp1")] and ("ntp_unsynced", "") in kinds
        inv["dhcp_leases"] = inv["dhcp_leases"][:5]
        inv["ntp_client"]["status"] = "synchronized"
        inventory.store(db, tid, gw, inv, [])
        with operator_scope(db) as tx:
            kinds = {a.kind for a in alerts.conditions(tx)}
        assert "dhcp_pool_full" not in kinds and "ntp_unsynced" not in kinds
    finally:
        drop_db(name)
