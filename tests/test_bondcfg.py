"""LACP a MLAG: skripty s návratem, přenos VLAN z portů na bond."""

import pytest

from test_routeros import _balanced

from mtcontroller import bondcfg, portcfg

INV = {
    "interfaces": [{"name": f"ether{i}", "type": "ether"} for i in range(1, 9)] + [{"name": "sfp1", "type": "ether"}],
    "ethernet": [{"name": f"ether{i}"} for i in range(1, 9)],
    "bridges": [{"name": "bridge", "vlan-filtering": "true", "protocol-mode": "rstp", "priority": "0x8000"}],
    "bridge_ports": [{"bridge": "bridge", "interface": f"ether{i}", "pvid": "10" if i in (5, 6) else "1", "edge": "auto",
                      "frame-types": "admit-all"} for i in range(1, 9)] + [{"bridge": "bridge", "interface": "sfp1", "pvid": "1"}],
    "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "20", "tagged": "ether1,ether5,ether6"},
                     {"bridge": "bridge", "vlan-ids": "30", "tagged": "ether1,sfp1"},
                     {"bridge": "bridge", "vlan-ids": "40,41", "tagged": "ether1"}],
    "neighbors": [{"interface": "sfp1", "identity": "SW-B"}],
}


def test_lacp_moves_vlans_to_bond_and_reverts():
    p = bondcfg.lacp_plan(INV, ["ether6", "ether5"])
    a = p["apply"].splitlines()
    assert a[0] == '/interface bridge vlan set [find bridge="bridge" vlan-ids=20 dynamic=no] tagged="ether1" untagged=""'
    assert '/interface bridge port remove [find interface="ether5" dynamic=no]' in a
    assert ('/interface bonding add name="bond-mtc1" slaves="ether5,ether6" mode=802.3ad lacp-rate=30secs '
            'transmit-hash-policy=layer-2-and-3 comment="mtc"') in a
    assert any(x.startswith('/interface bridge port add bridge="bridge" interface="bond-mtc1" pvid=10') for x in a)
    assert a[-1] == '/interface bridge vlan set [find bridge="bridge" vlan-ids=20 dynamic=no] tagged="ether1,bond-mtc1" untagged=""'
    r = p["revert"].splitlines()
    assert r.index('/interface bridge port remove [find interface="bond-mtc1"]') < \
        r.index('/interface bonding remove [find name="bond-mtc1" comment="mtc"]') < \
        next(i for i, x in enumerate(r) if x.startswith('/interface bridge port add bridge="bridge" interface="ether5"'))
    assert r[-1] == '/interface bridge vlan set [find bridge="bridge" vlan-ids=20 dynamic=no] tagged="ether1,ether5,ether6" untagged=""'
    assert 'frame-types="admit-all"' in p["revert"] and _balanced(p["apply"]) and p["apply"].isascii()
    assert "LACP" in p["summary"]


def test_lacp_guards():
    with pytest.raises(portcfg.PortError):
        bondcfg.lacp_plan(INV, ["ether1", "ether2"])  # ether1 je v řádku "40,41"
    with pytest.raises(portcfg.PortError):
        bondcfg.lacp_plan(INV, ["ether2"])  # jeden port
    with pytest.raises(portcfg.PortError):
        bondcfg.lacp_plan(INV, ["ether2", "ether9"])
    inv = dict(INV, bonds=[{"name": "bond-mtc1", "slaves": "ether3,ether4", "comment": "mtc"}])
    with pytest.raises(portcfg.PortError):
        bondcfg.lacp_plan(inv, ["ether3", "ether7"])
    assert bondcfg.free_name(inv) == "bond-mtc2"
    p = bondcfg.lacp_plan(INV, ["ether2", "ether3"], uplinks={"ether2"})
    assert any("uplink" in w for w in p["warnings"])


