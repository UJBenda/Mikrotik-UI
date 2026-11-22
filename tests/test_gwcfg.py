"""Řízená brána (gateway mode, krok 1): model, čistá konfigurace, sladění, převzetí přes API."""

import copy
import re

from mtcontroller import gwcfg, intent, switchcfg

INTENT_RAW = {"networks": [
    {"name": "Management", "vlan_id": 99, "purpose": "mgmt", "subnet": "10.99.0.0/24", "gateway": "10.99.0.1"},
    {"name": "Kancelář", "vlan_id": 10, "purpose": "lan", "subnet": "10.10.0.0/24"},
    {"name": "Hosté", "vlan_id": 20, "purpose": "hoste", "subnet": "10.20.0.0/24", "dns": ["9.9.9.9"]},
    {"name": "Kamery", "vlan_id": 30, "purpose": "kamery"}]}
INTENT = intent.validate(INTENT_RAW)

# tovární router: ether1 WAN (DHCP klient), ostatní porty v bridgi, defconf firewall
INV = {
    "identity": {"name": "MikroTik"},
    "interfaces": [{"name": n, "type": "ether"} for n in ("ether1", "ether2", "ether3", "ether4", "ether5")]
    + [{"name": "bridge", "type": "bridge"}],
    "ethernet": [{"name": n, "default-name": n, "loop-protect": "default"} for n in
                 ("ether1", "ether2", "ether3", "ether4", "ether5")],
    "bridges": [{"name": "bridge", "vlan-filtering": "false"}],
    "bridge_ports": [{"bridge": "bridge", "interface": n} for n in ("ether2", "ether3", "ether4", "ether5")],
    "addresses": [{"address": "192.168.88.1/24", "interface": "bridge"},
                  {"address": "100.64.3.7/24", "interface": "ether1", "dynamic": "true"}],
    "dhcp_client": [{"interface": "ether1", "status": "bound", "address": "100.64.3.7/24", "gateway": "100.64.3.1"}],
    "routes_default": [{"dst-address": "0.0.0.0/0", "gateway": "100.64.3.1", "immediate-gw": "100.64.3.1%ether1",
                        "active": "true"}],
    "dhcp_servers": [{"name": "defconf", "interface": "bridge"}],
    "neighbors": [{"interface": "ether3,bridge", "identity": "SW-1", "platform": "MikroTik", "board": "CRS326"}],
    "fw_filter": [{"chain": "input", "action": "drop", "comment": "defconf: drop all not coming from LAN"}],
    "fw_nat": [{"chain": "srcnat", "action": "masquerade", "comment": "defconf: masquerade"}],
}


def _build(inv=INV, gw=None, it=INTENT, **kw):
    return switchcfg.build(inv, it, {}, model_name="RB5009UG+S+", identity="GW", role="gateway",
                           gateway=gw if gw is not None else {"device_id": "x", "wan": {"mode": "dhcp", "vlan": None}, "dhcp": {}},
                           **kw)


def test_model():
    m = _build()
    assert m["blockers"] == [], m["blockers"]
    g = m["gw"]
    # WAN = port dnešní výchozí routy, není v bridgi; soused na ether3 je trunk
    assert g["wan"]["port"] == "ether1" and g["wan"]["iface"] == "ether1"
    ports = {p["port"]: p["mode"] for p in m["ports"]}
    assert "ether1" not in ports and ports["ether3"] == "trunk" and ports["ether2"] == "access"
    assert m["mgmt"]["mode"] == "gateway" and m["uplink"] is None and m["isolate"] is None
    nets = {n["vid"]: n for n in g["nets"]}
    assert set(nets) == {99, 10, 20} and any("Kamery" in w for w in m["warnings"])
    assert nets[99]["iface"] == "bridge" and nets[10]["iface"] == "vlan10" and nets[10]["address"] == "10.10.0.1/24"
    assert nets[10]["dhcp"] == {"ranges": "10.10.0.100-10.10.0.254", "lease": "12h", "dns": ["10.10.0.1"]}
    assert nets[20]["dhcp"]["dns"] == ["9.9.9.9"] and nets[20]["dhcp"]["lease"] == "2h"
    assert g["zones"] == {"WAN": ["ether1"], "MGMT": ["bridge"], "LAN": ["vlan10"], "GUEST": ["vlan20"]}
    pol = {(p["src"], p["dst"]) for p in g["policies"]}
    assert ("GUEST", "WAN") in pol and ("LAN", "GUEST") in pol and ("GUEST", "LAN") not in pol and ("MGMT", "LAN") in pol
    assert g["admin"] == ["10.99.0.0/24"] and g["admin_zone"] == "MGMT"
    # bridge je tagovaný člen VLAN s adresou na vlan<id>, mgmt VLAN má netagovaně
    vl = {v["vid"]: v for v in m["vlans"]}
    assert "bridge" in vl[10]["tagged"] and "bridge" in vl[99]["untagged"] and "bridge" not in vl[30]["tagged"]


