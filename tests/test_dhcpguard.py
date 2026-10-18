"""Sítě lokality a DHCP snooping s důvěryhodnými porty podle cesty k DHCP serveru."""

import copy

from mtcontroller import dhcpguard


def _ports(names, bridge="bridge", trusted=()):
    return [{"bridge": bridge, "interface": n, "pvid": "1", "trusted": "true" if n in trusted else "false"} for n in names]


def _ifs(prefix, names, extra=()):
    return [{"name": n, "type": "ether", "mac-address": f"{prefix}:00:00:00:00:{k:02X}"} for k, n in enumerate(names, 1)] + list(extra)


GW = {"id": "gw", "identity": "GW", "model": "RB5009UPr+S+", "ros_version": "7.20"}
SW1 = {"id": "sw1", "identity": "SW1", "model": "CRS326-24G-2S+", "ros_version": "7.20"}
SW2 = {"id": "sw2", "identity": "SW2", "model": "RB750Gr3", "ros_version": "7.20"}
AP = {"id": "ap", "identity": "AP1", "model": "cAP ax", "ros_version": "7.20"}
DEVS = [GW, SW1, SW2, AP]

INVS = {
    "gw": {
        "interfaces": _ifs("A0", ["ether1", "ether2", "ether3"], [
            {"name": "bridge", "type": "bridge", "mac-address": "A0:00:00:00:00:02"},
            {"name": "vlan10", "type": "vlan", "mac-address": "A0:00:00:00:00:02"},
            {"name": "vlan20", "type": "vlan", "mac-address": "A0:00:00:00:00:02"},
            {"name": "vlan30", "type": "vlan", "mac-address": "A0:00:00:00:00:02"}]),
        "bridges": [{"name": "bridge", "vlan-filtering": "true", "protocol-mode": "rstp", "dhcp-snooping": "false"}],
        "bridge_ports": _ports(["ether2", "ether3"]),
        "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "10,20,30", "tagged": "bridge,ether2"}],
        "vlans": [{"name": "vlan10", "vlan-id": "10", "interface": "bridge", "comment": "LAN"},
                  {"name": "vlan20", "vlan-id": "20", "interface": "bridge"},
                  {"name": "vlan30", "vlan-id": "30", "interface": "bridge"}],
        "addresses": [{"address": "192.168.10.1/24", "interface": "vlan10"},
                      {"address": "192.168.20.1/24", "interface": "vlan20"},
                      {"address": "192.168.30.1/24", "interface": "vlan30"},
                      {"address": "81.2.3.4/24", "interface": "ether1", "dynamic": "true"}],
        "dhcp_client": [{"interface": "ether1", "status": "bound", "dhcp-server": "81.2.3.1"}],
        "dhcp_servers": [{"name": "dhcp10", "interface": "vlan10", "address-pool": "pool10"}],
        "ip_pools": [{"name": "pool10", "ranges": "192.168.10.10-192.168.10.109"}],
        "dhcp_leases": [{"address": "192.168.10.50", "server": "dhcp10", "status": "bound"}],
        "dhcp_relays": [{"name": "relay20", "interface": "vlan20", "dhcp-server": "192.168.30.10"}],
        "neighbors": [{"interface": "ether2,bridge", "identity": "SW1", "mac-address": "B0:00:00:00:00:01"}],
    },
    "sw1": {
        "interfaces": _ifs("B0", ["ether1", "ether2", "ether3"]),
        "bridges": [{"name": "bridge", "vlan-filtering": "true", "protocol-mode": "rstp"}],
        "bridge_ports": _ports(["ether1", "ether2", "ether3"]),
        "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "10,20,30", "tagged": "ether1,ether2"}],
        "neighbors": [{"interface": "ether1,bridge", "identity": "GW", "mac-address": "A0:00:00:00:00:02"},
                      {"interface": "ether2,bridge", "identity": "SW2", "mac-address": "C0:00:00:00:00:01"}],
    },
    "sw2": {
        "interfaces": _ifs("C0", ["ether1", "ether2", "ether5"]),
        "bridges": [{"name": "bridge", "vlan-filtering": "true", "protocol-mode": "rstp"}],
        "bridge_ports": _ports(["ether1", "ether2", "ether5"]),
        "neighbors": [{"interface": "ether1,bridge", "identity": "SW1", "mac-address": "B0:00:00:00:00:02"},
                      {"interface": "ether2,bridge", "identity": "AP1", "mac-address": "D0:00:00:00:00:01"}],
    },
    "ap": {
        "interfaces": _ifs("D0", ["ether1"], [{"name": "bridge", "type": "bridge"}]),
        "bridges": [{"name": "bridge", "protocol-mode": "rstp"}],
        "bridge_ports": _ports(["ether1", "wifi1"]),
        "dhcp_client": [{"interface": "bridge", "status": "bound", "dhcp-server": "192.168.10.1"}],
        "neighbors": [{"interface": "ether1", "identity": "SW2", "mac-address": "C0:00:00:00:00:02"}],
    },
}
# Windows DHCP server ve VLAN 30 na SW2 ether5 (z korelace klientů)
CLIENTS = {"192.168.30.10": {"ip": "192.168.30.10", "device_id": "sw2", "port": "ether5"}}


