"""VLAN lokality na adoptovaném switchi: plán s návratem, uplink podle brány, profil „všechny VLAN“."""

import pytest

from test_routeros import _balanced

from mtcontroller import portcfg, profiles, vlanprep
from mtcontroller.services import ServiceError

INV = {
    "bridges": [{"name": "bridge", "vlan-filtering": "false"}],
    "bridge_ports": [{"bridge": "bridge", "interface": f"ether{i}", "pvid": "1"} for i in range(1, 11)],
    "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "1", "untagged": "bridge,ether1", "dynamic": "true"},
                     {"bridge": "bridge", "vlan-ids": "30", "tagged": "ether5"}],
    "vlans": [{"name": "mgmt", "vlan-id": "99", "interface": "bridge"}],
    "addresses": [{"address": "10.99.0.5/24", "interface": "mgmt"}],
    "routes_default": [{"dst-address": "0.0.0.0/0", "gateway": "10.99.0.1", "routing-table": "main", "active": "true"}],
    "arp": [{"address": "10.99.0.1", "mac-address": "aa:bb:cc:00:00:01", "interface": "mgmt"}],
    "bridge_hosts": [{"bridge": "bridge", "mac-address": "AA:BB:CC:00:00:01", "on-interface": "ether1"}],
}
INTENT = {"networks": [{"vlan_id": 1, "name": "LAN"}, {"vlan_id": 20, "name": "Kamery"},
                       {"vlan_id": 30, "name": "Hosté"}, {"vlan_id": 99, "name": "Správa"}]}


def test_status_lists_missing_vlans():
    st = vlanprep.status(INV, INTENT)
    assert st == {"bridge": "bridge", "site_vlans": [1, 20, 30, 99], "missing": [20, 99], "vlan_filtering": False}
    assert vlanprep.status(INV, None) is None


def test_plan_tags_uplink_and_enables_filtering_last():
    assert vlanprep.gateway_ports(INV, "bridge") == {"ether1"}
    assert vlanprep.mgmt_vlans(INV, "bridge") == {99}
    p = vlanprep.plan(INV, INTENT, {"ether9"})
    a = p["apply"].splitlines()
    assert a[0] == '/interface bridge vlan add bridge="bridge" vlan-ids=20 tagged="ether1,ether9" comment="mtc"'
    assert '/interface bridge vlan set [find bridge="bridge" vlan-ids=30 dynamic=no] tagged="ether5,ether1,ether9"' in a
    # správa jde přes VLAN rozhraní → bridge sám je tagovaný člen
    assert '/interface bridge vlan add bridge="bridge" vlan-ids=99 tagged="ether1,ether9,bridge" comment="mtc"' in a
    assert not any("vlan-ids=1 " in x for x in a)
    assert a[-1] == '/interface bridge set [find name="bridge"] vlan-filtering=yes'
    r = p["revert"].splitlines()
    assert r[0] == '/interface bridge set [find name="bridge"] vlan-filtering=no'
    assert '/interface bridge vlan remove [find bridge="bridge" vlan-ids=20 comment="mtc"]' in r
    assert '/interface bridge vlan set [find bridge="bridge" vlan-ids=30 dynamic=no] tagged="ether5"' in r
    assert _balanced(p["apply"]) and p["apply"].isascii() and p["revert"].isascii()
    assert "vlan-filtering" in p["summary"] and any("PVID 1" in w for w in p["warnings"])


def test_plan_guards_and_no_uplink():
    with pytest.raises(portcfg.PortError):
        vlanprep.plan(INV, {"networks": []})
    with pytest.raises(portcfg.PortError):
        vlanprep.plan({"bridges": [], "bridge_ports": []}, INTENT)
    inv = {**INV, "bridge_hosts": [], "vlans": [], "bridges": [{"name": "bridge", "vlan-filtering": "true"}]}
    p = vlanprep.plan(inv, INTENT)
    assert '/interface bridge vlan add bridge="bridge" vlan-ids=20 tagged="" comment="mtc"' in p["apply"].splitlines()
    assert "vlan-filtering" not in p["apply"] and any("uplink nepoznán" in w for w in p["warnings"])
    # vše už je → plán by nic neudělal
    done = {**inv, "bridge_vlans": [{"bridge": "bridge", "vlan-ids": str(v)} for v in (20, 30, 99)]}
    with pytest.raises(portcfg.PortError):
        vlanprep.plan(done, INTENT)


