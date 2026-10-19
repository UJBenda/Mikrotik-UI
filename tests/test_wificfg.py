"""Řízený režim F5b: Wi-Fi přes CAPsMAN – SSID, rádia, plán kanálů, CAP na AP, hesla jen šifrovaně."""

import copy
import re

import pytest

from mtcontroller import intent, routeros, switchcfg, wificfg
from test_routeros import _balanced
from test_switchcfg import INTENT, INV, _managed_inv

AP_INV = dict(copy.deepcopy(INV),
              wifi_radios=[{"interface": "wifi1", "radio-mac": "48:A9:8A:00:00:01", "bands": "5ghz-a,5ghz-n,5ghz-ac,5ghz-ax"},
                           {"interface": "wifi2", "radio-mac": "48:a9:8a:00:00:02", "bands": "2ghz-g,2ghz-n,2ghz-ax"}],
              wifi=[{"name": "wifi1", "default-name": "wifi1"}, {"name": "wifi2", "default-name": "wifi2"}],
              wifi_monitor=[{"name": "wifi1", "channel": "5500/ax/Ceee"}],
              wifi_cap={"enabled": "false", "discovery-interfaces": "", "caps-man-names": "", "lock-to-caps-man": "false"})

SSIDS = [
    {"id": "0b9a3c1e-0000-4000-8000-000000000001", "ssid": "Firma", "vlan_id": 10, "bands": "2.4,5",
     "isolation": False, "ft": True, "enabled": True},
    {"id": "0b9a3c1e-0000-4000-8000-000000000002", "ssid": "Hoste", "vlan_id": 20, "bands": "2.4,5",
     "isolation": True, "ft": True, "enabled": True},
    {"id": "0b9a3c1e-0000-4000-8000-000000000003", "ssid": "IoT", "vlan_id": 10, "bands": "2.4",
     "isolation": False, "ft": False, "enabled": True},
]
POLICY = {"country": "Czech Republic", "dfs": True, "max_width": 80}


def test_radios_and_support():
    r = wificfg.radios(AP_INV)
    assert r == [{"interface": "wifi1", "mac": "48:A9:8A:00:00:01", "band": "5", "std": "5ghz-ax"},
                 {"interface": "wifi2", "mac": "48:A9:8A:00:00:02", "band": "2.4", "std": "2ghz-ax"}]
    assert wificfg.supports_wifi(AP_INV) is True
    assert wificfg.supports_wifi({"wireless": [{"name": "wlan1"}]}) is False
    assert wificfg.supports_wifi(INV) is None
    ac = {"wifi_radios": [{"interface": "wifi1", "radio-mac": "AA:00:00:00:00:01", "bands": "5ghz-a,5ghz-n,5ghz-ac"}]}
    assert wificfg.radios(ac)[0]["std"] == "5ghz-ac"


def _aps():
    a = [{"id": "d1", "identity": "AP-chodba"}, {"id": "d2", "identity": "AP-sklad"}]
    inv2 = copy.deepcopy(AP_INV)
    inv2["wifi_radios"][0]["radio-mac"] = "48:A9:8A:00:00:11"
    inv2["wifi_radios"][1]["radio-mac"] = "48:A9:8A:00:00:12"
    inv2["wifi_monitor"] = [{"name": "wifi1", "channel": "5500/ax/Ceee"}]
    return a, {"d1": AP_INV, "d2": inv2}


