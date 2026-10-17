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
    assert m["WAN"]["LAN"]["exceptions"] == [5, 6]  # ipsec pravidla jsou výjimky, ne „vše povoleno“
    assert a["router"]["WAN"]["decision"] == "block" and a["router"]["LAN"]["decision"] == "allow"
    assert a["router"]["WAN"]["exceptions"] == [2, 3]  # icmp, 127.0.0.1
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
    assert m["GUEST"]["WAN"]["exceptions"] == [5, 6, 12]  # ipsec + udp/53 z vlan30


def test_missing_return_traffic_note_and_no_data():
    inv = dict(DEFCONF, fw_filter=[{"chain": "forward", "action": "drop", "in-interface-list": "WAN"}])
    a = firewall.analyze(inv)
    assert any("návratového" in n for n in a["notes"]) and any("invalid" in n for n in a["notes"])
    assert firewall.analyze({}) is None
