import copy

from fixtures import iface, legacy_per_vlan_bridges, modern

from mtcontroller import analysis, ntp
from mtcontroller.web.devices import build_topology


def codes(rep):
    return {f.code for f in rep.findings}


def test_legacy_detected():
    rep = analysis.analyze_vlan(legacy_per_vlan_bridges())
    assert rep.status == "legacy"
    assert {"multiple_bridges", "vlan_iface_bridged", "vlan_on_physical", "hw_offload_note"} <= codes(rep)


def test_modern_conforming():
    rep = analysis.analyze_vlan(modern())
    assert rep.status == "conforming", rep.as_dict()
    assert not [f for f in rep.findings if f.severity in ("error", "warning")]


def test_bridge_not_tagged_is_error():
    inv = modern()
    inv["bridge_vlans"][0]["tagged"] = "ether1"
    rep = analysis.analyze_vlan(inv)
    assert "bridge_not_tagged" in codes(rep)


def test_ingress_filtering_off_warns():
    inv = modern()
    inv["bridge_ports"][1]["ingress-filtering"] = "false"
    assert "ingress_filtering_off" in codes(analysis.analyze_vlan(inv))


def test_nested_bridge():
    inv = modern()
    inv["bridges"].append({"name": "br2", "vlan-filtering": "false", "disabled": "false"})
    inv["bridge_ports"].append({"bridge": "bridge", "interface": "br2", "disabled": "false", "dynamic": "false"})
    assert "nested_bridge" in codes(analysis.analyze_vlan(inv))


def test_simple_flat_network():
    inv = modern()
    inv["vlans"], inv["bridge_vlans"] = [], []
    inv["bridges"][0]["vlan-filtering"] = "false"
    assert analysis.analyze_vlan(inv).status == "simple"


def test_unknown_without_inventory():
    assert analysis.analyze_vlan({}).status == "unknown"


def test_migration_plan_for_per_vlan_bridges():
    plan = analysis.plan_vlan_migration(legacy_per_vlan_bridges())
    assert plan.ok, plan.blockers
    assert plan.target_bridge == "bridge"
    s = plan.script
    # přístupové porty dostanou pvid, trunk je tagovaný v obou VLAN
    assert '/interface bridge port set [find interface="ether3"] bridge="bridge" pvid=10' in s
    assert 'vlan-ids=10 tagged="bridge,ether1" untagged="ether3"' in s
    assert 'vlan-ids=20 tagged="bridge,ether1" untagged="ether4"' in s
    # L3 a DHCP se přesunou na nové VLAN rozhraní na bridge
    assert '/interface vlan add name="vlan10" interface="bridge" vlan-id=10' in s
    assert '/ip address set [find address="10.0.10.1/24" interface="br-vlan10"] interface="vlan10"' in s
    assert '/ip dhcp-server set [find name="dhcp-v10"] interface="vlan10"' in s
    # staré objekty pryč, vlan-filtering až úplně nakonec
    assert '/interface bridge remove [find name="br-vlan10"]' in s
    assert s.rstrip().splitlines()[-2].startswith('/interface bridge set [find name="bridge"] vlan-filtering=yes')
    s.encode("ascii")


def test_migration_name_collision_avoided():
    inv = legacy_per_vlan_bridges()
    inv["interfaces"].append(iface("vlan10", "vlan"))
    inv["vlans"].append({"name": "vlan10", "vlan-id": "99", "interface": "ether2", "disabled": "false"})
    plan = analysis.plan_vlan_migration(inv)
    assert 'name="vlan10-br"' in plan.script


def test_migration_blocks_ambiguous_bridge():
    inv = legacy_per_vlan_bridges()
    inv["bridge_ports"].append({"bridge": "br-vlan10", "interface": "vlan20-e1", "disabled": "false", "dynamic": "false"})
    plan = analysis.plan_vlan_migration(inv)
    assert not plan.ok and any("víc VLAN" in b for b in plan.blockers)


def test_ntp_primary_and_client():
    primary = modern()
    client = copy.deepcopy(modern())
    client["addresses"] = [{"address": "10.0.10.20/24", "interface": "vlan10", "disabled": "false", "dynamic": "false"}]
    policy = ntp.validate_policy({"primary_device_id": "P", "upstream": ["tik.cesnet.cz"], "timezone": "Europe/Prague"})
    d, err = ntp.desired_for(policy, "P", primary, primary)
    assert err is None and d.server_enabled is True and d.client_servers == ["tik.cesnet.cz"]
    chg = ntp.changes(d, primary)
    assert {c["what"] for c in chg} == {"ntp_client", "ntp_server", "timezone"}
    sc = ntp.script(d, chg)
    assert '/system ntp server set enabled=yes' in sc and 'time-zone-name="Europe/Prague"' in sc
    dc, err = ntp.desired_for(policy, "C", client, primary)
    assert err is None and dc.client_servers == ["10.0.10.2", "tik.cesnet.cz"]
    assert dc.server_enabled is None  # u klienta NTP server neměníme
    assert "ntp_server" not in {c["what"] for c in ntp.changes(dc, client)}


def test_ntp_client_in_other_network_skipped():
    other = modern()
    other["addresses"] = [{"address": "172.16.0.5/24", "interface": "vlan10", "disabled": "false", "dynamic": "false"}]
    policy = ntp.validate_policy({"primary_device_id": "P"})
    d, err = ntp.desired_for(policy, "C", other, modern())
    assert d is None and "síti" in err


def test_ntp_no_changes_when_compliant():
    inv = modern()
    inv["ntp_client"] = {"enabled": "true", "servers": "tik.cesnet.cz,tak.cesnet.cz"}
    inv["ntp_server"] = {"enabled": "true"}
    inv["clock"] = {"time-zone-name": "Europe/Prague"}
    policy = ntp.validate_policy({"primary_device_id": "P"})
    d, _ = ntp.desired_for(policy, "P", inv, inv)
    assert ntp.changes(d, inv) == []


def test_ntp_policy_validation():
    import pytest

    for bad in ({"primary_device_id": "P", "upstream": ["bad host;rm"]},
                {"primary_device_id": "P", "timezone": "../../etc"},
                {"upstream": ["tik.cesnet.cz"]}):
        with pytest.raises(ntp.PolicyError):
            ntp.validate_policy(bad)


def test_topology_matches_neighbors_by_mac():
    a, b = modern(), legacy_per_vlan_bridges()
    a["neighbors"] = [{"interface": "ether1", "mac-address": "AA:00:00:00:00:01", "identity": "sw-b",
                       "interface-name": "ether1"},
                      {"interface": "ether2", "mac-address": "CC:CC:CC:00:00:01", "identity": "neznamy-ap",
                       "board": "cAP ax"}]
    devs = [{"id": "A", "serial": "SA", "identity": "gw-a", "state": "adopted", "model": "RB5009"},
            {"id": "B", "serial": "SB", "identity": "sw-b", "state": "adopted", "model": "CRS"}]
    t = build_topology(devs, {"A": a, "B": b})
    assert {n["id"] for n in t["nodes"]} == {"A", "B", "ext:CC:CC:CC:00:00:01"}
    e = next(e for e in t["edges"] if set((e["a"], e["b"])) == {"A", "B"})
    assert e["ports"] == {"A": "ether1", "B": "ether1"}