def test_blockers_and_ranges():
    bad = intent.validate({"networks": [
        {"name": "M", "vlan_id": 99, "purpose": "mgmt", "subnet": "10.99.0.0/24"},
        {"name": "A", "vlan_id": 10, "purpose": "lan", "subnet": "10.99.0.128/25"}]})
    assert any("překrývá" in b for b in _build(it=bad)["blockers"])
    m = _build(gw={"device_id": "x", "wan": {"mode": "dhcp", "port": "ether9", "vlan": None}, "dhcp": {}})
    assert any("ether9" in b for b in m["blockers"])
    # brána uprostřed rozsahu se vynechá; rozsah mimo síť je chyba
    net = __import__("ipaddress").IPv4Network("10.0.0.0/24")
    ip = __import__("ipaddress").IPv4Address
    assert gwcfg._ranges(net, ip("10.0.0.150"), {}) == "10.0.0.100-10.0.0.149,10.0.0.151-10.0.0.254"
    assert gwcfg._ranges(net, ip("10.0.0.1"), {"start": "10.0.0.10", "end": "10.0.0.20"}) == "10.0.0.10-10.0.0.20"
    assert gwcfg._ranges(__import__("ipaddress").IPv4Network("10.0.0.0/30"), ip("10.0.0.1"), {}) is None
    m = _build(gw={"device_id": "x", "wan": {"mode": "dhcp", "vlan": None},
                   "dhcp": {"10": {"enabled": True, "start": "10.77.0.10", "end": "10.77.0.20"}, "20": {"enabled": False}}})
    assert any("neleží" in b for b in m["blockers"])
    assert next(n for n in m["gw"]["nets"] if n["vid"] == 20)["dhcp"] is None


def test_validate():
    v = gwcfg.validate({"wan": {"mode": "pppoe", "port": "ether1", "vlan": "848", "user": "o2", "password": "tajne"},
                        "dhcp": {"10": {"start": "10.10.0.50", "end": "10.10.0.99", "lease": "8h"}}})
    assert v["wan"] == {"port": "ether1", "mode": "pppoe", "vlan": 848, "user": "o2"} and "password" not in str(v)
    assert v["dhcp"]["10"] == {"enabled": True, "start": "10.10.0.50", "end": "10.10.0.99", "lease": "8h"}
    for bad in ({"wan": {"mode": "lte"}}, {"wan": {"mode": "static", "address": "1.2.3.4"}},
                {"wan": {"mode": "static", "address": "1.2.3.4/24", "gateway": "1.2.3.1", "dns": []}},
                {"wan": {"mode": "pppoe", "user": "má mezeru"}}, {"wan": {"mode": "dhcp", "vlan": 5000}},
                {"wan": {"mode": "dhcp"}, "dhcp": {"10": {"start": "10.0.0.9", "end": "10.0.0.2"}}},
                {"wan": {"mode": "dhcp"}, "dhcp": {"10": {"lease": "navzdy"}}}):
        try:
            gwcfg.validate(bad)
        except gwcfg.GatewayError:
            continue
        raise AssertionError(bad)


def _ascii(script):
    bad = [ln for ln in script.splitlines() if not re.fullmatch(r"[\x20-\x7e\t]*", ln)]
    assert not bad, bad[:3]


