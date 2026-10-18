"""Panel portů: připojená zařízení s ikonou, skóre anomálie, jména VLAN."""

from mtcontroller import portview


def test_device_kind():
    assert portview.device_kind("RBcAPGi-5acD2nD", mikrotik=True) == "ap"
    assert portview.device_kind("cAP ax", mikrotik=True) == "ap"
    assert portview.device_kind("CRS326-24G-2S+", mikrotik=True) == "switch"
    assert portview.device_kind("RB5009UPr+S+", mikrotik=True) == "router"
    assert portview.device_kind("Kamera-vchod") == "camera"
    assert portview.device_kind("DiskStation") == "nas"
    assert portview.device_kind("HP-MFP-M428") == "printer"
    assert portview.device_kind("idrac-SRV01") == "server"
    assert portview.device_kind("SIP-T46U-yealink") == "phone"
    assert portview.device_kind("NB-novak") == "pc"
    assert portview.device_kind(None, "xyz") == "unknown"


INV = {
    "interfaces": [{"name": f"ether{i}", "type": "ether", "comment": "Kamery" if i == 3 else None} for i in range(1, 8)],
    "neighbors": [
        {"interface": "bridge/ether1", "identity": "GW", "board": "RB5009UPr+S+", "platform": "MikroTik",
         "mac-address": "AA:00:00:00:00:01", "address": "10.0.0.1"},
        {"interface": "ether2,bridge", "identity": "SW-sklad", "board": "CRS328-24P-4S+", "platform": "MikroTik"},
        {"interface": "ether6", "identity": "usw-flex", "platform": "", "board": "", "system-caps": "bridge"},
    ],
    "vlans": [{"name": "vlan10", "vlan-id": "10"}, {"name": "vlan20-guest", "vlan-id": "20"},
              {"name": "x", "vlan-id": "30", "disabled": "true"}],
    "addresses": [{"address": "192.168.10.1/24", "interface": "vlan10"}],
}


def test_connected_neighbors_first_then_clients():
    clients = [
        {"mac": "B0:00:00:00:00:01", "ip": "192.168.10.50", "hostname": "ipc-vchod", "port": "ether3"},
        {"mac": "B0:00:00:00:00:02", "ip": "192.168.10.51", "hostname": None, "port": "ether2"},  # za switchem
        {"mac": "B0:00:00:00:00:03", "ip": "192.168.10.60", "hostname": "nb-novak", "port": "ether4"},
        {"mac": "B0:00:00:00:00:04", "ip": "192.168.10.61", "hostname": None, "port": "ether4"},
        *({"mac": f"C0:00:00:00:00:0{k}", "ip": None, "hostname": None, "port": "ether5"} for k in range(5)),
        {"mac": "B0:00:00:00:00:09", "ip": None, "hostname": None, "port": "wifi1"},
    ]
    c = portview.connected(INV, clients, {"GW": "gateway"})
    assert c["ether1"]["kind"] == "router" and c["ether1"]["managed"] and c["ether1"]["source"] == "neighbor"
    assert c["ether2"]["kind"] == "switch" and c["ether2"]["name"] == "SW-sklad"
    assert c["ether3"] == {"name": "ipc-vchod", "kind": "camera", "mac": "B0:00:00:00:00:01", "ip": "192.168.10.50",
                           "model": None, "count": 1, "managed": False, "source": "client"}
    assert c["ether4"]["name"] == "nb-novak +1" and c["ether4"]["kind"] == "pc"
    assert c["ether5"]["kind"] == "switch" and c["ether5"]["count"] == 5
    assert c["ether6"]["kind"] == "switch"
    assert "wifi1" not in c and "ether7" not in c


def test_score():
    assert portview.score(0, 0, 0, False)["score"] == 0
    r = portview.score(4, 1, 0, False)
    assert r["score"] == 45 and r["reasons"] == ["3× výpadek linky"]
    assert portview.score(0, 0, 999, False)["score"] == 30
    assert portview.score(20, 0, 10 ** 6, True)["score"] == 100
    assert portview.score(0, 0, 0, False, fe_on_gbe=True)["reasons"] == ["jen 100 Mb/s"]


class FakeTx:
    def __init__(self, events, samples):
        self.events, self.samples = events, samples

    def fetchall(self, sql, params=None):
        return self.events if "port_event" in sql else self.samples


