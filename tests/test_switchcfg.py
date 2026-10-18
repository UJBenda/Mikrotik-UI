"""Řízený režim F5a: záměr lokality, model switche, bootstrap převzetí a sladění."""

import copy
import re

from mtcontroller import intent, switchcfg

INTENT = intent.validate({
    "networks": [
        {"name": "Management", "vlan_id": 99, "purpose": "mgmt", "subnet": "192.168.77.0/24", "gateway": "192.168.77.1"},
        {"name": "Kancelář", "vlan_id": 10, "purpose": "lan"},
        {"name": "Hosté", "vlan_id": 20, "purpose": "hoste"},
    ],
})

GW_MAC = "AA:BB:CC:00:00:01"

INV = {
    "identity": {"name": "SW-sklad"},
    "interfaces": [{"name": n, "type": "ether"} for n in ("ether1", "ether2", "ether3", "ether4", "ether5-tiskarna", "sfp1")]
    + [{"name": "bridge", "type": "bridge"}],
    "ethernet": [{"name": n, "default-name": d, "loop-protect": "default"} for n, d in
                 (("ether1", "ether1"), ("ether2", "ether2"), ("ether3", "ether3"), ("ether4", "ether4"),
                  ("ether5-tiskarna", "ether5"), ("sfp1", "sfp1"))],
    "bridges": [{"name": "bridge", "vlan-filtering": "false", "protocol-mode": "rstp"}],
    "bridge_ports": [{"bridge": "bridge", "interface": n} for n in ("ether1", "ether2", "ether3", "ether4", "ether5-tiskarna", "sfp1")],
    "addresses": [{"address": "192.168.77.50/24", "interface": "bridge", "dynamic": "true"}],
    "dhcp_client": [{"interface": "bridge", "status": "bound", "address": "192.168.77.50/24", "gateway": "192.168.77.1"}],
    "routes_default": [{"dst-address": "0.0.0.0/0", "gateway": "192.168.77.1", "immediate-gw": "192.168.77.1%bridge",
                        "active": "true"}],
    "arp": [{"address": "192.168.77.1", "mac-address": GW_MAC, "interface": "bridge"}],
    "bridge_hosts": [{"mac-address": GW_MAC, "on-interface": "ether1", "bridge": "bridge"},
                     {"mac-address": "AA:BB:CC:00:00:99", "on-interface": "bridge", "local": "true"}],
    "neighbors": [
        {"interface": "ether3,bridge", "identity": "cAP-chodba", "platform": "MikroTik", "board": "cAPGi-5HaxD2HaxD"},
        {"interface": "ether4", "identity": "SIP-T46U", "platform": "Yealink", "system-caps-enabled": "bridge,telephone"},
    ],
    "fw_filter": [], "fw_nat": [],
}


def test_intent_validate():
    assert INTENT["mgmt_vlan"] == 99 and INTENT["access_vlan"] == 10 and INTENT["mgmt_native"] is True
    for bad, msg in [
        ({"networks": []}, "aspoň jednu"),
        ({"networks": [{"name": "a", "vlan_id": 5, "purpose": "lan"}]}, "management"),
        ({"networks": [{"name": "a", "vlan_id": 5, "purpose": "mgmt"}, {"name": "b", "vlan_id": 5}]}, "VLAN 5"),
        ({"networks": [{"name": "a", "vlan_id": 5000, "purpose": "mgmt"}]}, "1–4094"),
        ({"networks": [{"name": "a", "vlan_id": 5, "purpose": "mgmt", "subnet": "10.0.0.0/24", "gateway": "10.1.0.1"}]}, "není v podsíti"),
        ({"networks": [{"name": "a", "vlan_id": 5, "purpose": "mgmt"}], "access_vlan": 7}, "výchozí síť"),
    ]:
        try:
            intent.validate(bad)
            raise AssertionError(f"prošlo: {bad}")
        except intent.IntentError as e:
            assert msg in str(e), (msg, str(e))