def test_manager_script():
    aps, invs = _aps()
    m = wificfg.manager(SSIDS, wificfg.site_radios(aps, invs), POLICY, host_has_radios=False)
    s = m["script"]
    assert m["blockers"] == [] and len(m["radios"]) == 4
    s.encode("ascii")
    assert _balanced(s)
    # hesla jen jako token, FT/over-DS jen kde je zapnuté, hosté izolovaní
    assert "passphrase=MTCPSK0b9a3c1e000040008000000000000001 ft=yes ft-over-ds=yes" in s
    assert "passphrase=MTCPSK0b9a3c1e000040008000000000000003 ft=no" in s
    # VLAN neřeší datapath (ac ji neumí) – jen izolace; do VLAN dá rozhraní skript na AP
    assert 'name="mtc-dp2" client-isolation=yes' in s and "vlan-id" not in s
    assert m["config_map"]["mtc-s2"] == SSIDS[1]["id"] and m["config_map"]["mtc-r-48a98a000001"] == SSIDS[0]["id"]
    # každé rádio: kanál + master konfigurace + provisioning; 5 GHz bez IoT, 2,4 GHz se třemi SSID
    assert s.count("/interface wifi provisioning add") == 4
    assert 'radio-mac=48:A9:8A:00:00:01 action=create-dynamic-enabled master-configuration="mtc-r-48a98a000001" slave-configurations="mtc-s2"' in s
    assert 'master-configuration="mtc-r-48a98a000002" slave-configurations="mtc-s2,mtc-s3"' in s
    assert 'band=5ghz-ax width=20/40/80mhz frequency=' in s and "band=2ghz-ax width=20mhz frequency=" in s
    # dvě AP v 5 GHz nedostanou stejný kanál
    f5 = re.findall(r"band=5ghz-ax width=\S+ frequency=(\d+)", s)
    assert len(set(f5)) == 2
    assert s.index("remove [find comment=\"mtc\"]") < s.index("steering add") < s.index("steering=\"mtc-steer\"")
    assert "capsman set enabled=yes interfaces=bridge" in s and 'country="Czech Republic"' in s
    assert "caps-man-addresses=127.0.0.1" not in s


def test_manager_limits_and_dfs():
    aps, invs = _aps()
    rad = wificfg.site_radios(aps, invs)
    many = [dict(SSIDS[0], id=f"0b9a3c1e-0000-4000-8000-0000000001{i:02d}", ssid=f"S{i}") for i in range(9)]
    assert any("nejvýš 8" in b for b in wificfg.manager(many, rad, POLICY, host_has_radios=False)["blockers"])
    m = wificfg.manager(many[:5], rad, POLICY, host_has_radios=False)
    assert m["blockers"] == [] and any("nad 4" in n for n in m["notes"])
    assert wificfg.manager([], [], POLICY, host_has_radios=False)["blockers"]
    aps, invs = _aps()
    m = wificfg.manager(SSIDS, wificfg.site_radios(aps, invs), dict(POLICY, dfs=False), host_has_radios=True)
    assert "skip-dfs-channels=all" in m["script"] and "caps-man-addresses=127.0.0.1" in m["script"]
    for f in re.findall(r"band=5ghz-ax width=\S+ frequency=(\d+)", m["script"]):
        assert int(f) < 5250  # bez DFS jen 36–48
    m = wificfg.manager(SSIDS, wificfg.site_radios(aps, invs), dict(POLICY, max_width=20), host_has_radios=False)
    assert "width=20/40/80mhz" not in m["script"]


def test_fill_secrets():
    s = 'passphrase=MTCPSK0b9a3c1e000040008000000000000001 ft=yes'
    out = wificfg.fill_secrets(s, lambda h: 'tajne"heslo$1' if h.endswith("1") else None)
    assert out == 'passphrase="tajne\\"heslo\\$1" ft=yes'
    with pytest.raises(ValueError):
        wificfg.fill_secrets(s, lambda h: None)


def test_ssid_validation():
    v = intent.validate_ssid({"ssid": "Firma", "vlan_id": 10, "psk": "heslo123"}, {10}, True)
    assert v["bands"] == "2.4,5" and v["ft"] is True
    assert intent.validate_ssid({"ssid": "Firma", "vlan_id": 10}, {10}, False)["psk"] is None
    for bad in ({"ssid": "Firma", "vlan_id": 10}, {"ssid": "Firma", "vlan_id": 30, "psk": "heslo123"},
                {"ssid": "Kavárna", "vlan_id": 10, "psk": "heslo123"}, {"ssid": "x" * 33, "vlan_id": 10, "psk": "heslo123"},
                {"ssid": "Firma", "vlan_id": 10, "psk": "kratke"}, {"ssid": "Firma", "vlan_id": 10, "psk": "heslo123", "bands": "6"}):
        with pytest.raises(intent.IntentError):
            intent.validate_ssid(bad, {10}, True)
    assert intent.validate_wifi({})["country"] == "Czech Republic"
    with pytest.raises(intent.IntentError):
        intent.validate_wifi({"max_width": 160})


