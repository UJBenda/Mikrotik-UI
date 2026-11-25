"""Firewall jako zone-based: matice zón z výchozí konfigurace MikroTiku a výjimky."""

from mtcontroller import firewall

DEFCONF = {
    "if_lists": [{"name": "all", "builtin": "true"}, {"name": "none", "builtin": "true"},
                 {"name": "WAN"}, {"name": "LAN"}],
    "if_list_members": [{"list": "LAN", "interface": "bridge"}, {"list": "WAN", "interface": "ether1"}],
    "fw_filter": [
        {"chain": "input", "action": "accept", "connection-state": "established,related,untracked"},
        {"chain": "input", "action": "drop", "connection-state": "invalid"},
        {"chain": "input", "action": "accept", "protocol": "icmp"},
        {"chain": "input", "action": "accept", "dst-address": "127.0.0.1"},
        {"chain": "input", "action": "drop", "in-interface-list": "!LAN", "comment": "defconf: drop all not coming from LAN"},
        {"chain": "forward", "action": "accept", "ipsec-policy": "in,ipsec"},
        {"chain": "forward", "action": "accept", "ipsec-policy": "out,ipsec"},
        {"chain": "forward", "action": "fasttrack-connection", "connection-state": "established,related"},
        {"chain": "forward", "action": "accept", "connection-state": "established,related,untracked"},
        {"chain": "forward", "action": "drop", "connection-state": "invalid"},
        {"chain": "forward", "action": "drop", "connection-state": "new", "connection-nat-state": "!dstnat",
         "in-interface-list": "WAN", "packets": "1234", "bytes": "99000"},
    ],
    "fw_nat": [{"chain": "srcnat", "action": "masquerade", "out-interface-list": "WAN"},
               {"chain": "dstnat", "action": "dst-nat", "in-interface-list": "WAN", "protocol": "tcp",
                "dst-port": "8443", "to-addresses": "192.168.88.10", "to-ports": "443"}],
}


def test_defconf_matrix():
    a = firewall.analyze(DEFCONF)
    assert a["zones"] == ["WAN", "LAN"]
    m = a["matrix"]
    assert m["LAN"]["WAN"]["decision"] == "allow" and m["LAN"]["WAN"]["rule"] is None  # výchozí accept
    assert m["WAN"]["LAN"]["decision"] == "block" and "dstnat" in m["WAN"]["LAN"]["note"]
    # ipsec pravidla nemají určení místa: nejsou výjimkou jedné dvojice zón, platí všude (obecná)
    assert m["WAN"]["LAN"]["exceptions"] == [] and a["general"]["forward"] == [5, 6]
    assert a["router"]["WAN"]["decision"] == "block" and a["router"]["LAN"]["decision"] == "allow"
    assert a["router"]["WAN"]["exceptions"] == [] and a["general"]["input"] == [2, 3]  # icmp, 127.0.0.1
    assert a["return_traffic"] == {"forward": True, "input": True} and a["drop_invalid"]["forward"]
    assert a["notes"] == []
    r = {x["index"]: x for x in a["rules"]}
    assert r[10]["packets"] == 1234 and r[10]["summary"] == "z WAN, stav new, NAT !dstnat"
    assert r[5]["summary"] == "ipsec-policy=in,ipsec"
    assert a["nat"][1]["summary"] == "z WAN, tcp/8443, → 192.168.88.10:443"


def test_custom_zone_and_negation():
    inv = dict(DEFCONF, if_lists=DEFCONF["if_lists"] + [{"name": "GUEST"}],
               if_list_members=DEFCONF["if_list_members"] + [{"list": "GUEST", "interface": "vlan30"}],
               fw_filter=DEFCONF["fw_filter"] + [
                   {"chain": "forward", "action": "drop", "in-interface-list": "GUEST", "out-interface-list": "!WAN"},
                   {"chain": "forward", "action": "accept", "in-interface": "vlan30", "protocol": "udp", "dst-port": "53"},
                   {"chain": "forward", "action": "drop", "disabled": "true", "in-interface-list": "LAN"}])
    a = firewall.analyze(inv)
    m = a["matrix"]
    assert m["GUEST"]["LAN"]["decision"] == "block" and m["GUEST"]["WAN"]["decision"] == "allow"
    assert m["LAN"]["GUEST"]["decision"] == "allow"  # vypnuté pravidlo se nepočítá
    assert m["GUEST"]["WAN"]["exceptions"] == [12] and m["LAN"]["WAN"]["exceptions"] == []  # udp/53 jen z vlan30
    r = {x["index"]: x for x in a["rules"]}
    assert r[12]["src_zones"] == ["GUEST"] and r[12]["dst_zones"] is None and r[5]["src_zones"] is None


