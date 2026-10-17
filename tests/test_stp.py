"""STP lokality: root bridge, blokované porty, STP vypnuté na bridgi s více linkami."""

from mtcontroller import inventory, ports, stp
from mtcontroller.rosapi import RosApiError

CORE, ACC, LEG = "c1", "a1", "l1"
DEVS = [{"id": CORE, "serial": "S1", "identity": "core"}, {"id": ACC, "serial": "S2", "identity": "access 1"},
        {"id": LEG, "serial": "S3", "identity": "legacy"}]


def _invs(core_root_mac="48:A9:8A:00:00:01"):
    return {
        CORE: {
            "interfaces": [{"name": "bridge", "type": "bridge", "mac-address": "48:A9:8A:00:00:01"},
                           {"name": "ether1", "mac-address": "48:A9:8A:00:00:02"}],
            "bridges": [{"name": "bridge", "protocol-mode": "rstp"}],
            "bridge_ports": [{"bridge": "bridge", "interface": "ether2"}, {"bridge": "bridge", "interface": "ether3"}],
            "stp_bridges": [{"name": "bridge", "root-bridge": "true", "root-bridge-id": f"0x8000.{core_root_mac}",
                             "current-mac-address": "48:A9:8A:00:00:01"}],
            "stp_ports": [{"interface": "ether2", "bridge": "bridge", "role": "designated-port", "forwarding": "true"},
                          {"interface": "ether3", "bridge": "bridge", "role": "designated-port", "forwarding": "true"}],
            "neighbors": [{"interface": "ether2", "mac-address": "48:A9:8A:00:00:10"},
                          {"interface": "ether3", "mac-address": "48:A9:8A:00:00:20"}],
        },
        ACC: {
            "interfaces": [{"name": "bridge", "type": "bridge", "mac-address": "48:A9:8A:00:00:10"}],
            "bridges": [{"name": "bridge", "protocol-mode": "rstp"}],
            "bridge_ports": [{"bridge": "bridge", "interface": "ether1"}, {"bridge": "bridge", "interface": "ether2"}],
            "stp_bridges": [{"name": "bridge", "root-bridge": "false", "root-bridge-id": f"0x8000.{core_root_mac}",
                             "root-port": "ether1", "root-path-cost": "10"}],
            "stp_ports": [{"interface": "ether1", "bridge": "bridge", "role": "root-port", "forwarding": "true"},
                          {"interface": "ether2", "bridge": "bridge", "role": "alternate-port", "forwarding": "false",
                           "edge-port": "false"}],
            "neighbors": [{"interface": "ether1", "mac-address": "48:A9:8A:00:00:01"},
                          {"interface": "ether2", "mac-address": "48:A9:8A:00:00:20"}],
        },
        LEG: {
            "interfaces": [{"name": "br-lan", "type": "bridge", "mac-address": "48:A9:8A:00:00:20"}],
            "bridges": [{"name": "br-lan", "protocol-mode": "none"}],
            "bridge_ports": [{"bridge": "br-lan", "interface": "ether1"}, {"bridge": "br-lan", "interface": "ether2"},
                             {"bridge": "br-lan", "interface": "ether5"}],
            "neighbors": [{"interface": "ether1", "mac-address": "48:A9:8A:00:00:02"},
                          {"interface": "ether2", "mac-address": "48:A9:8A:00:00:10"},
                          {"interface": "ether5", "mac-address": "AA:BB:CC:00:00:01"}],
        },
    }


def test_root_blocked_and_loop_risk():
    s = stp.site_stp(DEVS, _invs())
    assert s["roots"] == [{"root_id": "0x8000.48:A9:8A:00:00:01", "device": "core"}]
    acc = next(b for b in s["bridges"] if b["device"] == "access 1")
    assert acc["root_device"] == "core" and acc["root_port"] == "ether1"
    assert s["blocked"] == [{"device_id": ACC, "device": "access 1", "port": "ether2", "bridge": "bridge",
                             "role": "alternate-port"}]
    texts = " | ".join(w["text"] for w in s["warnings"])
    assert "legacy br-lan: STP vypnuté, ale 2 linky" in texts and "ether5" not in texts
    assert "není spravované" not in texts


def test_unmanaged_root_and_multiple_roots():
    invs = _invs(core_root_mac="00:0C:42:99:99:99")
    invs[CORE]["stp_bridges"][0]["root-bridge"] = "false"
    invs[ACC]["stp_bridges"][0]["root-bridge-id"] = "0x7000.48:A9:8A:00:00:10"
    s = stp.site_stp(DEVS, invs)
    texts = " | ".join(w["text"] for w in s["warnings"])
    assert "STP root bridge 0x8000.00:0C:42:99:99:99 není spravované" in texts
    assert "2 různých STP root bridge" in texts


def test_port_info_has_stp_role():
    p = ports.port_info(_invs()[ACC])
    assert p["ether2"]["stp_role"] == "alternate-port" and p["ether2"]["stp_forwarding"] is False
    assert p["ether2"]["edge"] is False


def test_collect_bridge_monitor_names_by_order():
    from test_inventory_backup import FakeApi

    responses = {s.path: [] for s in inventory.SECTIONS}
    responses["/interface/bridge"] = [{"name": "bridge", "protocol-mode": "rstp"},
                                      {"name": "br-old", "protocol-mode": "none"},
                                      {"name": "br2", "protocol-mode": "mstp"}]
    responses[("POST", "/interface/bridge/monitor")] = lambda b: [{"root-bridge": "true"}, {"root-bridge": "false"}]
    api = FakeApi(responses)
    data, errors = inventory.collect(api)
    body = next(c[2] for c in api.calls if c[1] == "/interface/bridge/monitor")
    assert body["numbers"] == "bridge,br2" and errors == []
    assert [r["name"] for r in data["stp_bridges"]] == ["bridge", "br2"]
    api = FakeApi(responses, {"/interface/bridge/monitor": RosApiError(400, "no such command")})
    data, errors = inventory.collect(api)
    assert errors == [] and "stp_bridges" not in data