def test_ap_role_model():
    m = switchcfg.build(AP_INV, INTENT, {}, role="ap", cap_manager="SW-core")
    assert m["blockers"] == [] and m["role"] == "ap" and m["cap"] == {"manager": "SW-core"}
    body = switchcfg.config_body(m)
    assert 'wifi cap set enabled=yes discovery-interfaces=bridge caps-man-names="SW-core" lock-to-caps-man=yes' in body
    assert "configuration.manager=capsman disabled=no" in body and "slaves-static=yes" in body
    assert any("bez CAPsMAN" in b or "CAPsMAN" in b for b in switchcfg.build(AP_INV, INTENT, {}, role="ap")["blockers"])
    assert any("wireless" in b for b in switchcfg.build(dict(INV, wireless=[{"name": "wlan1"}]), INTENT, {}, role="ap",
                                                       cap_manager="X")["blockers"])
    assert any("nemá Wi-Fi" in b for b in switchcfg.build(INV, INTENT, {}, role="ap", cap_manager="X")["blockers"])
    assert "CAPsMAN" in switchcfg.classify(AP_INV, "ap")["removed"][-1]


def test_reconcile_cap():
    inv = _managed_inv()
    inv.update({k: AP_INV[k] for k in ("wifi_radios", "wifi", "wifi_cap")})
    r = switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, role="ap", cap_manager="SW-core"))
    assert r["changes"] == ["CAP → CAPsMAN SW-core"] and r["additive"] is False
    assert 'caps-man-names="SW-core"' in r["apply"] and "enabled=no" in r["revert"]
    inv["wifi_cap"] = {"enabled": "true", "discovery-interfaces": "bridge", "caps-man-names": "SW-core", "lock-to-caps-man": "true"}
    # rádia po resetu řídí „local“ → CAPsMAN by je nepřevzal
    assert switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, role="ap", cap_manager="SW-core"))["changes"] == [
        "CAP → CAPsMAN SW-core"]
    for w in inv["wifi"]:
        w["configuration.manager"] = "capsman"
    assert switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, role="ap", cap_manager="SW-core"))["changes"] == []
    r = switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, role="switch"))
    assert r["changes"] == ["CAP vypnout (role switch)"]
    for x in (r["apply"], r["revert"]):
        assert _balanced(x)
        x.encode("ascii")


# --- tok přes API a check-in -------------------------------------------------------------------------

def _wifi_env():
    from test_modules import _adopted, _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    host = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "SW-core")
    ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "AP-chodba")
    inventory.store(db, tid, host, copy.deepcopy(_managed_inv()), [])
    inventory.store(db, tid, ap, copy.deepcopy(AP_INV), [])
    return db, name, settings, box, app, tid, sid, host, ap, operator_scope