def test_config_body():
    s = switchcfg.config_body(_build())
    _ascii(s)
    assert "interface=ether1" not in s.split("# --- brana")[0]          # WAN není port bridge
    assert '/ip dhcp-client add interface="ether1" use-peer-dns=yes' in s and 'comment="mtc WAN"' in s
    assert '/interface vlan add name="vlan10" interface=bridge vlan-id=10' in s
    assert '/ip address add address="10.99.0.1/24" interface="bridge"' in s
    assert '/ip pool add name=mtc-v20 ranges="10.20.0.100-10.20.0.254"' in s
    assert '/ip dhcp-server network add address="10.20.0.0/24" gateway="10.20.0.1" dns-server="9.9.9.9"' in s
    assert 'list member add list="GUEST" interface="vlan20"' in s and "action=masquerade out-interface-list=WAN" in s
    assert 'in-interface-list="GUEST" out-interface-list="WAN"' in s and 'in-interface-list="GUEST" out-interface-list="LAN"' not in s
    assert s.index("vlan-filtering=yes") < s.index("# --- brana") < s.index("input vychozi zakaz")
    assert "allow-remote-requests=yes" in s and "allow-remote-requests=no" not in s
    assert 'mac-winbox set allowed-interface-list="MGMT"' in s
    # PPPoE ve VLAN: heslo jen z tajemství, v náhledu maska
    gw = {"device_id": "x", "wan": {"mode": "pppoe", "vlan": 848, "user": "o2@o2"}, "dhcp": {}}
    m = _build(gw=gw, dns=["1.1.1.1"])
    assert m["gw"]["zones"]["WAN"] == ["ether1", "wan-vlan", "pppoe-wan"] and "tajne" not in str(m)
    s = switchcfg.config_body(m)
    assert 'interface vlan add name=wan-vlan interface="ether1" vlan-id=848' in s and 'password="********"' in s
    s = switchcfg.config_body(m, {"wan_password": "taj ne"})
    assert 'pppoe-client add name=pppoe-wan interface="wan-vlan" user="o2@o2" password="taj ne"' in s
    assert "use-peer-dns=no" in s and '/ip dns set servers="1.1.1.1"' in s
    gw = {"device_id": "x", "wan": {"mode": "static", "vlan": None, "address": "203.0.113.10/29", "gateway": "203.0.113.9",
                                    "dns": ["8.8.8.8"]}, "dhcp": {}}
    s = switchcfg.config_body(_build(gw=gw))
    assert 'address="203.0.113.10/29" interface="ether1"' in s and 'gateway="203.0.113.9" comment="mtc WAN"' in s
    b = switchcfg.bootstrap(_build(), "# adopt", "# checkin", reverted_url="https://c/x", auth_header="A: b",
                            check_certificate="yes", backup_password="PW")
    _ascii(b)
    assert b.count("{") == b.count("}") and "[/interface ethernet find]] < 5" in b