def test_device_intent():
    d = intent.validate_device({"ports": {"ether2": {"mode": "access", "vlan": 20}, "sfp1": {"mode": "off"}},
                                "mgmt": {"mode": "static", "address": "192.168.77.11/24"}}, INTENT, {"ether2", "sfp1"})
    assert d["ports"]["ether2"] == {"mode": "access", "vlan": 20} and d["mgmt"]["address"] == "192.168.77.11/24"
    for bad in ({"ports": {"ether2": {"mode": "access", "vlan": 30}}}, {"ports": {"x;y": {"mode": "off"}}},
                {"mgmt": {"mode": "static", "address": "10.0.0.5/24"}}, {"ports": {"ether9": {"mode": "off"}}}):
        try:
            intent.validate_device(bad, INTENT, {"ether2", "sfp1"})
            raise AssertionError(f"prošlo: {bad}")
        except intent.IntentError:
            pass


def test_mgmt_path_bridge():
    p = switchcfg.mgmt_path(INV)
    assert p["uplink"] == "ether1" and p["mode"] == "dhcp" and p["tagged_vid"] is None
    assert p["gateway"] == "192.168.77.1" and "MAC brány" in p["how"]


def test_mgmt_path_port_and_vlan():
    # hEX defconf: DHCP klient přímo na ether1
    inv = {"ethernet": [{"name": "ether1", "default-name": "ether1"}, {"name": "ether2", "default-name": "ether2"}],
           "dhcp_client": [{"interface": "ether1", "status": "bound", "address": "10.0.0.5/24", "gateway": "10.0.0.1"}],
           "routes_default": [{"gateway": "10.0.0.1", "immediate-gw": "10.0.0.1%ether1", "active": "true"}]}
    p = switchcfg.mgmt_path(inv)
    assert p["uplink"] == "ether1" and p["how"] == "L3 přímo na portu"
    # tagovaná mgmt: VLAN rozhraní na bridgi, statická adresa, uplink z root portu
    inv = {"ethernet": [{"name": "sfp1", "default-name": "sfp1"}], "bridges": [{"name": "br"}],
           "vlans": [{"name": "mgmt", "vlan-id": "99", "interface": "br"}],
           "addresses": [{"address": "10.9.9.5/24", "interface": "mgmt"}],
           "routes_default": [{"gateway": "10.9.9.1", "immediate-gw": "10.9.9.1%mgmt", "active": "true"}],
           "dns": {"servers": "10.9.9.1"},
           "stp_ports": [{"interface": "sfp1", "role": "root-port"}]}
    p = switchcfg.mgmt_path(inv)
    assert p == {"l3": "mgmt", "address": "10.9.9.5/24", "gateway": "10.9.9.1", "mode": "static", "uplink": "sfp1",
                 "tagged_vid": 99, "how": "root port RSTP", "dns": ["10.9.9.1"]}


def test_build_model():
    m = switchcfg.build(INV, INTENT, {"ports": {"ether5-tiskarna": {"mode": "access", "vlan": 20}, "sfp1": {"mode": "off"}}},
                        model_name="CRS326-24G-2S+", identity="SW-sklad")
    assert m["blockers"] == []
    ports = {p["port"]: p for p in m["ports"]}
    assert ports["ether1"]["mode"] == "trunk" and ports["ether1"]["trusted"] and ports["ether1"]["pvid"] == 99
    assert ports["ether3"]["mode"] == "trunk" and "cAP-chodba" in ports["ether3"]["reason"]
    assert ports["ether4"]["mode"] == "access" and ports["ether4"]["pvid"] == 10    # telefon není trunk
    assert ports["ether5"]["pvid"] == 20 and ports["ether5"]["comment"] == "ether5-tiskarna"
    assert ports["sfp1"]["mode"] == "off"
    vl = {v["vid"]: v for v in m["vlans"]}
    assert vl[99] == {"vid": 99, "name": "Management", "tagged": [], "untagged": ["bridge", "ether1", "ether3"]}
    assert vl[10]["tagged"] == ["ether1", "ether3"] and vl[10]["untagged"] == ["ether2", "ether4"]
    assert vl[20]["untagged"] == ["ether5"]
    assert m["mgmt"] == {"vid": 99, "native": True, "mode": "dhcp"} and m["snooping"] is True