def test_wifi_flow():
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_switchcfg import INTENT_RAW

    db, name, settings, box, app, tid, sid, host, ap, operator_scope = _wifi_env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        S = base + f"/sites/{sid}"
        assert c.put(S + "/intent", json_body=INTENT_RAW).status_code == 200
        # AP bez CAPsMANu v záměru: role AP má překážku
        assert c.put(base + f"/devices/{ap}/intent", json_body={"role": "ap"}).status_code == 200
        pv = c.get(base + f"/devices/{ap}/takeover").json()
        assert pv["model"]["role"] == "ap" and any("CAPsMAN" in b for b in pv["blockers"]) and pv["wifi"] is True
        # CAPsMAN jen na řízeném zařízení
        r = c.put(S + "/intent", json_body=dict(INTENT_RAW, wifi={"capsman_device_id": host}))
        assert r.status_code == 400 and "řízeném" in r.json()["detail"]
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET managed_at = now(), role = 'switch' WHERE id = %(h)s", {"h": host})
        assert c.put(S + "/intent", json_body=dict(INTENT_RAW, wifi={"capsman_device_id": host})).status_code == 200
        # uložení sítí bez wifi nesmí CAPsMAN zapomenout
        assert c.put(S + "/intent", json_body=INTENT_RAW).json()["intent"]["wifi"]["capsman_device_id"] == host
        pv = c.get(base + f"/devices/{ap}/takeover").json()
        assert pv["blockers"] == [] and pv["model"]["cap"] == {"manager": "SW-sklad"} and pv["capsman"] == "SW-sklad", pv.get("blockers")
        # SSID: heslo povinné, jen šifrovaně, výpis bez hesla
        assert c.post(S + "/ssids", json_body={"ssid": "Firma", "vlan_id": 10}).status_code == 400
        r = c.post(S + "/ssids", json_body={"ssid": "Firma", "vlan_id": 10, "psk": "Tajne-heslo-123"})
        assert r.status_code == 201, r.text
        fid = r.json()["id"]
        assert c.post(S + "/ssids", json_body={"ssid": "Firma", "vlan_id": 20, "psk": "jine-heslo"}).status_code == 400
        assert c.post(S + "/ssids", json_body={"ssid": "Hoste", "vlan_id": 20, "psk": "hoste-2026", "isolation": True}).status_code == 201
        assert c.put(S + f"/ssids/{fid}", json_body={"ssid": "Firma", "vlan_id": 10, "ft": False}).status_code == 200
        v = c.get(S + "/intent").json()
        assert [x["ssid"] for x in v["ssids"]] == ["Firma", "Hoste"] and "Tajne-heslo" not in str(v) and all(x["has_psk"] for x in v["ssids"])
        assert next(x for x in v["ssids"] if x["ssid"] == "Firma")["ft"] is False
        with operator_scope(db) as tx:
            raw = tx.fetchone("SELECT psk_ciphertext FROM site_ssid WHERE id = %(i)s", {"i": fid})["psk_ciphertext"]
            audit = str(tx.fetchall("SELECT before, after FROM audit_log WHERE action LIKE 'site.ssid%%'"))
        assert b"Tajne-heslo-123" not in bytes(raw) and "Tajne-heslo-123" not in audit
        # síť používaná SSID nejde ze záměru odebrat
        less = dict(INTENT_RAW, networks=[n for n in INTENT_RAW["networks"] if n["vlan_id"] != 20])
        assert "Wi-Fi" in c.put(S + "/intent", json_body=less).json()["detail"]
        # AP převzaté jako AP + switch
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET managed_at = now(), role = 'ap' WHERE id = %(a)s", {"a": ap})
        w = c.get(S + "/wifi/preview").json()
        assert w["blockers"] == [] and len(w["radios"]) == 2 and "host_inv" not in w
        r = c.post(S + "/wifi/plan", json_body={})
        assert r.status_code == 201, r.text
        pid = r.json()["plan_id"]
        with operator_scope(db) as tx:
            script = tx.fetchone("SELECT script FROM config_plan WHERE id = %(i)s", {"i": pid})["script"]
        assert "Tajne-heslo-123" not in script and "MTCPSK" in script
        r = c.post(base + f"/plans/{pid}/approve", json_body={})
        assert r.status_code == 200, r.text
        # doručení na zařízení s CAPsMANem: hesla dosazená, bez tokenů a bez návratu
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert 'passphrase="Tajne-heslo-123" ft=no' in s and 'passphrase="hoste-2026" ft=yes' in s
        assert "MTCPSK" not in s and "mtc-revert" not in s
        cid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT status::text AS s FROM config_plan WHERE id = %(i)s", {"i": pid})["s"] == "applied"
        assert c.delete(S + f"/ssids/{fid}").status_code == 200
    finally:
        drop_db(name)


