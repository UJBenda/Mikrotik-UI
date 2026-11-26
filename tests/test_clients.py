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


def test_ap_behind_unmanaged_switch_does_not_attract_wired_clients():
    """gw ── sw (ether7) ── [hloupý switch + cizí bezdrátový spoj] ── ap (ether1). Počítače visí na hloupém switchi.
    AP přes ether1 vidí celou síť; sousedy (MNDP) přes cizí spoj neslyší – přesto jeho port nahoru není port klienta."""
    pcs = ["50:00:00:00:00:01", "50:00:00:00:00:02", "50:00:00:00:00:03"]
    host = lambda mac, port: {"mac-address": mac, "on-interface": port, "local": "false"}  # noqa: E731
    gw = {"interfaces": [_iface("ether1", "AA:00:00:00:00:01")],
          "bridge_hosts": [host("BB:00:00:00:00:01", "ether1"), host("CC:00:00:00:00:01", "ether1")] + [host(m, "ether1") for m in pcs]}
    sw = {"interfaces": [_iface("ether1", "BB:00:00:00:00:01")],
          "bridge_hosts": [host("AA:00:00:00:00:01", "ether1"), host("CC:00:00:00:00:01", "ether7"),
                           host("60:00:00:00:00:09", "ether3")] + [host(m, "ether7") for m in pcs]}
    ap = {"interfaces": [_iface("ether1", "CC:00:00:00:00:01")],
          "wifi_clients": [{"interface": "wifi1", "mac-address": "70:00:00:00:00:07", "ssid": "Firma", "signal": "-60"}],
          # AP vidí bránu, switch i všechny počítače na svém jediném ethernetu; žádní sousedé
          "bridge_hosts": [host("AA:00:00:00:00:01", "ether1"), host("BB:00:00:00:00:01", "ether1"), host("60:00:00:00:00:09", "ether1"),
                           host("70:00:00:00:00:07", "wifi1")] + [host(m, "ether1") for m in pcs]}
    devs = [{"id": "ap", "identity": "ap"}, {"id": "gw", "identity": "gw", "role": "gateway"}, {"id": "sw", "identity": "sw"}]
    cl = {c["mac"]: c for c in correlate(devs, {"gw": gw, "sw": sw, "ap": ap})}
    for m in pcs:
        assert (cl[m]["device"], cl[m]["port"]) == ("sw", "ether7"), cl[m]      # ne pod AP
        assert cl[m]["shared"] and cl[m]["unmanaged"] and cl[m]["port_clients"] == 3 and not cl[m].get("uncertain")
    assert (cl["60:00:00:00:00:09"]["device"], cl["60:00:00:00:00:09"]["port"]) == ("sw", "ether3")
    assert not cl["60:00:00:00:00:09"]["unmanaged"]
    assert cl["70:00:00:00:00:07"]["device"] == "ap" and cl["70:00:00:00:00:07"]["wifi"]["ssid"] == "Firma"
    assert [c["mac"] for c in cl.values() if c["device"] == "ap"] == ["70:00:00:00:00:07"]   # pod AP jen jeho Wi-Fi klient
    # i bez označené brány: nahoru vede port, za kterým je vidět nejvíc spravovaných zařízení
    for d in devs:
        d.pop("role", None)
    cl = {c["mac"]: c for c in correlate(devs, {"gw": gw, "sw": sw, "ap": ap})}
    assert all(cl[m]["device"] != "ap" for m in pcs)
    # stejná MAC na koncovém portu dvou zařízení = nejistá poloha
    sw2 = {"interfaces": [_iface("ether1", "DD:00:00:00:00:01")], "bridge_hosts": [host("60:00:00:00:00:09", "ether4")]}
    cl = {c["mac"]: c for c in correlate(devs + [{"id": "sw2", "identity": "sw2"}], {"gw": gw, "sw": sw, "ap": ap, "sw2": sw2})}
    assert cl["60:00:00:00:00:09"]["uncertain"] and cl["60:00:00:00:00:09"]["candidates"] == 2