def test_build_blockers():
    tagged = dict(INTENT, mgmt_native=False)
    m = switchcfg.build(INV, tagged, {})
    assert any("tagovaný ve VLAN 99" in b for b in m["blockers"])
    m = switchcfg.build(INV, INTENT, {"ports": {"ether1": {"mode": "access"}}})
    assert any("uplink" in b for b in m["blockers"])
    inv = copy.deepcopy(INV)
    inv["bridge_hosts"] = []
    m = switchcfg.build(inv, INTENT, {})
    assert any("uplink" in b for b in m["blockers"])
    # tagovaná mgmt na trunkách: trunk jen tagované rámce, mgmt tagovaná
    inv = copy.deepcopy(INV)
    inv["vlans"] = [{"name": "vlan99", "vlan-id": "99", "interface": "bridge"}]
    inv["dhcp_client"][0]["interface"] = "vlan99"
    inv["routes_default"][0]["immediate-gw"] = "192.168.77.1%vlan99"
    m = switchcfg.build(inv, tagged, {}, model_name="RB5009")
    assert m["blockers"] == [] and m["snooping"] is False
    up = next(p for p in m["ports"] if p["port"] == "ether1")
    assert up["frame_types"] == switchcfg.FT_TRUNK_TAGGED
    assert next(v for v in m["vlans"] if v["vid"] == 99)["tagged"] == ["ether1", "ether3"]


def test_static_mgmt_kept():
    inv = copy.deepcopy(INV)
    inv["dhcp_client"] = []
    inv["addresses"] = [{"address": "192.168.77.50/24", "interface": "bridge"}]
    inv["dns"] = {"servers": "192.168.77.1"}
    m = switchcfg.build(inv, INTENT, {})
    assert m["mgmt"]["mode"] == "static" and m["mgmt"]["address"] == "192.168.77.50/24"
    assert m["mgmt"]["gateway"] == "192.168.77.1" and m["mgmt"]["dns"] == ["192.168.77.1"]


def test_classify():
    assert switchcfg.classify(INV)["class"] == "simple"
    defconf = dict(INV, addresses=[{"address": "192.168.88.1/24", "interface": "bridge"}],
                   fw_filter=[{"chain": "input", "comment": "defconf: accept established"}],
                   fw_nat=[{"chain": "srcnat", "comment": "defconf: masquerade"}],
                   dhcp_servers=[{"name": "defconf", "interface": "bridge"}])
    c = switchcfg.classify(defconf)
    assert c["class"] == "factory" and any("DHCP server defconf" in r for r in c["removed"])
    router = dict(INV, fw_nat=[{"chain": "srcnat", "action": "masquerade"}],
                  interfaces=INV["interfaces"] + [{"name": "pppoe-out1", "type": "pppoe-out"}])
    c = switchcfg.classify(router)
    assert c["class"] == "custom" and "vlastní NAT" in c["custom"] and any("pppoe" in r for r in c["removed"])


def _ascii(script):
    bad = [ln for ln in script.splitlines() if not re.fullmatch(r"[\x20-\x7e\t]*", ln)]
    assert not bad, bad[:3]


def test_bootstrap_script():
    m = switchcfg.build(INV, INTENT, {"ports": {"sfp1": {"mode": "off"}}}, model_name="CRS326", identity="Sklad – přízemí")
    s = switchcfg.bootstrap(m, "/interface sstp-client add name=\"mtc-sstp\"", "/system script add name=\"mtc-checkin\"",
                            reverted_url="https://c.example/api/checkin/ack?id=7&status=reverted",
                            auth_header="Authorization: Bearer KEY", check_certificate="yes-without-crl",
                            backup_password="PW")
    _ascii(s)
    assert s.startswith("# MikroTik controller") and s.count("{") == s.count("}")
    # návrat se nastaví dřív než konfigurace; vlan-filtering až po VLAN tabulce
    assert s.index("mtc-revert") < s.index("/interface bridge add")
    assert s.index("vlan-ids=99") < s.index("vlan-filtering=yes")
    assert 'comment="mtc-takeover"' in s and "backup load" in s and "Sklad - prizemi" in s
    assert "pvid=99" in s and 'untagged="bridge,ether1,ether3"' in s and "disabled=yes" in s
    assert "dhcp-client add interface=bridge" in s and 'mtc ether5-tiskarna' in s
    assert ':if (!$ok) do={ /system script run [find name="mtc-revert"] }' in s
    _ascii(switchcfg.takeover_body("https://c.example/api/checkin/bootstrap?id=7", "Authorization: Bearer KEY",
                                   "yes-without-crl", "PW"))
    _ascii(switchcfg.TAKEOVER_TAIL + switchcfg.TAKEOVER_CONFIRM)