def test_ssid_isolated_between_tenants():
    """Heslo SSID jiného tenanta nejde dosadit (RLS) ani přesunout (kontext šifrování)."""
    from helpers import drop_db

    from mtcontroller import services, takeover
    from mtcontroller.db import tenant_scope

    db, name, settings, box, app, tid, sid, host, ap, operator_scope = _wifi_env()
    try:
        from test_switchcfg import INTENT_RAW

        takeover.set_site_intent(db, tid, sid, INTENT_RAW)
        out = takeover.save_ssid(db, box, tid, sid, {"ssid": "Firma", "vlan_id": 10, "psk": "Tajne-heslo-123"})
        hexid = out["id"].replace("-", "")
        other = services.create_tenant(db, settings, "Jina")
        with tenant_scope(db, other["id"]) as tx:
            assert takeover.ssid_psk(tx, box, hexid) is None
        with tenant_scope(db, tid) as tx:
            assert takeover.ssid_psk(tx, box, hexid) == "Tajne-heslo-123"
    finally:
        drop_db(name)


def test_cap_script_ascii():
    s = routeros.wrap_command("https://c/ack?id=1", "K", wificfg.cap_body("SW-core"), routeros.FetchOpts())
    assert _balanced(s)
    s.encode("ascii")


# --- skupiny AP, více hesel, VLAN rádií na AP podle MAC ------------------------------------------------

def _grouped():
    aps, invs = _aps()
    rad = wificfg.site_radios(aps, invs)
    for r in rad:
        # d1 = cAP ax ve skupině sklad, d2 = cAP ac bez skupiny
        r["group"], r["chip"] = ("g-sklad", "ax") if r["device_id"] == "d1" else (None, "ac")
    ssids = [dict(SSIDS[0], groups=[]),
             dict(SSIDS[1], groups=["g-sklad"]),
             dict(SSIDS[2], id="0b9a3c1e-0000-4000-8000-000000000004", ssid="Zamestnanci", groups=[],
                  ppsk=[{"name": "Ucetni", "vlan_id": 30}, {"name": "Vyroba", "vlan_id": 40}])]
    return rad, ssids


def test_groups_and_ppsk():
    rad, ssids = _grouped()
    m = wificfg.manager(ssids, rad, POLICY, host_has_radios=False)
    s = m["script"]
    s.encode("ascii")
    assert _balanced(s) and m["blockers"] == []
    rows = {(r["device_id"], r["band"]): r for r in m["radios"]}
    # Hoste jen ve skupině sklad; více hesel jen na ax, ac ho vynechá s poznámkou
    assert rows[("d1", "5")]["ssids"] == ["Firma", "Hoste"]
    assert rows[("d1", "2.4")]["ssids"] == ["Firma", "Hoste", "Zamestnanci"]
    assert rows[("d2", "2.4")]["ssids"] == ["Firma"] and rows[("d2", "2.4")]["skipped"] == ["Zamestnanci"]
    assert any("umí jen ax" in n for n in m["notes"])
    # multi-passphrase: jen WPA2, každé heslo token a svou VLAN; mazání před security
    assert 'multi-passphrase-group="mtc-mp3"' in s and "authentication-types=wpa2-psk passphrase=MTCPSK" in s
    assert 'passphrase=MTCMP0b9a3c1e000040008000000000000004X1 vlan-id=40' in s
    assert s.index("multi-passphrase remove") < s.index("security remove")
    filled = wificfg.fill_secrets(s, lambda h: "hlavni-heslo", lambda h, i: ["ucetni-heslo", "MTCPSK" + "0" * 32][i])
    assert '"ucetni-heslo" vlan-id=30' in filled and "MTCMP" not in filled
    # heslo, které vypadá jako token, se nepřepisuje znovu
    assert 'passphrase="MTCPSK' + "0" * 32 + '" vlan-id=40' in filled
    with pytest.raises(ValueError):
        wificfg.fill_secrets(s, lambda h: "hlavni-heslo")


