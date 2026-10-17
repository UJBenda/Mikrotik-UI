from mtcontroller.clients import correlate


def _iface(name, mac):
    return {"name": name, "type": "ether", "mac-address": mac}


def test_client_location_wired_wifi_and_identity():
    # gw (ether1 → sw) ── sw (ether1 uplink, ether5 klient, ether8 nespravovaný switch) ── ap (wifi)
    gw = {"interfaces": [_iface("ether1", "AA:00:00:00:00:01")],
          "neighbors": [{"interface": "ether1", "mac-address": "BB:00:00:00:00:01", "identity": "sw"}],
          "bridge_hosts": [{"mac-address": "11:11:11:11:11:11", "on-interface": "ether1", "vid": "10", "local": "false"},
                           {"mac-address": "22:22:22:22:22:22", "on-interface": "ether1", "vid": "20", "local": "false"},
                           {"mac-address": "BB:00:00:00:00:01", "on-interface": "ether1", "local": "false"}],
          "dhcp_leases": [{"mac-address": "11:11:11:11:11:11", "address": "10.0.10.50", "host-name": "tiskarna",
                           "status": "bound"},
                          {"mac-address": "33:33:33:33:33:33", "active-address": "10.0.20.7", "host-name": "telefon"}],
          "arp": [{"mac-address": "22:22:22:22:22:22", "address": "10.0.20.9", "complete": "true"}]}
    sw = {"interfaces": [_iface("ether1", "BB:00:00:00:00:01")],
          "neighbors": [{"interface": "ether1", "mac-address": "AA:00:00:00:00:01", "identity": "gw"},
                        {"interface": "ether8", "mac-address": "EE:EE:EE:EE:EE:EE", "identity": "levny-switch"}],
          "bridge_hosts": [{"mac-address": "11:11:11:11:11:11", "on-interface": "ether5", "vid": "10", "local": "false"},
                           {"mac-address": "22:22:22:22:22:22", "on-interface": "ether8", "vid": "20", "local": "false"},
                           {"mac-address": "44:44:44:44:44:44", "on-interface": "ether8", "vid": "20", "local": "false"},
                           {"mac-address": "BB:00:00:00:00:01", "on-interface": "bridge", "local": "true"}]}
    ap = {"interfaces": [_iface("ether1", "CC:00:00:00:00:01")],
          "wifi_clients": [{"interface": "wifi1", "mac-address": "33:33:33:33:33:33", "ssid": "Firma", "signal": "-58"}],
          "bridge_hosts": [{"mac-address": "33:33:33:33:33:33", "on-interface": "wifi1", "vid": "20", "local": "false"}]}
    devs = [{"id": "gw", "identity": "gw"}, {"id": "sw", "identity": "sw"}, {"id": "ap", "identity": "ap"}]
    cl = {c["mac"]: c for c in correlate(devs, {"gw": gw, "sw": sw, "ap": ap})}
    # infrastruktura se mezi klienty neobjeví
    assert "BB:00:00:00:00:01" not in cl
    # drátový klient na koncovém portu switche, ne na uplinku gatewaye
    assert (cl["11:11:11:11:11:11"]["device"], cl["11:11:11:11:11:11"]["port"]) == ("sw", "ether5")
    assert cl["11:11:11:11:11:11"]["hostname"] == "tiskarna" and cl["11:11:11:11:11:11"]["vlan"] == "10"
    # klient za nespravovaným switchem = port k tomuto switchi
    assert cl["22:22:22:22:22:22"]["port"] == "ether8" and cl["22:22:22:22:22:22"]["ip"] == "10.0.20.9"
    # wifi klient: AP, SSID, signál, VLAN z bridge host tabulky AP
    w = cl["33:33:33:33:33:33"]
    assert w["device"] == "ap" and w["wifi"]["ssid"] == "Firma" and w["vlan"] == "20" and w["hostname"] == "telefon"
