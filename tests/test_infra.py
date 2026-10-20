"""Infrastruktura: strom od gateway, porty na spojích, WAN (aktivní/záloha), STP root."""

from mtcontroller import infra

GW, SW, AP, LOST = "gw", "sw", "ap", "lost"
DEVS = [{"id": GW, "identity": "core-gw", "role": "gateway", "state": "adopted", "model": "RB5009"},
        {"id": SW, "identity": "sw-hala", "role": "switch", "state": "adopted", "model": "CRS328"},
        {"id": AP, "identity": "ap-hala", "role": "ap", "state": "offline", "model": "cAP ax"},
        {"id": LOST, "identity": "osamely", "role": "unknown", "state": "adopted"}]


def _if(name, mac, running="true"):
    return {"name": name, "type": "ether", "mac-address": mac, "running": running, "disabled": "false"}


INVS = {
    GW: {"interfaces": [_if("ether1", "AA:00:00:00:00:01"), _if("ether2", "AA:00:00:00:00:02", "false"),
                        _if("ether3", "AA:00:00:00:00:03")],
         "neighbors": [{"interface": "bridge/ether3", "mac-address": "BB:00:00:00:00:18", "identity": "sw-hala"},
                       {"interface": "ether1", "mac-address": "11:22:33:44:55:66", "identity": "isp-modem"}],
         "routes_default": [{"dst-address": "0.0.0.0/0", "gateway": "81.2.3.1", "immediate-gw": "81.2.3.1%ether1",
                             "distance": "1", "active": "true"},
                            {"dst-address": "0.0.0.0/0", "gateway": "192.168.8.1", "immediate-gw": "192.168.8.1%ether2",
                             "distance": "2", "active": "false"},
                            {"dst-address": "0.0.0.0/0", "gateway": "10.9.9.9", "routing-table": "vpn"}],
         "dhcp_client": [{"interface": "ether2", "address": "192.168.8.100/24", "gateway": "192.168.8.1", "status": "bound"}],
         "addresses": [{"address": "81.2.3.4/24", "interface": "ether1"}],
         "resource": {"cpu-load": "19", "free-memory": "330", "total-memory": "1000"}},
    SW: {"interfaces": [_if("ether1", "BB:00:00:00:00:01"), _if("ether24", "BB:00:00:00:00:18"),
                        _if("sfp-sfpplus1", "BB:00:00:00:00:19", "false")],
         "neighbors": [{"interface": "ether24", "mac-address": "AA:00:00:00:00:03", "identity": "core-gw"},
                       {"interface": "ether1", "mac-address": "CC:00:00:00:00:01", "identity": "ap-hala"}],
         "ethernet_monitor": [{"name": "ether1", "status": "link-ok", "rate": "100Mbps"}],
         "poe_monitor": [{"name": "ether1", "poe-out-power": "6.2"}, {"name": "ether2", "poe-out-power": "4.1"}],
         "stp_bridges": [{"name": "bridge", "root-bridge": "true"}], "bridges": [{"name": "bridge", "priority": "0x7000"}],
         "resource": {"cpu-load": "41"}},
    AP: {"interfaces": [_if("ether1", "CC:00:00:00:00:01")]},
    LOST: {"interfaces": [_if("ether1", "DD:00:00:00:00:01")]},
}


def test_tree_ports_wan_stp():
    r = infra.site_infra(DEVS, INVS)
    n = {x["id"]: x for x in r["nodes"]}
    assert r["nodes"][0]["id"] == GW and n[GW]["parent"] is None and n[GW]["cpu"] == 19 and n[GW]["mem"] == 67
    assert (n[SW]["parent"], n[SW]["parent_port"], n[SW]["uplink_port"]) == (GW, "ether3", "ether24")
    assert (n[AP]["parent"], n[AP]["parent_port"], n[AP]["level"]) == (SW, "ether1", 2)
    assert n[AP]["uplink_port"] is None  # offline AP nemá inventuru sousedů
    assert n[LOST]["parent"] is None  # nepropojené zařízení = další kořen
    assert n[SW]["stp_root"] and n[SW]["stp_priority"] == 0x7000 and n[SW]["poe_w"] == 10.3
    assert [p["cls"] for p in n[SW]["ports"]] == ["fe", "gbe", "down"]  # ether1, ether24, sfp
    wans = [(w["interface"], w["active"], w["address"]) for w in r["wans"] if w["device_id"] == GW]
    assert wans == [("ether1", True, "81.2.3.4"), ("ether2", False, "192.168.8.100")]  # vpn tabulka ignorována
    assert r["externals"] == [{"parent": GW, "port": "ether1", "label": "isp-modem", "platform": None, "address": None}]
    assert any("ap-hala: offline" in w["text"] for w in r["warnings"])


def test_empty():
    assert infra.site_infra([], {}) == {"nodes": [], "wans": [], "externals": [], "warnings": []}