def test_map_inserts_unmanaged_switch_node():
    from mtcontroller import infra

    pc = lambda mac: {"mac": mac, "port": "ether7", "ssid": None}  # noqa: E731
    out = {"nodes": [
        {"id": "gw", "parent": None, "level": 0, "parent_port": None, "clients": []},
        {"id": "sw", "parent": "gw", "level": 1, "parent_port": "ether1",
         "clients": [pc("50:00:00:00:00:01"), pc("50:00:00:00:00:02"), {"mac": "60:00:00:00:00:09", "port": "ether3", "ssid": None}]},
        {"id": "ap", "parent": "sw", "level": 2, "parent_port": "ether7", "uplink_port": "ether1",
         "clients": [{"mac": "70:00:00:00:00:07", "port": "wifi1", "ssid": "Firma"}]},
        {"id": "ap2", "parent": "ap", "level": 3, "parent_port": "wifi2", "clients": []}]}
    assert infra.insert_unmanaged(out) == 1
    n = {x["id"]: x for x in out["nodes"]}
    hub = n["usw-sw-ether7"]
    assert hub["kind"] == "hub" and hub["guess"] and hub["parent"] == "sw" and hub["parent_port"] == "ether7" and hub["level"] == 2
    assert [c["mac"] for c in hub["clients"]] == ["50:00:00:00:00:01", "50:00:00:00:00:02"] and hub["members"] == ["sw", "ap"]
    assert n["ap"]["parent"] == "usw-sw-ether7" and n["ap"]["parent_port"] is None and n["ap"]["level"] == 3 and n["ap2"]["level"] == 4
    assert [c["port"] for c in n["sw"]["clients"]] == ["ether3"] and len(n["ap"]["clients"]) == 1
    assert [x["id"] for x in out["nodes"]][:3] == ["gw", "sw", "usw-sw-ether7"]
    assert infra.insert_unmanaged(out) == 0                                    # podruhé už není co vkládat


def test_wireless_link_is_not_an_unmanaged_switch():
    """ap (wlan1) ≋ sta: MAC viděná jen na rádiu AP patří někam za stanici – ve vzduchu žádný switch není."""
    from mtcontroller import infra, unmanaged

    host = lambda mac, port: {"mac-address": mac, "on-interface": port, "local": "false"}  # noqa: E731
    ap = {"interfaces": [_iface("ether1", "AA:00:00:00:00:01"), {"name": "wlan1", "type": "wlan", "mac-address": "AA:00:00:00:00:02"}],
          "wireless_clients": [{"interface": "wlan1", "mac-address": "BB:00:00:00:00:02"}],
          "bridge_hosts": [host("BB:00:00:00:00:02", "wlan1"), host("50:00:00:00:00:01", "wlan1")]}
    sta = {"interfaces": [_iface("ether1", "BB:00:00:00:00:01"), {"name": "wlan1", "type": "wlan", "mac-address": "BB:00:00:00:00:02"}],
           "wireless_clients": [{"interface": "wlan1", "mac-address": "AA:00:00:00:00:02"}], "bridge_hosts": []}
    devs = [{"id": "ap", "identity": "ap", "role": "gateway"}, {"id": "sta", "identity": "sta"}]
    found = correlate(devs, {"ap": ap, "sta": sta})
    cl = {c["mac"]: c for c in found}
    pc = cl["50:00:00:00:00:01"]
    assert (pc["device"], pc["port"]) == ("sta", "?") and pc["uncertain"] and pc["via_link"] and not pc.get("shared"), pc
    assert not pc["wifi"] and not pc["unmanaged"]
    assert unmanaged.detect(found) == {}
    # stejná MAC na koncovém portu stanice má přednost před rádiem
    sta["bridge_hosts"] = [host("50:00:00:00:00:01", "ether1")]
    pc = {c["mac"]: c for c in correlate(devs, {"ap": ap, "sta": sta})}["50:00:00:00:00:01"]
    assert (pc["device"], pc["port"]) == ("sta", "ether1") and not pc.get("via_link")
    # mapa: na rádiu AP se uzel nespravovaného switche nevkládá ani se dvěma stanicemi
    out = {"nodes": [{"id": "ap", "parent": None, "level": 0, "parent_port": None, "clients": []},
                     {"id": "s1", "parent": "ap", "level": 1, "parent_port": "wlan1", "clients": []},
                     {"id": "s2", "parent": "ap", "level": 1, "parent_port": "wlan1", "clients": []}]}
    assert infra.insert_unmanaged(out, {("ap", "wlan1")}) == 0 and len(out["nodes"]) == 3
    assert infra.insert_unmanaged(out) == 1