def test_ssid_validation_groups_ppsk():
    base = {"ssid": "Firma", "vlan_id": 10, "psk": "heslo123", "groups": ["g-a"]}
    v = intent.validate_ssid(dict(base, ppsk=[{"name": "Ucetni", "vlan_id": 20, "psk": "ucetni-1"}]), {10, 20}, True, {"g-a"})
    assert v["groups"] == ["g-a"] and v["ppsk"] == [{"name": "Ucetni", "vlan_id": 20, "psk": "ucetni-1"}]
    assert intent.validate_ssid(base, {10}, True, {"g-a"})["ppsk"] is None
    for bad in (dict(base, groups=["g-x"]),
                dict(base, ppsk=[{"name": "A", "vlan_id": 20, "psk": "heslo123"}]),  # = hlavní heslo
                dict(base, ppsk=[{"name": "A", "vlan_id": 20, "psk": "ucetni-1"}, {"name": "a", "vlan_id": 20, "psk": "ucetni-2"}]),
                dict(base, ppsk=[{"name": "A", "vlan_id": 99, "psk": "ucetni-1"}])):
        with pytest.raises(intent.IntentError):
            intent.validate_ssid(bad, {10, 20}, True, {"g-a"})
    w = intent.validate_wifi({"groups": [{"name": "Sklad"}, {"id": "g-kancl", "name": "Kancelář"}]})
    assert w["groups"][1] == {"id": "g-kancl", "name": "Kancelář"} and re.fullmatch(r"g-[0-9a-f]+", w["groups"][0]["id"])
    with pytest.raises(intent.IntentError):
        intent.validate_wifi({"groups": [{"name": "A"}, {"name": "a"}]})


HOST_INV = {"wifi": [
    # rozhraní CAP na CAPsMANu: konfigurace → SSID, MAC jako na AP
    {"name": "cap-wifi1", "mac-address": "08:55:31:53:B9:2E", "configuration": "mtc-r-085531" "53b92e"},
    {"name": "cap-wifi3", "mac-address": "0a:55:31:53:b9:2e", "configuration": "mtc-s3"},
    {"name": "cap-wifi9", "mac-address": "AA:AA:AA:AA:AA:AA", "configuration": "mtc-s2"},
]}
CAP_AC = {"wifi": [{"name": "wifi1", "mac-address": "08:55:31:53:B9:2E"},
                   {"name": "wifi4", "mac-address": "0A:55:31:53:B9:2E", "master-interface": "wifi1"}],
          "scripts": [{"name": "mtc-wifi-vlan", "comment": "mtc-wifi:abc"}]}


def test_cap_table_by_mac():
    _, ssids = _grouped()
    by_id = {s["id"]: s for s in ssids}
    cmap = {"mtc-r-08553153b92e": ssids[0]["id"], "mtc-s2": ssids[1]["id"], "mtc-s3": ssids[2]["id"]}
    t = wificfg.cap_table(HOST_INV, CAP_AC, cmap, by_id)
    # jméno slave rozhraní (wifi3/wifi4) nehraje roli, jen MAC; cizí AP se ignoruje
    assert t == [{"mac": "08:55:31:53:B9:2E", "ssid": "Firma", "pvid": 10, "tagged": []},
                 {"mac": "0A:55:31:53:B9:2E", "ssid": "Zamestnanci", "pvid": 10, "tagged": [30, 40]}]
    src = wificfg.cap_vlan_source(t)
    assert '"0A:55:31:53:B9:2E"="10|30,40"' in src and 'comment="mtc-wifi"' in src
    assert wificfg.installed_table_sha(CAP_AC) == "abc" and wificfg.installed_table_sha({}) is None
    body = wificfg.cap_vlan_body(t)
    assert f'comment="mtc-wifi:{wificfg.table_sha(t)}"' in body and "interval=20s" in body
    with pytest.raises(ValueError):
        wificfg.cap_vlan_source([{"mac": "x; /system reset", "pvid": 1, "tagged": []}])