def _sw(mac):
    return {"interfaces": [_if("ether1", mac)], "bridges": [{"name": "bridge"}]}


def test_unmanaged_switch_hub_upstream_and_lonely_stp_roots():
    """Situace z labu: 4 MikroTiky za nespravovaným switchem, MNDP je ukazuje všechny na uplinku,
    mgmt přes DHCP na bridgi (to není WAN) a každý se považuje za STP root."""
    ids = ["ax", "cm", "sw", "ac"]
    macs = {"ax": "10:00:00:00:00:01", "cm": "10:00:00:00:00:02", "sw": "10:00:00:00:00:03", "ac": "10:00:00:00:00:04"}
    devs = [{"id": i, "identity": i.upper(), "role": "switch", "state": "adopted"} for i in ids]
    invs = {}
    for i in ids:
        inv = _sw(macs[i])
        inv["neighbors"] = [{"interface": "bridge/ether1", "mac-address": macs[o], "identity": o.upper(),
                             "discovered-by": "mndp"} for o in ids if o != i]
        inv["neighbors"].append({"interface": "bridge/ether1", "mac-address": "AA:AA:AA:AA:AA:AA", "identity": "router-zakaznika"})
        inv["dhcp_client"] = [{"interface": "bridge", "address": "192.168.77.8/24", "gateway": "192.168.77.1", "status": "bound"}]
        inv["routes_default"] = [{"dst-address": "0.0.0.0/0", "gateway": "192.168.77.1", "immediate-gw": "192.168.77.1%bridge",
                                  "distance": "1", "active": "true"}]
        inv["stp_bridges"] = [{"name": "bridge", "root-bridge": "true" if i != "ac" else "false",
                               "root-bridge-id": f"0x8000.{macs[i] if i != 'ac' else macs['ax']}", "root-port": "ether1"}]
        inv["bridges"] = [{"name": "bridge", "protocol-mode": "rstp"}]
        invs[i] = inv
    r = infra.site_infra(devs, invs)
    n = {x["id"]: x for x in r["nodes"]}
    hub = r["nodes"][0]
    assert hub["kind"] == "hub" and hub["parent"] is None and sorted(hub["members"]) == sorted(ids)
    assert all(n[i]["parent"] == hub["id"] and n[i]["uplink_port"] == "ether1" for i in ids)
    # DHCP na bridgi není WAN; brána mimo správu nad hubem, router zákazníka jen jednou pod hubem
    assert r["wans"] == [{"interface": None, "port": None, "gateway": "192.168.77.1", "address": None, "distance": None,
                          "active": True, "dhcp": None, "comment": None, "device_id": hub["id"], "upstream": True}]
    assert [e["label"] for e in r["externals"]] == ["router-zakaznika"] and r["externals"][0]["parent"] == hub["id"]
    # STP: korunka jen tomu, koho vidí i ostatní (AX má za root i AC); CM a SW jen sami pro sebe
    assert [i for i in ids if n[i]["stp_root"]] == ["ax"]
    assert any("jen sám pro sebe" in w["text"] for w in r["warnings"])
    assert any("nespravovaný switch" in w["text"] for w in r["warnings"])


def test_chain_with_mndp_flooding_is_direct():
    """GW → SW1 → SW2 v řadě: MNDP projde bridgem, GW vidí na ether3 obě, ale přímý spoj je jen GW–SW1."""
    devs = [{"id": "gw", "identity": "GW", "role": "gateway", "state": "adopted"},
            {"id": "s1", "identity": "S1", "role": "switch", "state": "adopted"},
            {"id": "s2", "identity": "S2", "role": "switch", "state": "adopted"}]
    m = {"gw": "20:00:00:00:00:01", "s1": "20:00:00:00:00:02", "s2": "20:00:00:00:00:03"}
    nb = lambda iface, o: {"interface": iface, "mac-address": m[o], "identity": o.upper()}  # noqa: E731
    invs = {"gw": {"interfaces": [_if("ether3", m["gw"])], "neighbors": [nb("ether3", "s1"), nb("ether3", "s2")]},
            "s1": {"interfaces": [_if("ether24", m["s1"])], "neighbors": [nb("ether24", "gw"), nb("ether1", "s2")]},
            "s2": {"interfaces": [_if("ether1", m["s2"])], "neighbors": [nb("ether1", "gw"), nb("ether1", "s1")]}}
    n = {x["id"]: x for x in infra.site_infra(devs, invs)["nodes"]}
    assert (n["s1"]["parent"], n["s1"]["parent_port"], n["s1"]["uplink_port"]) == ("gw", "ether3", "ether24")
    assert (n["s2"]["parent"], n["s2"]["parent_port"], n["s2"]["uplink_port"]) == ("s1", "ether1", "ether1")
    assert not any(x.get("kind") == "hub" for x in n.values())