def _managed_inv():
    """Inventura zařízení po převzetí (podle modelu bez výjimek)."""
    m = switchcfg.build(INV, INTENT, {}, model_name="CRS326")
    inv = copy.deepcopy(INV)
    inv["bridges"] = [{"name": "bridge", "vlan-filtering": "true", "pvid": "99"}]
    inv["bridge_ports"] = [{"bridge": "bridge", "interface": next(e["name"] for e in INV["ethernet"] if e["default-name"] == p["port"]),
                            "pvid": str(p["pvid"]), "frame-types": p["frame_types"], "trusted": "true" if p["trusted"] else "false"}
                           for p in m["ports"]]
    names = {"ether5": "ether5-tiskarna"}
    inv["bridge_vlans"] = [{"bridge": "bridge", "vlan-ids": str(v["vid"]), "tagged": ",".join(names.get(x, x) for x in v["tagged"]),
                            "untagged": ",".join(names.get(x, x) for x in v["untagged"]), "comment": f"mtc:v{v['vid']}"}
                           for v in m["vlans"]]
    for e in inv["ethernet"]:
        e["loop-protect"] = "off" if e["default-name"] in ("ether1", "ether3") else "on"
    return inv


def test_reconcile_nothing_and_new_vlan():
    inv = _managed_inv()
    r = switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, model_name="CRS326"))
    assert r["changes"] == [] and r["apply"] == "" and r["additive"] is False
    more = intent.validate({"networks": INTENT["networks"] + [{"name": "Kamery", "vlan_id": 30, "purpose": "kamery"}]})
    r = switchcfg.reconcile(inv, switchcfg.build(inv, more, {}, model_name="CRS326"))
    assert r["changes"] == ["nová VLAN 30 (Kamery)"] and r["additive"] is True
    assert 'vlan-ids=30 tagged="ether1,ether3" untagged=""' in r["apply"]
    assert r["revert"].strip() == "/interface bridge vlan remove [find bridge=bridge vlan-ids=30 dynamic=no]"
    _ascii(r["apply"] + r["revert"])


def test_reconcile_new_neighbor_and_port_change():
    inv = _managed_inv()
    # na ether2 se objevil spravovaný switch → trunk
    inv["neighbors"].append({"interface": "ether2", "identity": "SW-2", "platform": "MikroTik"})
    r = switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, model_name="CRS326"))
    assert any(c.startswith("ether2: pvid 10 → 99") for c in r["changes"]) and r["additive"] is False
    assert "/interface bridge port set [find bridge=bridge interface=\"ether2\"] pvid=99 frame-types=admit-all" in r["apply"]
    assert "pvid=10 frame-types=admit-only-untagged-and-priority-tagged" in r["revert"]
    # port tiskárny do sítě hostů (přejmenovaný port)
    inv = _managed_inv()
    r = switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {"ports": {"ether5-tiskarna": {"mode": "access", "vlan": 20}}},
                                                 model_name="CRS326"))
    assert '[find bridge=bridge interface="ether5-tiskarna"] pvid=20' in r["apply"]
    assert any(c.startswith("VLAN 10") for c in r["changes"]) and any(c.startswith("VLAN 20") for c in r["changes"])


def test_basis_changes():
    inv = _managed_inv()
    b = switchcfg.basis(inv)
    inv["bridge_ports"][1]["pvid"] = "20"
    assert switchcfg.basis(inv) != b


def test_from_observed():
    d = intent.from_observed([{"name": "bridge", "vlan_id": 1, "tagged": False, "subnet": "192.168.77.0/24", "gateway": "192.168.77.1"},
                              {"name": "vlan20", "comment": "hoste", "vlan_id": 20, "tagged": True, "subnet": "10.20.0.0/24"}])
    v = intent.validate(d)
    assert v["mgmt_vlan"] == 1 and v["access_vlan"] == 20


# --- celý tok přes API a check-in (DB) ---------------------------------------------------------------

def _takeover_env():
    from test_modules import _adopted, _env

    from mtcontroller import inventory

    db, name, settings, box, app, tid, sid = _env()
    sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "SW-sklad")
    inventory.store(db, tid, sw, copy.deepcopy(INV), [])
    return db, name, settings, box, app, tid, sid, sw


