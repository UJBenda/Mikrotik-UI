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
    assert 'name="mtc-dp2" bridge=bridge vlan-id=20 client-isolation=yes' in s
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
    many = [dict(SSIDS[0], id=f"0b9a3c1e-0000-4000-8000-00000000001{i}", ssid=f"S{i}") for i in range(5)]
    assert any("nejvýš 4" in b for b in wificfg.manager(many, [], POLICY, host_has_radios=False)["blockers"])
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