def managed_inv(m=None):
    """Inventura brány po převzetí podle modelu."""
    m = m or _build()
    inv = copy.deepcopy(INV)
    g = m["gw"]
    inv["bridges"] = [{"name": "bridge", "vlan-filtering": "true", "pvid": "99"}]
    inv["bridge_ports"] = [{"bridge": "bridge", "interface": p["port"], "pvid": str(p["pvid"]),
                            "frame-types": p["frame_types"], "trusted": "false"} for p in m["ports"]]
    inv["bridge_vlans"] = [{"bridge": "bridge", "vlan-ids": str(v["vid"]), "tagged": ",".join(v["tagged"]),
                            "untagged": ",".join(v["untagged"]), "comment": f"mtc:v{v['vid']}"} for v in m["vlans"]]
    for e in inv["ethernet"]:
        e["loop-protect"] = "off" if e["default-name"] in ("ether1", "ether3") else "on"
    inv["vlans"] = [{"name": n["iface"], "vlan-id": str(n["vid"]), "interface": "bridge"} for n in g["nets"] if n["iface"] != "bridge"]
    inv["addresses"] = [{"address": n["address"], "interface": n["iface"]} for n in g["nets"]] + \
        [{"address": "100.64.3.7/24", "interface": "ether1", "dynamic": "true"}]
    inv["ip_pools"] = [{"name": f"mtc-v{n['vid']}", "ranges": n["dhcp"]["ranges"]} for n in g["nets"] if n["dhcp"]]
    inv["dhcp_servers"] = [{"name": f"mtc-v{n['vid']}", "interface": n["iface"], "address-pool": f"mtc-v{n['vid']}",
                            "lease-time": "12h" if n["dhcp"]["lease"] == "12h" else "02:00:00"} for n in g["nets"] if n["dhcp"]]
    inv["dhcp_networks"] = [{"address": n["network"], "gateway": n["gateway"], "dns-server": ",".join(n["dhcp"]["dns"])}
                            for n in g["nets"] if n["dhcp"]]
    inv["if_lists"] = [{"name": z} for z in g["zones"]]
    inv["if_list_members"] = [{"list": z, "interface": i} for z, ms in g["zones"].items() for i in ms]
    inv["fw_admin"] = [{"list": "mtc-admin", "address": a} for a in g["admin"]]
    fw = [{"chain": "input", "action": "drop", "comment": "mtc-fw: input vychozi zakaz"}]
    fw += [{"chain": "forward", "action": p["action"], "comment": f"mtc-fw: {p['src']} -> {p['dst']}"} for p in g["policies"]]
    fw += [{"chain": "forward", "action": "accept", "comment": f"mtc-fw: {z} -> {z}"} for z in g["zones"] if z != "WAN"]
    fw.append({"chain": "forward", "action": "drop", "comment": "mtc-fw: forward vychozi zakaz"})
    inv["fw_filter"], inv["fw_nat"] = fw, [{"chain": "srcnat", "action": "masquerade", "comment": "mtc-fw: maskarada WAN"}]
    return inv


def test_reconcile():
    inv = managed_inv()
    r = switchcfg.reconcile(inv, _build(inv))
    assert r["changes"] == [] and r["apply"] == "", r
    # nová síť s novou zónou + změna rozsahu DHCP + zrušení sítě hostů
    raw = copy.deepcopy(INTENT_RAW)
    raw["networks"][3]["subnet"] = "10.30.0.0/24"
    raw["networks"].pop(2)
    gw = {"device_id": "x", "wan": {"mode": "dhcp", "port": "ether1", "vlan": None},
          "dhcp": {"10": {"enabled": True, "start": "10.10.0.50", "end": "10.10.0.99"}}}
    m = _build(inv, gw=gw, it=intent.validate(raw))
    assert m["blockers"] == [], m["blockers"]
    r = switchcfg.reconcile(inv, m)
    a, rv = r["apply"], r["revert"]
    _ascii(a + rv)
    assert not r["additive"]
    assert '/interface list add name="CAM"' in a and 'comment="mtc-fw: input sluzby CAM" place-before=' in a
    assert '/interface vlan add name="vlan30" interface=bridge vlan-id=30' in a and "mtc-v30" in a
    assert 'in-interface-list="CAM" out-interface-list="WAN" comment="mtc-fw: CAM -> WAN" place-before=' in a
    assert '/ip pool set [find name=mtc-v10] ranges="10.10.0.50-10.10.0.99"' in a
    # zrušená síť: DHCP server dřív než pool, rozhraní nakonec; politiky zóny GUEST pryč
    assert a.index("dhcp-server remove [find name=mtc-v20]") < a.index("pool remove [find name=mtc-v20]") \
        < a.index('/interface vlan remove [find name="vlan20"]')
    assert '/ip firewall filter remove [find comment="mtc-fw: GUEST -> WAN"]' in a
    # návrat: pool se vrátí dřív než DHCP server, nová zóna zmizí až po svých pravidlech a členech
    assert rv.index("/ip pool add name=mtc-v20") < rv.index("/ip dhcp-server add name=mtc-v20")
    assert rv.index('member remove [find list="CAM"') < rv.index('/interface list remove [find name="CAM"]')
    assert rv.index('/interface vlan add name="vlan20"') < rv.index("/ip pool add name=mtc-v20")
    assert '/ip pool set [find name=mtc-v10] ranges="10.10.0.100-10.10.0.254"' in rv
    assert "192.168.88" not in a and "dhcp-client" not in a      # WAN a cizí objekty se nemění
    # změna účelu sítě = jiná zóna; adresa správy routeru se drží se sítí managementu
    raw = copy.deepcopy(INTENT_RAW)
    raw["networks"][1]["purpose"] = "iot"
    raw["networks"][0]["subnet"], raw["networks"][0]["gateway"] = "10.98.0.0/24", "10.98.0.1"
    r = switchcfg.reconcile(inv, _build(inv, it=intent.validate(raw)))
    a = r["apply"]
    assert 'member remove [find list="LAN" interface="vlan10"]' in a and 'member add list="IOT" interface="vlan10"' in a
    assert '/ip address set [find interface="bridge" address="10.99.0.1/24"] address="10.98.0.1/24"' in a
    assert 'address-list add list=mtc-admin address="10.98.0.0/24"' in a
    assert 'address-list remove [find list=mtc-admin address="10.99.0.0/24"]' in a
    assert a.index("address-list add list=mtc-admin") < a.index("address-list remove [find list=mtc-admin")