def _plan(db, pid):
    from mtcontroller.db import operator_scope

    with operator_scope(db) as tx:
        return tx.fetchone("SELECT status::text AS s, module FROM config_plan WHERE id = %(i)s", {"i": pid})


def _dev(db, did):
    from mtcontroller.db import operator_scope

    with operator_scope(db) as tx:
        return tx.fetchone("SELECT managed_at, role::text AS role, api_cert_fingerprint AS fp FROM device WHERE id = %(d)s",
                           {"d": did})


INTENT_RAW = {"networks": [
    {"name": "Management", "vlan_id": 99, "purpose": "mgmt", "subnet": "192.168.77.0/24", "gateway": "192.168.77.1"},
    {"name": "Kancelář", "vlan_id": 10, "purpose": "lan"},
    {"name": "Hosté", "vlan_id": 20, "purpose": "hoste"}]}


def _start_takeover(app, db, tid, sid, sw, c=None):
    from test_flow import _device_post, _login

    base = f"/api/admin/tenants/{tid}"
    if c is not None:
        return (c,) + _approve(app, c, base, sw)
    c = _login(app)
    pv = c.get(base + f"/devices/{sw}/takeover").json()
    assert pv["has_intent"] is False and "záměr" in pv["blockers"][0]
    assert c.get(base + f"/sites/{sid}/intent").json()["intent"] is None
    r = c.put(base + f"/sites/{sid}/intent", json_body=INTENT_RAW)
    assert r.status_code == 200, r.text
    assert c.put(base + f"/sites/{sid}/intent", json_body={"networks": []}).status_code == 400
    r = c.put(base + f"/devices/{sw}/intent", json_body={"ports": {"sfp1": {"mode": "off"}}})
    assert r.status_code == 200, r.text
    pv = c.get(base + f"/devices/{sw}/takeover").json()
    assert pv["blockers"] == [] and pv["classification"]["class"] == "simple", pv
    assert {p["port"]: p["mode"] for p in pv["model"]["ports"]}["sfp1"] == "off"
    return (c,) + _approve(app, c, base, sw)


def _approve(app, c, base, sw):
    from test_flow import _device_post

    r = c.post(base + f"/devices/{sw}/takeover", json_body={})
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    r = c.post(base + f"/plans/{pid}/approve", json_body={})
    assert r.status_code == 200, r.text
    cid = r.json()["command_id"]
    s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
    assert "backup save" in s and f"api/checkin/bootstrap\\?id={cid}" in s and "reset-configuration keep-users=yes" in s, s
    assert s.index("&status=") < s.index("reset-configuration")  # reset až po ack
    return pid, cid


def test_takeover_flow():
    from helpers import drop_db
    from test_flow import _device_post

    from mtcontroller import inventory

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        c, pid, cid = _start_takeover(app, db, tid, sid, sw)
        base = f"/api/admin/tenants/{tid}"
        # bootstrap jen se správným klíčem a jen pro probíhající příkaz
        assert _device_post(app, f"/api/checkin/bootstrap?id={cid}", "spatny-klic").status_code == 401
        assert _device_post(app, f"/api/checkin/bootstrap?id={cid + 99}", "key-SW1").status_code == 404
        b = _device_post(app, f"/api/checkin/bootstrap?id={cid}", "key-SW1").text
        assert "vlan-ids=99" in b and 'comment="mtc-takeover"' in b and "mtc-checkin" in b and "pw-SW1" in b
        assert "********" not in b and 'name="mtc-sstp"' in b
        _ascii(b)
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        assert _plan(db, pid)["s"] == "confirming"
        # ozvání ze staré konfigurace (před resetem) ani bez tunelu převzetí nepotvrdí
        assert "nic k provedeni" in _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up", "tk": 0}).text
        assert "nic k provedeni" in _device_post(app, "/api/checkin", "key-SW1", {"sstp": "down", "tk": 1}).text
        # bootstrap běží, tunel nahoře, nový certifikát REST
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up", "tk": 1, "fp": "cd" * 32}).text
        assert "prevzeti neni aktivni" in s and "backup.backup" not in s
        assert _dev(db, sw)["fp"] == "cd" * 32
        ccid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={ccid}&status=done", "key-SW1")
        assert _plan(db, pid)["s"] == "applied"
        d = _dev(db, sw)
        assert d["managed_at"] is not None and d["role"] == "switch"
        assert c.post(base + f"/devices/{sw}/takeover", json_body={}).status_code == 400  # už řízené
        # řízené zařízení: inventura odpovídá modelu → sladění nic nemění
        assert c.put(base + f"/devices/{sw}/intent", json_body={"ports": {}}).status_code == 200
        inventory.store(db, tid, sw, _managed_inv(), [])
        r = c.post(base + f"/sites/{sid}/intent/reconcile", json_body={}).json()
        assert r["devices"][0]["result"] == "ok", r
        # nová síť = jen přidání tagované VLAN → plán se schválí sám
        more = dict(INTENT_RAW, networks=INTENT_RAW["networks"] + [{"name": "Kamery", "vlan_id": 30, "purpose": "kamery"}])
        assert c.put(base + f"/sites/{sid}/intent", json_body=more).status_code == 200
        r = c.post(base + f"/sites/{sid}/intent/reconcile", json_body={}).json()
        assert r["devices"][0]["result"] == "auto", r
        assert _plan(db, r["devices"][0]["plan_id"])["s"] == "queued"
        # management řízené lokality nejde změnit
        bad = dict(INTENT_RAW, mgmt_native=False)
        assert "management" in c.put(base + f"/sites/{sid}/intent", json_body=bad).json()["detail"]
    finally:
        drop_db(name)


