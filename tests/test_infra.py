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