def _by(res):
    return {d["device_id"]: d for d in res["devices"]}


def test_networks():
    n = dhcpguard.site_networks(DEVS, INVS)
    rows = [(x["vlan_id"], x["name"], x["subnet"], x["dhcp"]["mode"]) for x in n["networks"]]
    assert rows == [(10, "vlan10", "192.168.10.0/24", "server"), (20, "vlan20", "192.168.20.0/24", "relay"),
                    (30, "vlan30", "192.168.30.0/24", None)]
    lan = n["networks"][0]
    assert lan["dhcp"]["used"] == 1 and lan["dhcp"]["size"] == 100 and lan["available"] == 99
    assert lan["router"] == "GW" and lan["comment"] == "LAN" and lan["devices"] == 2
    assert n["networks"][1]["dhcp"]["target"] == "192.168.30.10"
    assert {b["device"] for b in n["bridges"]} == {"GW", "SW1", "SW2", "AP1"}
    assert n["warnings"] == []


def test_trusted_ports_follow_path_to_dhcp():
    res = dhcpguard.site_snooping(DEVS, INVS, CLIENTS)
    assert res["missing"] == []
    by = _by(res)
    trusted = {k: [t["port"] for t in v["trusted"]] for k, v in by.items()}
    assert trusted == {"gw": ["ether2"], "sw1": ["ether1", "ether2"], "sw2": ["ether1", "ether5"], "ap": ["ether1"]}
    assert all(v["status"] == "plan" for v in by.values())
    gw = by["gw"]
    assert gw["alerts"] == ["vlan10"] and "DHCP alert na vlan10" in gw["summary"]
    a = gw["apply"].splitlines()
    # nejdřív trusted, pak snooping, pak alert
    assert a[0].endswith("trusted=yes") and "dhcp-snooping=yes" in a[1] and a[2].startswith("/ip dhcp-server alert add")
    assert 'valid-server=A0:00:00:00:00:02' in a[2] and 'comment="mtc"' in a[2]
    r = gw["revert"].splitlines()
    assert r[0].startswith("/ip dhcp-server alert remove") and "dhcp-snooping=no" in r[1] and r[2].endswith("trusted=no")
    # proč je port trusted
    sw2 = by["sw2"]["trusted"]
    assert any("192.168.30.10" in w for w in sw2[1]["why"])
    # HW snooping jen CRS3xx/5xx/CCR2116/2216
    assert by["sw1"]["warnings"] == [] and "CRS3xx" in by["sw2"]["warnings"][0]
    assert all(d["apply"].isascii() and d["revert"].isascii() for d in by.values())


def test_non_ascii_interface_blocks():
    invs = copy.deepcopy(INVS)
    invs["ap"]["bridge_ports"][0]["interface"] = "ether1-č"
    invs["ap"]["neighbors"][0]["interface"] = "ether1-č"
    by = _by(dhcpguard.site_snooping(DEVS, invs, CLIENTS))
    assert by["ap"]["status"] == "blocked" and by["gw"]["status"] == "plan"


def test_unknown_location_blocks_and_manual_source_fixes():
    res = dhcpguard.site_snooping(DEVS, INVS, {})
    assert [m["ip"] for m in res["missing"]] == ["192.168.30.10"]
    assert all(d["status"] == "blocked" for d in res["devices"])
    assert "zadej ho ručně" in res["devices"][0]["reasons"][-1]
    pol = {"sources": [{"device_id": "sw2", "port": "ether5", "label": "Windows DHCP", "ip": "192.168.30.10"}]}
    res = dhcpguard.site_snooping(DEVS, INVS, {}, pol)
    assert res["missing"] == [] and _by(res)["sw2"]["trusted"][1]["port"] == "ether5"


def test_missing_neighbor_blocks_only_that_device():
    invs = copy.deepcopy(INVS)
    invs["ap"]["neighbors"] = []
    invs["sw2"]["neighbors"] = invs["sw2"]["neighbors"][:1]
    by = _by(dhcpguard.site_snooping(DEVS, invs, CLIENTS))
    assert by["ap"]["status"] == "blocked" and "není známá" in by["ap"]["reasons"][0]
    assert by["sw2"]["status"] == "plan"