def test_reconcile_legacy_default_config():
    """Brána z výchozí konfigurace role firewall (LAN 192.168.88.0/24, server mtc-lan) přejde na sítě lokality."""
    inv = managed_inv()
    inv["dhcp_servers"] = [{"name": "mtc-lan", "interface": "bridge", "address-pool": "mtc-lan", "lease-time": "8h"}]
    inv["ip_pools"] = [{"name": "mtc-lan", "ranges": "192.168.88.10-192.168.88.254"}]
    inv["dhcp_networks"] = [{"address": "192.168.88.0/24", "gateway": "192.168.88.1", "dns-server": "192.168.88.1"}]
    inv["addresses"] = [{"address": "192.168.88.1/24", "interface": "bridge"}]
    inv["vlans"] = []
    r = switchcfg.reconcile(inv, _build(inv))
    a = r["apply"]
    assert a.index("dhcp-server remove [find name=mtc-lan]") < a.index("/ip dhcp-server add name=mtc-v99")
    assert 'network remove [find address="192.168.88.0/24"]' in a
    assert '/ip address set [find interface="bridge" address="192.168.88.1/24"] address="10.99.0.1/24"' in a
    # bez firewallu brány (zařízení nepřevzaté jako brána) se L3 neslaďuje naslepo
    inv["fw_filter"] = []
    assert "dhcp-server" not in switchcfg.reconcile(inv, _build(inv))["apply"]