def test_all_vlans_profile():
    assert profiles.clean({"name": "T", "pvid": 1, "tagged": "*"})["tagged"] == "*"
    assert profiles.clean({"name": "T", "tagged": ["vše"]})["tagged"] == "*"
    with pytest.raises(ServiceError):
        profiles.clean({"name": "T", "tagged": "*,10"})
    row = profiles._row({"id": 1, "name": profiles.TRUNK_NAME, "pvid": 1, "tagged": "*", "poe": None, "edge": None,
                         "builtin": True})
    assert row["all_vlans"] and row["builtin"] and row["tagged"] == ["*"]
    assert profiles.expand(row, [1, 20, 30])["tagged"] == ["20", "30"]
    assert profiles.desired(row, None, [1, 20, 30])[0] == {"pvid": 1, "tagged": ["20", "30"]}
    assert "lokalit" in (profiles.desired(row, None, [])[1] or "")
    assert profiles.matching([row], {"pvid": 1, "tagged": ["30", "20"]}, [1, 20, 30])["id"] == 1
    assert profiles.matching([row], {"pvid": 1, "tagged": []}, []) is None


def test_api_detail_banner_and_plan():
    from helpers import drop_db
    from test_flow import _login
    from test_portcfg import _setup

    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sw = _setup()
    try:
        with operator_scope(db) as tx:
            sid = tx.fetchone("SELECT site_id FROM device WHERE id = %(d)s", {"d": str(sw)})["site_id"]
            tx.execute("""INSERT INTO site_policy (tenant_id, site_id, module, data) VALUES (%(t)s, %(s)s, 'intent', %(d)s)""",
                       {"t": str(tid), "s": str(sid), "d": {"networks": [{"vlan_id": 1, "name": "LAN"},
                                                                        {"vlan_id": 10, "name": "Kamery"},
                                                                        {"vlan_id": 60, "name": "Hosté"}]}})
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        d = c.get(f"{T}/devices/{sw}").json()
        assert d["site_vlans"]["missing"] == [60] and d["site_vlans"]["vlan_filtering"]
        st = c.get(f"{T}/devices/{sw}/ports/ether2").json()
        assert "60" in st["known_vlans"] and st["site_vlans"] == ["1", "10", "60"]
        r = c.post(f"{T}/devices/{sw}/bonds", json_body={"kind": "site_vlans"})
        assert r.status_code == 201, r.text
        with operator_scope(db) as tx:
            sc = tx.fetchone("SELECT script FROM config_plan WHERE id = %(i)s", {"i": r.json()["plan_id"]})["script"]
        # ether1 = STP root port → uplink
        assert 'vlan add bridge="bridge" vlan-ids=60 tagged="ether1" comment="mtc"' in sc
        # vestavěný trunk na portu: PVID 1 + všechny sítě kromě 1
        trunk = c.get(f"{T}/port-profiles").json()[0]
        r = c.post(T + "/ports/bulk", json_body={"targets": [{"device_id": sw, "ports": ["ether2"]}], "profile_id": trunk["id"]})
        res = r.json()["results"][0]
        assert r.status_code == 201 and "error" not in res, r.text
        with operator_scope(db) as tx:
            sc = tx.fetchone("SELECT script FROM config_plan WHERE id = %(i)s", {"i": res["plan_id"]})["script"]
        assert 'vlan-ids=10 dynamic=no] tagged="ether1,bridge,ether2"' in sc
        assert 'vlan add bridge="bridge" vlan-ids=60 tagged="ether2" comment="mtc"' in sc
    finally:
        drop_db(name)
