"""Syntetické inventury pro testy analýzy (formát jako REST RouterOS: hodnoty jsou řetězce)."""


def iface(name, typ="ether", mac=None, **kw):
    d = {"name": name, "type": typ, "running": "true", "disabled": "false", "dynamic": "false",
         "rx-byte": "1000", "tx-byte": "2000", "rx-error": "0", "tx-error": "0",
         "rx-drop": "0", "tx-drop": "0", "link-downs": "0"}
    if mac:
        d["mac-address"] = mac
    d.update(kw)
    return d


def legacy_per_vlan_bridges():
    """Typický starý způsob: bridge pro LAN + bridge pro každou VLAN,
    VLAN rozhraní na trunku ether1 zapojená do bridgů."""
    return {
        "interfaces": [iface("ether1", mac="AA:00:00:00:00:01"), iface("ether2"), iface("ether3"),
                       iface("ether4"), iface("bridge", "bridge"), iface("br-vlan10", "bridge"),
                       iface("br-vlan20", "bridge"), iface("vlan10-e1", "vlan"), iface("vlan20-e1", "vlan")],
        "bridges": [{"name": "bridge", "vlan-filtering": "false", "disabled": "false"},
                    {"name": "br-vlan10", "vlan-filtering": "false", "disabled": "false"},
                    {"name": "br-vlan20", "vlan-filtering": "false", "disabled": "false"}],
        "bridge_ports": [
            {"bridge": "bridge", "interface": "ether1", "pvid": "1", "disabled": "false", "dynamic": "false"},
            {"bridge": "bridge", "interface": "ether2", "pvid": "1", "disabled": "false", "dynamic": "false"},
            {"bridge": "br-vlan10", "interface": "ether3", "pvid": "1", "disabled": "false", "dynamic": "false"},
            {"bridge": "br-vlan10", "interface": "vlan10-e1", "pvid": "1", "disabled": "false", "dynamic": "false"},
            {"bridge": "br-vlan20", "interface": "ether4", "pvid": "1", "disabled": "false", "dynamic": "false"},
            {"bridge": "br-vlan20", "interface": "vlan20-e1", "pvid": "1", "disabled": "false", "dynamic": "false"},
        ],
        "bridge_vlans": [],
        "vlans": [{"name": "vlan10-e1", "vlan-id": "10", "interface": "ether1", "disabled": "false"},
                  {"name": "vlan20-e1", "vlan-id": "20", "interface": "ether1", "disabled": "false"}],
        "addresses": [{"address": "192.168.88.1/24", "interface": "bridge", "disabled": "false", "dynamic": "false"},
                      {"address": "10.0.10.1/24", "interface": "br-vlan10", "disabled": "false", "dynamic": "false"}],
        "dhcp_servers": [{"name": "dhcp-v10", "interface": "br-vlan10", "disabled": "false"}],
    }


def modern():
    """Cílový model: jeden bridge, vlan-filtering, bridge tagovaný, VLAN rozhraní na bridge."""
    return {
        "interfaces": [iface("ether1", mac="BB:00:00:00:00:01"), iface("ether2"), iface("bridge", "bridge"),
                       iface("vlan10", "vlan")],
        "bridges": [{"name": "bridge", "vlan-filtering": "true", "disabled": "false"}],
        "bridge_ports": [
            {"bridge": "bridge", "interface": "ether1", "pvid": "1", "ingress-filtering": "true",
             "disabled": "false", "dynamic": "false"},
            {"bridge": "bridge", "interface": "ether2", "pvid": "10", "ingress-filtering": "true",
             "disabled": "false", "dynamic": "false"},
        ],
        "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "10", "tagged": "bridge,ether1", "untagged": "ether2",
                          "disabled": "false", "dynamic": "false"}],
        "vlans": [{"name": "vlan10", "vlan-id": "10", "interface": "bridge", "disabled": "false"}],
        "addresses": [{"address": "10.0.10.2/24", "interface": "vlan10", "disabled": "false", "dynamic": "false"}],
        "ntp_client": {"enabled": "false", "servers": ""},
        "ntp_server": {"enabled": "false"},
        "clock": {"time-zone-name": "manual"},
    }