def test_gateway_takeover_flow():
    """API: nastavení brány (PPPoE, heslo jen v tajemství) → převzetí → řízená brána → sladění."""
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "GW")
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET role = 'gateway', model = 'RB5009UG+S+' WHERE id = %(d)s", {"d": gw})
        inventory.store(db, tid, gw, copy.deepcopy(INV), [])
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        assert c.put(base + f"/sites/{sid}/intent", json_body=INTENT_RAW).status_code == 200
        v = c.get(base + f"/devices/{gw}/gateway").json()
        assert v["configured"] is False and v["wan_guess"] == "ether1" and v["zones"]["GUEST"] == ["vlan20"], v
        assert [n["plan"] is not None for n in v["networks"]] == [True, True, False, True]
        # PPPoE bez hesla nejde; heslo se uloží jako tajemství, ne do nastavení ani do plánu
        body = {"wan": {"mode": "pppoe", "port": "ether1", "vlan": 848, "user": "o2@o2"}, "dhcp": {"20": {"lease": "1h"}}}
        r = c.put(base + f"/devices/{gw}/gateway", json_body=body)
        assert r.status_code == 400 and "heslo" in r.json()["detail"]
        body["wan"]["password"] = "Tajne-Heslo-77"
        r = c.put(base + f"/devices/{gw}/gateway", json_body=body)
        assert r.status_code == 200, r.text
        v = c.get(base + f"/devices/{gw}/gateway").json()
        assert v["configured"] and v["wan"]["has_password"] and "Tajne" not in str(v)
        with operator_scope(db) as tx:
            raw = tx.fetchone("SELECT data::text AS d FROM site_policy WHERE site_id = %(s)s AND module = 'gateway'", {"s": sid})
            assert "Tajne" not in raw["d"] and '"user": "o2@o2"' in raw["d"]
        pv = c.get(base + f"/devices/{gw}/takeover").json()
        assert pv["blockers"] == [] and pv["model"]["gw"]["wan"]["iface"] == "pppoe-wan", pv
        assert pv["classification"]["class"] == "factory"
        r = c.post(base + f"/devices/{gw}/takeover", json_body={})
        assert r.status_code == 201 and "Převzetí brány" in r.json()["summary"], r.text
        pid = r.json()["plan_id"]
        with operator_scope(db) as tx:
            p = tx.fetchone("SELECT script, changes::text AS ch FROM config_plan WHERE id = %(i)s", {"i": pid})
        assert "Tajne" not in p["script"] and "Tajne" not in p["ch"] and 'password="********"' in p["script"]
        cid = c.post(base + f"/plans/{pid}/approve", json_body={}).json()["command_id"]
        assert "backup save" in _device_post(app, "/api/checkin", "key-GW1", {"sstp": "up"}).text
        b = _device_post(app, f"/api/checkin/bootstrap?id={cid}", "key-GW1").text
        _ascii(b)
        assert 'user="o2@o2" password="Tajne-Heslo-77"' in b and "mtc-fw: forward vychozi zakaz" in b
        assert 'lease-time=1h' in b and "mtc-checkin" in b
        # správa controllerem (pravidla mtc:) se zakládá až po firewallu brány – přesune se nad výchozí zákaz
        assert b.index("mtc-fw: input vychozi zakaz") < b.index("mtc: sprava REST")
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-GW1")
        s = _device_post(app, "/api/checkin", "key-GW1", {"sstp": "up", "tk": 1, "fp": "cd" * 32}).text
        ccid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={ccid}&status=done", "key-GW1")
        with operator_scope(db) as tx:
            d = tx.fetchone("SELECT managed_at, role::text AS role FROM device WHERE id = %(d)s", {"d": gw})
        assert d["managed_at"] is not None and d["role"] == "gateway"
        # řízená brána: inventura podle modelu → nic; nová síť s podsítí → plán ke schválení (ne automaticky)
        gwi = {"device_id": gw, "wan": {"mode": "pppoe", "port": "ether1", "vlan": 848, "user": "o2@o2"},
               "dhcp": {"20": {"enabled": True, "lease": "1h"}}}
        inv = managed_inv(_build(gw=gwi))
        for srv in inv["dhcp_servers"]:
            srv["lease-time"] = "01:00:00" if srv["name"] == "mtc-v20" else "12h"
        inventory.store(db, tid, gw, inv, [])
        r = c.post(base + f"/sites/{sid}/intent/reconcile", json_body={}).json()
        assert r["devices"][0]["result"] == "ok", (r, _plan_script(db, r["devices"][0].get("plan_id")))
        more = copy.deepcopy(INTENT_RAW)
        more["networks"][3]["subnet"] = "10.30.0.0/24"
        assert c.put(base + f"/sites/{sid}/intent", json_body=more).status_code == 200
        r = c.post(base + f"/sites/{sid}/intent/reconcile", json_body={}).json()
        assert r["devices"][0]["result"] == "plan" and "zóna CAM" in r["devices"][0]["summary"], r
        # WAN řízené brány za provozu měnit nejde (zatím)
        body["wan"]["mode"] = "dhcp"
        r = c.put(base + f"/devices/{gw}/gateway", json_body=body)
        assert r.status_code == 400 and "WAN" in r.json()["detail"]
    finally:
        drop_db(name)


def _plan_script(db, pid):
    from mtcontroller.db import operator_scope

    if not pid:
        return None
    with operator_scope(db) as tx:
        return tx.fetchone("SELECT script FROM config_plan WHERE id = %(i)s", {"i": pid})["script"]