def test_unmanaged_detect():
    from mtcontroller import unmanaged

    c = lambda port, **kw: {"device_id": "sw", "port": port, "wifi": None, **kw}  # noqa: E731
    found = unmanaged.detect([c("ether1"), c("ether1"),                               # telefon + počítač = nic
                              c("ether2"), c("ether2"), c("ether2"),                  # tři klienti = switch
                              c("ether3", shared=True),                               # klient + spravované zařízení
                              c("?", via_link=True, uncertain=True), {"device_id": "ap", "port": "wifi1", "wifi": {"ssid": "x"}}])
    assert found == {("sw", "ether2"): {"clients": 3, "shared": False}, ("sw", "ether3"): {"clients": 1, "shared": True}}
    assert unmanaged.what({"clients": 3, "shared": False}) == "3 klienti"
    assert unmanaged.what({"clients": 1, "shared": True}) == "1 klient a spravované zařízení"


def test_capsman_clients_belong_to_their_ap():
    """S CAPsMANem je tabulka klientů na správci; klient se má přiřadit AP, jehož rádio ho drží."""
    devs = [{"id": "gw", "serial": "GW", "identity": "GW"}, {"id": "ap1", "serial": "A1", "identity": "AP-Velin"},
            {"id": "ap2", "serial": "A2", "identity": "AP-Sklad"}]
    invs = {
        "gw": {"interfaces": [{"name": "ether1", "mac-address": "AA:00:00:00:00:01"}],
               # cap-wifi1: MAC sedí s rádiem AP-Velin; cap-wifi2: MAC nesedí, pozná se podle jména CAP v rádiích
               "wifi": [{"name": "cap-wifi1", "mac-address": "BB:00:00:00:00:01"},
                        {"name": "cap-wifi1-host", "mac-address": "BE:00:00:00:00:99", "master-interface": "cap-wifi1"},
                        {"name": "cap-wifi2", "mac-address": "02:00:00:00:00:77"},
                        {"name": "wifi1", "mac-address": "AA:00:00:00:00:02"}],
               "wifi_radios": [{"interface": "cap-wifi2", "radio-mac": "02:00:00:00:00:78", "bands": "5ghz-ax", "cap": "AP-Sklad@10.0.0.7"},
                               {"interface": "wifi1", "radio-mac": "AA:00:00:00:00:02", "bands": "2ghz-ax"}],
               "wifi_clients": [{"interface": "cap-wifi1", "mac-address": "50:00:00:00:00:01", "ssid": "Firma", "signal": "-55"},
                                {"interface": "cap-wifi1-host", "mac-address": "50:00:00:00:00:02", "ssid": "Hoste", "signal": "-70"},
                                {"interface": "cap-wifi2", "mac-address": "50:00:00:00:00:03", "ssid": "Firma", "signal": "-61"},
                                {"interface": "wifi1", "mac-address": "50:00:00:00:00:04", "ssid": "Firma", "signal": "-40"}]},
        "ap1": {"interfaces": [{"name": "ether1", "mac-address": "BB:00:00:00:00:00"}],
                "wifi": [{"name": "wifi1", "mac-address": "BB:00:00:00:00:01"}],
                "wifi_radios": [{"interface": "wifi1", "radio-mac": "BB:00:00:00:00:01", "bands": "5ghz-ax"}],
                "bridge_hosts": [{"mac-address": "50:00:00:00:00:01", "on-interface": "wifi1", "vid": "20"}]},
        "ap2": {"interfaces": [{"name": "ether1", "mac-address": "CC:00:00:00:00:00"}],
                "wifi": [{"name": "wifi1", "mac-address": "CC:00:00:00:00:01"}]},
    }
    got = {c["mac"]: c for c in correlate(devs, invs)}
    assert (got["50:00:00:00:00:01"]["device"], got["50:00:00:00:00:01"]["port"]) == ("AP-Velin", "wifi1")
    assert got["50:00:00:00:00:01"]["vlan"] == "20" and got["50:00:00:00:00:01"]["wifi"]["signal"] == "-55"
    assert got["50:00:00:00:00:02"]["device"] == "AP-Velin"                 # další SSID na stejném rádiu
    assert got["50:00:00:00:00:03"]["device"] == "AP-Sklad"                 # podle jména CAP
    assert (got["50:00:00:00:00:04"]["device"], got["50:00:00:00:00:04"]["port"]) == ("GW", "wifi1")   # vlastní rádio správce
    # dvě zařízení se stejnou identitou: podle jména se nehádá
    devs2 = devs + [{"id": "ap3", "serial": "A3", "identity": "AP-Sklad"}]
    got2 = {c["mac"]: c for c in correlate(devs2, dict(invs, ap3={}))}
    assert got2["50:00:00:00:00:03"]["device"] == "GW"