def test_mlag_new_and_old_syntax():
    p = bondcfg.mlag_plan(INV, "7.22.1 (stable)", "sfp1", [{"ports": ["ether7"], "mlag_id": 10}], primary=True)
    a = p["apply"]
    assert '/interface bridge set [find name="bridge"] mlag-peer-port="sfp1" mlag-priority=50' in a
    assert '/interface bridge vlan set [find bridge="bridge" vlan-ids=20 dynamic=no] tagged="ether1,ether5,ether6,sfp1"' in a
    assert 'vlan add bridge="bridge" vlan-ids=10 tagged="sfp1" comment="mtc"' in a  # PVID 10 bez řádku
    assert 'name="bond-mtc-mlag10" slaves="ether7" mode=802.3ad lacp-rate=1sec' in a and "mlag-id=10" in a
    assert any("40,41" in w for w in p["warnings"])
    r = p["revert"].splitlines()
    # nejdřív pryč bond, pak MLAG z bridge, nakonec VLAN peer portu
    i_bond = r.index('/interface bonding remove [find name="bond-mtc-mlag10" comment="mtc"]')
    i_mlag = next(i for i, x in enumerate(r) if "mlag-peer-port=" in x)
    i_vlan = r.index('/interface bridge vlan remove [find bridge="bridge" vlan-ids=10 comment="mtc"]')
    assert i_bond < i_mlag < i_vlan and 'mlag-peer-port="none" mlag-priority=128' in r[i_mlag]
    old = bondcfg.mlag_plan(INV, "7.16.2", "sfp1", [{"ports": ["ether7", "ether8"], "mlag_id": 11}], primary=False)
    assert '/interface bridge mlag set bridge="bridge" peer-port="sfp1" priority=128' in old["apply"]
    assert '/interface bridge mlag set bridge="none" peer-port="none"' in old["revert"]
    for s in (a, p["revert"], old["apply"], old["revert"]):
        assert _balanced(s) and s.isascii()


def test_mlag_guards_and_pair():
    with pytest.raises(portcfg.PortError):
        bondcfg.mlag_plan(INV, "7.8", "sfp1", [{"ports": ["ether7"], "mlag_id": 1}], primary=True)
    with pytest.raises(portcfg.PortError):
        bondcfg.mlag_plan(INV, "7.22", "sfp1", [{"ports": ["sfp1"], "mlag_id": 1}], primary=True)
    with pytest.raises(portcfg.PortError):
        bondcfg.mlag_plan(INV, "7.22", "sfp1", [{"ports": ["ether7"], "mlag_id": 1}, {"ports": ["ether8"], "mlag_id": 1}],
                          primary=True)
    nf = dict(INV, bridges=[dict(INV["bridges"][0], **{"vlan-filtering": "false"})])
    with pytest.raises(portcfg.PortError):
        bondcfg.mlag_plan(nf, "7.22", "sfp1", [{"ports": ["ether7"], "mlag_id": 1}], primary=True)
    l3 = dict(INV, switch=[{"name": "switch1", "l3-hw-offloading": "true"}])
    with pytest.raises(portcfg.PortError):
        bondcfg.mlag_plan(l3, "7.22", "sfp1", [{"ports": ["ether7"], "mlag_id": 1}], primary=True)
    b = dict(INV, bridges=[dict(INV["bridges"][0], priority="0x1000")], neighbors=[])
    w = bondcfg.pair_checks(INV, "sfp1", b, "sfp1", "SW-A", "SW-B")
    assert any("priorita" in x for x in w) and not any("spojení" in x for x in w)  # A vidí B na sfp1
    assert any("spojení" in x for x in bondcfg.pair_checks(b, "sfp1", b, "sfp1", "SW-A", "SW-B"))