def test_groups_ppsk_flow():
    """Skupiny AP, více hesel (šifrovaně, odhalení s auditem), plán CAPsMAN s hesly, tabulka VLAN na AP."""
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_switchcfg import INTENT_RAW

    from mtcontroller import inventory, takeover

    db, name, settings, box, app, tid, sid, host, ap, operator_scope = _wifi_env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        S = base + f"/sites/{sid}"
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET managed_at = now(), role = 'switch' WHERE id = %(h)s", {"h": host})
            tx.execute("UPDATE device SET managed_at = now(), role = 'ap' WHERE id = %(a)s", {"a": ap})
        wifi = {"capsman_device_id": host, "groups": [{"id": "g-sklad", "name": "Sklad"}]}
        assert c.put(S + "/intent", json_body=dict(INTENT_RAW, wifi=wifi)).status_code == 200
        ax = copy.deepcopy(AP_INV)
        ax["resource"] = dict(ax.get("resource") or {}, **{"board-name": "cAP ax", "architecture-name": "arm64"})
        ax["wifi"] = [{"name": "wifi1", "default-name": "wifi1", "mac-address": "48:A9:8A:00:00:01"},
                      {"name": "wifi7", "mac-address": "4A:A9:8A:00:00:01", "master-interface": "wifi1"}]
        inventory.store(db, tid, ap, ax, [])
        r = c.post(S + "/ssids", json_body={"ssid": "Firma", "vlan_id": 10, "psk": "Tajne-heslo-123", "groups": ["g-x"]})
        assert r.status_code == 400
        r = c.post(S + "/ssids", json_body={"ssid": "Firma", "vlan_id": 10, "psk": "Tajne-heslo-123", "groups": ["g-sklad"],
                                            "ppsk": [{"name": "Hoste", "vlan_id": 20, "psk": "hoste-heslo-1"}]})
        assert r.status_code == 201, r.text
        fid = r.json()["id"]
        lst = c.get(S + "/ssids").json()["ssids"]
        assert lst[0]["ppsk"] == [{"name": "Hoste", "vlan_id": 20}] and lst[0]["groups"] == ["g-sklad"]
        assert "hoste-heslo-1" not in str(lst)
        # úprava bez hesla položky ponechá uložené; nová položka heslo potřebuje
        r = c.put(S + f"/ssids/{fid}", json_body={"ssid": "Firma", "vlan_id": 10, "groups": ["g-sklad"],
                                                  "ppsk": [{"name": "hoste", "vlan_id": 20}, {"name": "Novy", "vlan_id": 20}]})
        assert r.status_code == 400 and "Novy" in r.json()["detail"]
        r = c.put(S + f"/ssids/{fid}", json_body={"ssid": "Firma", "vlan_id": 10, "groups": ["g-sklad"],
                                                  "ppsk": [{"name": "Hoste", "vlan_id": 20}]})
        assert r.status_code == 200, r.text
        # stejné jako hlavní heslo nejde ani u ponechaného hlavního hesla
        r = c.put(S + f"/ssids/{fid}", json_body={"ssid": "Firma", "vlan_id": 10,
                                                  "ppsk": [{"name": "X", "vlan_id": 20, "psk": "Tajne-heslo-123"}]})
        assert r.status_code == 400
        rv = c.post(S + f"/ssids/{fid}/ppsk/reveal", json_body={}).json()["ppsk"]
        assert rv == [{"name": "Hoste", "vlan_id": 20, "psk": "hoste-heslo-1"}]
        with operator_scope(db) as tx:
            row = tx.fetchone("SELECT ppsk_ciphertext FROM site_ssid WHERE id = %(i)s", {"i": fid})
            aud = str(tx.fetchall("SELECT action, after FROM audit_log WHERE action LIKE 'site.ssid%%'"))
        assert b"hoste-heslo-1" not in bytes(row["ppsk_ciphertext"]) and "hoste-heslo-1" not in aud
        assert "site.ssid_ppsk_reveal" in aud
        # VLAN použitá heslem nejde ze záměru odebrat
        less = dict(INTENT_RAW, wifi=wifi, networks=[n for n in INTENT_RAW["networks"] if n["vlan_id"] != 20])
        assert c.put(S + "/intent", json_body=less).status_code == 400
        # AP bez skupiny: SSID skupiny Sklad se na něm nevysílá
        w = c.get(S + "/wifi/preview").json()
        assert all(r["ssids"] == [] for r in w["radios"]) or not w["radios"]
        assert c.post(S + "/ap-groups", json_body={"devices": [ap], "group": "g-nic"}).status_code == 400
        assert c.post(S + "/ap-groups", json_body={"devices": [ap], "group": "g-sklad"}).json()["moved"] == 1
        w = c.get(S + "/wifi/preview").json()
        assert w["blockers"] == [] and w["groups"] == wifi["groups"]
        r5 = next(r for r in w["radios"] if r["band"] == "5")
        assert r5["group"] == "g-sklad" and r5["chip"] == "ax" and r5["ssids"] == ["Firma"]
        pid = c.post(S + "/wifi/plan", json_body={}).json()["plan_id"]
        assert c.post(base + f"/plans/{pid}/approve", json_body={}).status_code == 200
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert '"hoste-heslo-1" vlan-id=20' in s and "MTCMP" not in s
        cid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        # CAPsMAN vidí rozhraní AP (MAC) a jeho konfiguraci → tabulka VLAN na AP
        with operator_scope(db) as tx:
            cmap = tx.fetchone("SELECT changes FROM config_plan WHERE id = %(i)s", {"i": pid})["changes"]["config_map"]
        cfg = next(k for k, v in cmap.items() if k.startswith("mtc-r-") and v == fid)
        hinv = copy.deepcopy(_managed_inv())
        hinv["wifi"] = [{"name": "cap-wifi1", "mac-address": "48:A9:8A:00:00:01", "configuration": cfg}]
        inventory.store(db, tid, host, hinv, [])
        assert takeover.sync_wifi_tables(db, settings, tid, sid) == 1
        assert takeover.sync_wifi_tables(db, settings, tid, sid) == 0  # už čeká ve frontě
        with operator_scope(db) as tx:
            p = tx.fetchone("""SELECT status::text AS st, script, changes FROM config_plan WHERE device_id = %(a)s
                               AND module = 'wifitable'""", {"a": ap})
        assert p["st"] == "queued" and p["changes"]["table"] == [
            {"mac": "48:A9:8A:00:00:01", "ssid": "Firma", "pvid": 10, "tagged": [20]}]
        assert '"48:A9:8A:00:00:01"="10|20"' in p["script"].replace('\\"', '"')
        # doručení na AP; po úspěchu se stejná tabulka znovu neplánuje, dokud ji inventura neuvidí
        s = _device_post(app, "/api/checkin", "key-AP1", {"sstp": "up"}).text
        assert "mtc-wifi-vlan" in s
        cid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-AP1")
        assert takeover.sync_wifi_tables(db, settings, tid, sid) == 0
        # zrušení skupiny: AP i SSID se vrátí na „všechna AP“
        assert c.put(S + "/intent", json_body=dict(INTENT_RAW, wifi={"capsman_device_id": host})).status_code == 200
        lst = c.get(S + "/ssids").json()["ssids"]
        assert lst[0]["groups"] == []
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT ap_group FROM device_intent WHERE device_id = %(a)s", {"a": ap})["ap_group"] is None
            acts = {r["action"] for r in tx.fetchall("SELECT action FROM audit_log")}
        assert {"device.ap_group", "site.ssid", "plan.create"} <= acts
    finally:
        drop_db(name)