def test_takeover_rollback():
    from helpers import drop_db
    from test_flow import _device_post

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        c, pid, cid = _start_takeover(app, db, tid, sid, sw)
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        # bootstrap běží s novým certifikátem…
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "down", "tk": 1, "fp": "cd" * 32}).text
        assert _dev(db, sw)["fp"] == "cd" * 32
        # …ale tunel nenaběhl: zařízení se vrátilo ze zálohy a nahlásilo to
        assert _device_post(app, f"/api/checkin/ack?id={cid}&status=reverted", "key-SW1").text == "ok\n"
        assert _plan(db, pid)["s"] == "rolled_back"
        d = _dev(db, sw)
        assert d["managed_at"] is None and d["fp"] == "ab" * 32  # zpět původní certifikát
        # potvrzení, které na zařízení nenajde běžící převzetí (návrat bez hlášení) → rolled_back
        from mtcontroller import inventory

        inventory.store(db, tid, sw, copy.deepcopy(INV), [])  # po návratu nová inventura
        c, pid2, cid2 = _start_takeover(app, db, tid, sid, sw, c)
        _device_post(app, f"/api/checkin/ack?id={cid2}&status=done", "key-SW1")
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up", "tk": 1}).text
        ccid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={ccid}&status=failed", "key-SW1")
        assert _plan(db, pid2)["s"] == "rolled_back" and _dev(db, sw)["managed_at"] is None
        # návrat bez hlášení i bez potvrzení: po termínu se ozve stará konfigurace (bez tk) → rolled_back
        from mtcontroller.db import operator_scope

        inventory.store(db, tid, sw, copy.deepcopy(INV), [])
        c, pid3, cid3 = _start_takeover(app, db, tid, sid, sw, c)
        _device_post(app, f"/api/checkin/ack?id={cid3}&status=done", "key-SW1")
        _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"})
        assert _plan(db, pid3)["s"] == "confirming"  # ještě v termínu
        with operator_scope(db) as tx:
            tx.execute("UPDATE config_plan SET confirm_deadline = now() - interval '1 minute' WHERE id = %(i)s", {"i": pid3})
        _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"})
        assert _plan(db, pid3)["s"] == "rolled_back"
    finally:
        drop_db(name)


def test_takeover_custom_needs_confirm():
    from helpers import drop_db
    from test_flow import _login

    from mtcontroller import inventory

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        inv = copy.deepcopy(INV)
        inv["fw_nat"] = [{"chain": "srcnat", "action": "masquerade"}]
        inventory.store(db, tid, sw, inv, [])
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        c.put(base + f"/sites/{sid}/intent", json_body=INTENT_RAW)
        r = c.post(base + f"/devices/{sw}/takeover", json_body={})
        assert r.status_code == 400 and "potvrď" in r.json()["detail"]
        r = c.post(base + f"/devices/{sw}/takeover", json_body={"confirm_custom": True})
        assert r.status_code == 201 and "NAT: 1 pravidel" in r.json()["removed"]
    finally:
        drop_db(name)