def test_anomalies():
    inv = {"ethernet_monitor": [
        {"name": "ether1", "status": "link-ok", "rate": "1Gbps", "full-duplex": "true"},
        {"name": "ether2", "status": "link-ok", "rate": "100Mbps", "full-duplex": "false"},
        {"name": "ether3", "status": "no-link"},
    ]}
    tx = FakeTx(
        [{"port": "ether1", "field": "link", "new": "down"}, {"port": "ether1", "field": "link", "new": "up"},
         {"port": "ether4", "field": "flaps", "new": "3"}, {"port": "bridge", "field": "link", "new": "down"}],
        [{"name": "ether1", "e": 0}, {"name": "ether5", "e": 120}, {"name": "vlan10", "e": 5}])
    a = portview.anomalies(tx, "d", inv)
    assert a["ether1"]["score"] == 15
    assert a["ether2"]["score"] == 40 and "half-duplex" in a["ether2"]["reasons"]
    assert a["ether4"]["score"] == 45
    assert a["ether5"]["reasons"] == ["120 chyb"]
    assert set(a) == {"ether1", "ether2", "ether4", "ether5"}


def test_vlan_names():
    v = portview.vlan_names(INV)
    assert v == {"10": {"name": "vlan10", "subnet": "192.168.10.0/24"}, "20": {"name": "vlan20-guest", "subnet": None}}


def test_device_detail_api():
    """Detail zařízení přes API (i skutečný psycopg): připojení, anomálie, profil, VLAN."""
    import copy

    from helpers import drop_db
    from test_flow import _login
    from test_portcfg import INV as PINV, _setup

    from mtcontroller import inventory
    from mtcontroller.db import tenant_scope

    db, name, settings, box, app, tid, sw = _setup()
    try:
        inv = copy.deepcopy(PINV)
        for i in inv["interfaces"]:
            i.update({"running": "true", "link-downs": "0"})
        inv["neighbors"] = [{"interface": "ether1,bridge", "identity": "core-gw", "board": "RB5009UPr+S+", "platform": "MikroTik"}]
        inv["ethernet_monitor"] = [{"name": "ether2", "status": "link-ok", "rate": "100Mbps", "full-duplex": "false"}]
        inv["vlans"] = [{"name": "vlan10", "vlan-id": "10"}]
        inv["addresses"] = [{"address": "192.168.10.1/24", "interface": "vlan10"}]
        inventory.store(db, tid, sw, inv, [])
        with tenant_scope(db, tid) as tx:
            sid = tx.fetchone("SELECT site_id FROM device WHERE id = %(d)s", {"d": sw})["site_id"]
            tx.execute("""INSERT INTO client (tenant_id, site_id, mac, ip, hostname, device_id, device, port)
                          VALUES (%(t)s, %(s)s, 'B0:00:00:00:00:01', '192.168.10.50', 'ipc-vjezd', %(d)s, 'SW1', 'ether3')""",
                       {"t": tid, "s": str(sid), "d": sw})
            tx.execute("""INSERT INTO port_event (tenant_id, device_id, port, field, old, new)
                          VALUES (%(t)s, %(d)s, 'ether4', 'flaps', NULL, '2')""", {"t": tid, "d": sw})
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        assert c.post(base + "/port-profiles", json_body={"name": "Kamera", "pvid": 1, "poe": "auto-on"}).status_code == 201
        d = c.get(base + f"/devices/{sw}").json()
        ifs = {i["name"]: i for i in d["interfaces"]}
        assert ifs["ether1"]["connected"]["name"] == "core-gw" and ifs["ether1"]["connected"]["kind"] == "router"
        assert ifs["ether3"]["connected"]["kind"] == "camera" and ifs["ether3"]["connected"]["ip"] == "192.168.10.50"
        assert ifs["ether3"]["profile"] == "Kamera"
        assert ifs["ether2"]["anomaly"]["score"] == 40          # half-duplex + 100 Mb/s
        assert ifs["ether4"]["anomaly"]["reasons"] == ["2× výpadek linky"]
        assert ifs["ether5"]["anomaly"] is None and ifs["ether5"].get("profile") is None
        assert d["vlan_names"] == {"10": {"name": "vlan10", "subnet": "192.168.10.0/24"}}
    finally:
        drop_db(name)