def test_already_configured_and_keep_existing_trusted():
    invs = copy.deepcopy(INVS)
    invs["sw1"]["bridges"][0]["dhcp-snooping"] = "true"
    invs["sw1"]["bridge_ports"] = _ports(["ether1", "ether2", "ether3"], trusted=("ether1", "ether2", "ether3"))
    by = _by(dhcpguard.site_snooping(DEVS, invs, CLIENTS))
    assert by["sw1"]["status"] == "ok" and by["sw1"]["keep"] == ["ether1", "ether2", "ether3"]
    assert "trusted=no" not in by["sw1"]["revert"]


def test_basis_and_policy_validation():
    b = dhcpguard.basis(INVS["sw1"])
    inv = copy.deepcopy(INVS["sw1"])
    inv["bridge_ports"][0]["trusted"] = "true"
    assert dhcpguard.basis(inv) != b
    pol = dhcpguard.validate_policy({"sources": [{"device_id": "sw2", "port": "ether5", "ip": "192.168.30.10"}]}, {"sw2"})
    assert pol["sources"][0]["label"] == "ruční zdroj" and pol["rogue_alerts"] is True
    for bad in ({"device_id": "x", "port": "ether1"}, {"device_id": "sw2", "port": "eth er"},
                {"device_id": "sw2", "port": "ether1", "ip": "1.2.3"}):
        try:
            dhcpguard.validate_policy({"sources": [bad]}, {"sw2"})
            raise AssertionError(bad)
        except ValueError:
            pass


def test_api_generate_approve(tmp_path=None):
    """Celý tok přes API a DB: politika, analýza, plány, schválení jen v režimu manage, alert."""
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import alerts, inventory
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        ids = {}
        for d, ip in ((GW, "10.200.1.2"), (SW1, "10.200.1.3"), (SW2, "10.200.1.4"), (AP, "10.200.1.5")):
            ids[d["id"]] = _adopted(db, box, tid, sid, "SN-" + d["identity"], ip, d["identity"])
        for k, inv in INVS.items():
            inv = dict(inv, identity={"name": next(d["identity"] for d in DEVS if d["id"] == k)})
            inventory.store(db, tid, ids[k], inv, [])
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/sites/{sid}"
        n = c.get(base + "/networks").json()
        assert [x["vlan_id"] for x in n["networks"]] == [10, 20, 30]
        assert [m["ip"] for m in n["snooping"]["missing"]] == ["192.168.30.10"]
        assert "apply" not in n["snooping"]["devices"][0]
        r = c.post(base + "/policy/dhcp_snooping", json_body={"sources": [
            {"device_id": ids["sw2"], "port": "ether5", "label": "Windows DHCP", "ip": "192.168.30.10"}]})
        assert r.status_code == 200, r.text
        assert c.post(base + "/policy/dhcp_snooping", json_body={"sources": [
            {"device_id": "00000000-0000-0000-0000-000000000000", "port": "ether5"}]}).status_code == 400
        g = c.post(base + "/plans/dhcp_snooping").json()
        planned = [d for d in g["devices"] if d.get("plan_id")]
        assert len(planned) == 4
        pid = next(d["plan_id"] for d in planned if d["device"] == "SW1")
        r = c.post(f"/api/admin/tenants/{tid}/plans/{pid}/approve")
        assert r.status_code == 400 and "observe" in r.json()["detail"]
        assert c.post(f"/api/admin/tenants/{tid}/devices/{ids['sw1']}/modules",
                      json_body={"module": "dhcp_snooping", "mode": "manage"}).status_code == 200
        r = c.post(f"/api/admin/tenants/{tid}/plans/{pid}/approve")
        assert r.status_code == 200, r.text
        with tenant_scope(db, tid) as tx:
            pl = tx.fetchone("SELECT status::text AS s, revert_script, confirm_minutes FROM config_plan WHERE id = %(i)s",
                             {"i": pid})
        assert pl["s"] == "queued" and "dhcp-snooping=no" in pl["revert_script"] and pl["confirm_minutes"]
        # cizí DHCP server nahlášený DHCP alertem → kritický alert
        inventory.store(db, tid, ids["gw"], dict(INVS["gw"], dhcp_alerts=[
            {"interface": "vlan10", "valid-server": "A0:00:00:00:00:02", "unknown-server": "E4:5F:01:00:00:09"}]), [])
        with operator_scope(db) as tx:
            found = [x for x in alerts.conditions(tx) if x.kind == "rogue_dhcp"]
        assert len(found) == 1 and "E4:5F:01:00:00:09" in found[0].message
        assert c.get(base + "/networks").json()["snooping"]["rogue"][0]["servers"] == "E4:5F:01:00:00:09"
    finally:
        drop_db(name)
