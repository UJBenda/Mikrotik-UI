"""Bezpečnostní audit konfigurace: nálezy, sběr bez tajemství, alert, počty v seznamu."""

from helpers import drop_db
from test_flow import _login
from test_inventory_backup import FakeApi
from test_modules import _adopted, _env

from mtcontroller import alerts, hardening, inventory
from mtcontroller.db import operator_scope

DEFCONF = {  # výchozí domácí konfigurace RouterOS v7 (zjednodušeně)
    "addresses": [{"address": "192.168.88.1/24", "interface": "bridge"},
                  {"address": "81.2.3.4/24", "interface": "ether1", "dynamic": "true"}],
    "fw_filter": [{"chain": "input", "action": "accept", "connection-state": "established,related,untracked"},
                  {"chain": "input", "action": "drop", "connection-state": "invalid"},
                  {"chain": "input", "action": "accept", "protocol": "icmp"},
                  {"chain": "input", "action": "drop", "in-interface-list": "!LAN"}],
    "ip_services": [{"name": n, "disabled": "false", "address": ""} for n in ("telnet", "ftp", "www", "ssh", "winbox", "api")]
                   + [{"name": "www-ssl", "disabled": "false", "address": "10.200.0.1/32"}],
    "users": [{"name": "admin", "group": "full", "disabled": "false"}, {"name": "mtc-ctl", "group": "mtc"}],
    "mac_server": {"allowed-interface-list": "LAN"}, "mac_winbox": {"allowed-interface-list": "LAN"},
    "neighbor_discovery": {"discover-interface-list": "LAN"}, "dns": {"allow-remote-requests": "true"},
    "snmp": {"enabled": "false"}, "socks": {"enabled": "false"}, "proxy": {"enabled": "false"},
    "bw_server": {"enabled": "true"}, "romon": {"enabled": "false"},
}


def _codes(inv):
    return {f["code"]: f["severity"] for f in hardening.audit(inv)}


def test_defconf_has_no_critical():
    c = _codes(DEFCONF)
    assert not any(v == "critical" for v in c.values())
    assert c["service_telnet"] == "warning" and c["service_www"] == "warning" and c["service_api"] == "warning"
    assert c["default_admin"] == "info" and c["bw_server"] == "info"
    assert "service_www-ssl_open" not in c  # omezené na správu
    assert c["service_ssh_open"] == "info"  # veřejná IP, ssh odkudkoli


def test_exposed_router_is_critical():
    inv = dict(DEFCONF, fw_filter=[{"chain": "input", "action": "drop", "protocol": "tcp", "dst-port": "23"},
                                   {"chain": "input", "action": "drop", "connection-state": "invalid"},
                                   {"chain": "forward", "action": "drop"},
                                   {"chain": "input", "action": "drop", "disabled": "true"}],
               mac_server={"allowed-interface-list": "all"}, snmp={"enabled": "true"},
               snmp_communities=[{"is_public": True, "open": True, "disabled": "false"}],
               socks={"enabled": "true"})
    c = _codes(inv)
    assert c["no_input_firewall"] == "critical" and c["open_resolver"] == "critical"
    assert c["mac_server_all"] == "warning" and c["snmp_public"] == "warning" and c["socks_enabled"] == "warning"
    assert hardening.audit(inv)[0]["severity"] == "critical"  # kritické první
    # bez veřejné IP (za NATem) to kritické není
    priv = dict(inv, addresses=[{"address": "192.168.88.1/24", "interface": "bridge"},
                                {"address": "100.64.1.2/10", "interface": "ether1"}])
    assert "no_input_firewall" not in _codes(priv)


def test_collect_snmp_community_never_stored():
    responses = {s.path: [] for s in inventory.SECTIONS}
    responses["/snmp/community"] = [{"name": "public", "addresses": "0.0.0.0/0"},
                                    {"name": "TajnaKomunita42", "addresses": "10.0.0.5/32"}]
    data, errors = inventory.collect(FakeApi(responses))
    assert data["snmp_communities"] == [{"is_public": True, "disabled": None, "open": True},
                                        {"is_public": False, "disabled": None, "open": False}]
    assert "TajnaKomunita42" not in str(data)


def test_alert_and_device_list_count():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        inv = dict(DEFCONF, fw_filter=[])
        inventory.store(db, tid, gw, inv, [])
        with operator_scope(db) as tx:
            found = {(a.kind, a.subject) for a in alerts.conditions(tx)}
        assert ("security_exposed", "no_input_firewall") in found and ("security_exposed", "open_resolver") in found
        c = _login(app)
        d = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert d["security_issues"] == 2 + 4  # 2 kritické + telnet, ftp, www, api
        det = c.get(f"/api/admin/tenants/{tid}/devices/{gw}").json()
        assert det["security"][0]["code"] == "no_input_firewall" and det["security"][0]["fix"]
        inventory.store(db, tid, gw, DEFCONF, [])
        with operator_scope(db) as tx:
            assert not any(a.kind == "security_exposed" for a in alerts.conditions(tx))
    finally:
        drop_db(name)