def test_remove_bond():
    inv = dict(INV, bonds=[{"name": "bond-mtc1", "slaves": "ether5,ether6", "comment": "mtc", "lacp-rate": "30secs"}],
               bridge_ports=[b for b in INV["bridge_ports"] if b["interface"] not in ("ether5", "ether6")]
               + [{"bridge": "bridge", "interface": "bond-mtc1", "pvid": "10"}],
               bridge_vlans=[{"bridge": "bridge", "vlan-ids": "20", "tagged": "ether1,bond-mtc1"}])
    p = bondcfg.remove_plan(inv, "bond-mtc1")
    assert 'tagged="ether1,ether5,ether6"' in p["apply"] and 'interface="ether5" pvid=10' in p["apply"]
    assert 'bonding add name="bond-mtc1" slaves="ether5,ether6"' in p["revert"]
    with pytest.raises(portcfg.PortError):
        bondcfg.remove_plan(dict(inv, bonds=[{"name": "bond-mtc1", "slaves": "ether5", "comment": ""}]), "bond-mtc1")
    assert bondcfg.summarize_bonds(inv)[0]["slaves"] == ["ether5", "ether6"]


def test_lacp_trunk_on_non_adjacent_ports_with_vlans():
    """Porty nemusí být vedle sebe (ether2 + ether7 + sfp1); bond rovnou jako trunk:
    porty z bridge → bonding 802.3ad → bond do bridge → VLAN na bondu. Návrat v opačném pořadí."""
    p = bondcfg.lacp_plan(INV, ["sfp1", "ether7", "ether2"], vlan={"pvid": 99, "tagged": ["20", "30", "50"]})
    a = p["apply"].splitlines()
    i_rm = max(i for i, x in enumerate(a) if x.startswith("/interface bridge port remove"))
    i_bond = next(i for i, x in enumerate(a) if x.startswith("/interface bonding add"))
    i_bp = next(i for i, x in enumerate(a) if 'interface bridge port add bridge="bridge" interface="bond-mtc1"' in x)
    i_vlan = next(i for i, x in enumerate(a) if "pvid=99" in x)
    assert i_rm < i_bond < i_bp < i_vlan
    assert 'slaves="ether2,ether7,sfp1" mode=802.3ad' in a[i_bond]
    # VLAN 30 měl sfp1 → přešla na bond; 20 a 50 se přidají; 50 je nová řádka
    assert '/interface bridge vlan set [find bridge="bridge" vlan-ids=20 dynamic=no] tagged="ether1,ether5,ether6,bond-mtc1"' in a
    assert '/interface bridge vlan add bridge="bridge" vlan-ids=50 tagged="bond-mtc1" comment="mtc"' in a
    r = p["revert"].splitlines()
    assert r[0].startswith('/interface bridge vlan remove [find bridge="bridge" vlan-ids=50')
    assert r.index('/interface bonding remove [find name="bond-mtc1" comment="mtc"]') > r.index(
        next(x for x in r if "pvid=1" in x and "bond-mtc1" in x))
    assert "PVID" in p["summary"] and _balanced(p["apply"]) and p["apply"].isascii()


def test_bond_edited_like_port():
    """Existující bond: PVID / tagované VLAN / vypnutí jako u portu; PoE a popis ne."""
    inv = dict(INV)
    inv["interfaces"] = INV["interfaces"] + [{"name": "bond-mtc1", "type": "bond"}]
    inv["bonds"] = [{"name": "bond-mtc1", "slaves": "ether3,ether4", "comment": "mtc", "mode": "802.3ad"}]
    inv["bridge_ports"] = [b for b in INV["bridge_ports"] if b["interface"] not in ("ether3", "ether4")] + [
        {"bridge": "bridge", "interface": "bond-mtc1", "pvid": "1"}]
    st = portcfg.port_state(inv, "bond-mtc1")
    assert st["is_bond"] and st["slaves"] == ["ether3", "ether4"] and st["poe"] is None
    p = portcfg.plan(inv, "bond-mtc1", {"pvid": 10, "tagged": ["20"], "disabled": True})
    assert '/interface bonding set [find name="bond-mtc1"] disabled=yes' in p["apply"]
    assert '/interface bridge port set [find interface="bond-mtc1" dynamic=no] pvid=10' in p["apply"]
    assert 'tagged="ether1,ether5,ether6,bond-mtc1"' in p["apply"]
    with pytest.raises(portcfg.PortError):
        portcfg.plan(inv, "bond-mtc1", {"poe": "off"})
    with pytest.raises(portcfg.PortError):
        portcfg.plan(inv, "bond-mtc1", {"comment": "jine"})