def test_missing_return_traffic_note_and_no_data():
    inv = dict(DEFCONF, fw_filter=[{"chain": "forward", "action": "drop", "in-interface-list": "WAN"}])
    a = firewall.analyze(inv)
    assert any("návratového" in n for n in a["notes"]) and any("invalid" in n for n in a["notes"])
    assert firewall.analyze({}) is None


def test_zone_by_address_list_and_interface():
    """Pravidlo se k zóně váže seznamem rozhraní, rozhraním, adresou i address-listem; zóna bez IP jen rozhraním."""
    inv = {
        "if_lists": [{"name": "WAN"}, {"name": "LAN"}, {"name": "MNG"}, {"name": "GREGOR"}],
        "if_list_members": [{"list": "WAN", "interface": "ether1"}, {"list": "LAN", "interface": "vlan90"},
                            {"list": "MNG", "interface": "vlan93"}, {"list": "GREGOR", "interface": "vlan92"}],
        "addresses": [{"address": "192.168.90.1/24", "interface": "vlan90"}, {"address": "192.168.93.1/24", "interface": "vlan93"},
                      {"address": "81.2.3.4/30", "interface": "ether1"}],                    # vlan92 (GREGOR) adresu nemá
        "routes_default": [{"gateway": "81.2.3.5", "immediate-gw": "81.2.3.5%ether1"}],
        "fw_addrlists": [{"list": "servery", "address": "192.168.93.10"}, {"list": "servery", "address": "192.168.93.11"},
                         {"list": "lan-cela", "address": "192.168.90.0/24"}, {"list": "vypnuty", "address": "192.168.90.5", "disabled": "true"}],
        "fw_filter": [
            {"chain": "forward", "action": "accept", "connection-state": "established,related"},
            {"chain": "forward", "action": "accept", "protocol": "icmp"},                                        # 1 obecné
            {"chain": "forward", "action": "accept", "src-address": "192.168.90.0/24", "dst-address": "192.168.93.10",
             "protocol": "tcp", "dst-port": "443"},                                                           # 2 LAN → MNG
            {"chain": "forward", "action": "accept", "src-address-list": "servery", "out-interface-list": "WAN"},  # 3 část MNG → WAN
            {"chain": "forward", "action": "drop", "src-address-list": "lan-cela", "dst-address": "192.168.93.0/24"},  # 4 LAN → MNG vše
            {"chain": "forward", "action": "accept", "in-interface": "vlan92", "dst-address": "8.8.8.8"},        # 5 GREGOR → internet
            {"chain": "forward", "action": "drop", "in-interface": "vlan92"},                                    # 6 GREGOR → vše
            {"chain": "forward", "action": "accept", "src-address-list": "neznamy", "protocol": "udp"},          # 7 neznámý seznam
            {"chain": "forward", "action": "drop", "in-interface-list": "!WAN", "out-interface-list": "!WAN", "disabled": "true"},
        ],
    }
    a = firewall.analyze(inv)
    m = a["matrix"]
    assert a["subnets"]["GREGOR"] == [] and a["subnets"]["LAN"] == ["192.168.90.0/24"]
    assert a["general"]["forward"] == [1]
    # LAN → MNG: výjimka tcp/443 na jeden server, pak zákaz celé dvojice podle adres (pokrývají obě zóny celé)
    assert m["LAN"]["MNG"] == {"decision": "block", "rule": 4, "exceptions": [2], "note": None}
    assert m["LAN"]["WAN"]["exceptions"] == [7] and m["LAN"]["WAN"]["rule"] is None
    # MNG → WAN: seznam „servery“ je jen část zóny → výjimka, ne rozhodnutí
    assert m["MNG"]["WAN"]["exceptions"] == [3, 7] and m["MNG"]["WAN"]["decision"] == "allow"
    assert 3 not in m["LAN"]["WAN"]["exceptions"] and 3 not in m["MNG"]["LAN"]["exceptions"]
    # zóna bez adresy se pozná podle rozhraní; 8.8.8.8 není v žádné místní podsíti → patří k internetu
    assert m["GREGOR"]["WAN"] == {"decision": "block", "rule": 6, "exceptions": [5], "note": None}
    assert m["GREGOR"]["LAN"] == {"decision": "block", "rule": 6, "exceptions": [], "note": None}
    assert m["MNG"]["GREGOR"]["exceptions"] == [7]
    r = {x["index"]: x for x in a["rules"]}
    assert r[2]["src_zones"] == ["LAN"] and r[2]["dst_zones"] == ["MNG"] and r[5]["dst_zones"] == ["WAN"]
    assert r[8]["disabled"] and r[3]["summary"] == "do WAN, zdroj ∈ servery"
    # bez inventury seznamů adres se pravidlo se seznamem bere jako možná výjimka všude (nic se neschová)
    b = firewall.analyze({k: v for k, v in inv.items() if k != "fw_addrlists"})
    assert 3 in b["matrix"]["LAN"]["WAN"]["exceptions"] and b["matrix"]["LAN"]["MNG"]["rule"] is None
