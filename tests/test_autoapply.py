"""Uložit = nasadit: změny záměru a portů řízeného switche jdou rovnou na zařízení (s návratem)."""

import copy

from test_switchcfg import INTENT, INTENT_RAW, INV, _managed_inv

from mtcontroller import autoapply, intent, switchcfg


def test_override_for_profiles():
    st = {"pvid": 10, "tagged": [], "disabled": False}
    vids = [10, 20, 99]
    assert autoapply.override_for({"pvid": 10, "tagged": [], "disabled": False}, st, vids) is None
    assert autoapply.override_for({"pvid": 20, "tagged": []}, st, vids) == {"mode": "access", "vlan": 20}
    # všechny sítě lokality tagovaně (vestavěný trunk) → trunk podle záměru
    assert autoapply.override_for({"pvid": 10, "tagged": ["20", "99"]}, st, vids) == {"mode": "trunk"}
    assert autoapply.override_for({"pvid": 10, "tagged": ["20"]}, st, vids) == {"mode": "hybrid", "vlan": 10, "tagged": [20]}
    assert autoapply.override_for({"disabled": True}, st, vids) == {"mode": "off"}
    assert autoapply.split({"pvid": 1, "poe": "off", "comment": "x"}) == ({"pvid": 1}, {"poe": "off", "comment": "x"})


def test_hybrid_port_in_model():
    di = intent.validate_device({"ports": {"ether2": {"mode": "hybrid", "vlan": 10, "tagged": [20, 10]}}}, INTENT, None)
    assert di["ports"]["ether2"] == {"mode": "hybrid", "vlan": 10, "tagged": [10, 20]}
    m = switchcfg.build(INV, INTENT, di, model_name="CRS326")
    p2 = next(p for p in m["ports"] if p["port"] == "ether2")
    assert p2["pvid"] == 10 and p2["tagged"] == [20] and p2["frame_types"] == switchcfg.FT_ALL
    v = {x["vid"]: x for x in m["vlans"]}
    assert "ether2" in v[10]["untagged"] and "ether2" in v[20]["tagged"] and "ether2" not in v[99]["tagged"]
    r = switchcfg.reconcile(_managed_inv(), m)
    assert "pvid" not in r["apply"] or "frame-types=admit-all" in r["apply"]
    assert any(c.startswith("VLAN 20") for c in r["changes"])
    try:
        intent.validate_device({"ports": {"ether2": {"mode": "hybrid", "vlan": 10, "tagged": [30]}}}, INTENT, None)
        raise AssertionError("neznámá VLAN prošla")
    except intent.IntentError:
        pass


def _managed_env():
    from test_modules import _adopted, _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "SW-sklad")
    inventory.store(db, tid, sw, copy.deepcopy(_managed_inv()), [])
    with operator_scope(db) as tx:
        tx.execute("UPDATE device SET managed_at = now(), role = 'switch', model = 'CRS326' WHERE id = %(d)s", {"d": sw})
    return db, name, settings, box, app, tid, sid, sw, operator_scope


def test_save_applies_and_port_edit_goes_to_intent():
    from helpers import drop_db
    from test_flow import _login

    db, name, settings, box, app, tid, sid, sw, operator_scope = _managed_env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        r = c.put(base + f"/sites/{sid}/intent", json_body=INTENT_RAW)
        assert r.status_code == 200 and r.json()["apply"]["devices"][0]["result"] == "ok", r.text
        # nová síť → sladění se rovnou nasadí (bez ručního schválení)
        more = dict(INTENT_RAW, networks=INTENT_RAW["networks"] + [{"name": "Kamery", "vlan_id": 30, "purpose": "kamery"}])
        ap = c.put(base + f"/sites/{sid}/intent", json_body=more).json()["apply"]
        d0 = ap["devices"][0]
        assert d0["result"] == "applying" and d0["command_id"] and "VLAN 30" in d0["summary"], ap
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT status::text AS s FROM config_plan WHERE id = %(i)s", {"i": d0["plan_id"]})["s"] == "queued"
        stt = c.get(base + f"/plans/status?ids={d0['plan_id']},abc").json()
        assert stt == [{"id": d0["plan_id"], "status": "queued", "device": "SW-sklad"}], stt
        assert c.get(base + "/plans/status").json() == []
        # port řízeného switche: VLAN do záměru zařízení, PoE/popis plánem portu
        r = c.post(base + f"/devices/{sw}/ports/ether2/plan",
                   json_body={"desired": {"pvid": 20, "tagged": [], "disabled": False, "comment": "tiskarna"}, "apply": True})
        assert r.status_code == 201, r.text
        out = r.json()
        assert out["managed"] and out["apply"]["devices"][0]["result"] == "applying" and out["command_id"], out
        with operator_scope(db) as tx:
            di = tx.fetchone("SELECT ports FROM device_intent WHERE device_id = %(d)s", {"d": sw})["ports"]
            acts = [x["action"] for x in tx.fetchall("SELECT action FROM audit_log ORDER BY id")]
        assert di["ether2"] == {"mode": "access", "vlan": 20}
        assert "device.intent" in acts and acts.count("plan.approve") >= 3
        # uplink nesmí být přístupový port – nic se neuloží
        r = c.post(base + f"/devices/{sw}/ports/ether1/plan", json_body={"desired": {"pvid": 10, "tagged": []}, "apply": True})
        assert r.status_code == 400 and "uplink" in r.json()["detail"]
        # LACP a „Připravit VLAN“ na řízeném zařízení ne (porty určuje záměr)
        r = c.post(base + f"/devices/{sw}/bonds", json_body={"kind": "lacp", "ports": ["ether2", "ether4"]})
        assert r.status_code == 400 and "řízené" in r.json()["detail"]
        assert c.get(base + f"/devices/{sw}").json()["site_vlans"] is None
        # čtyři oči: modul v seznamu → plán zůstane ke schválení jiným správcem
        settings.security.four_eyes_modules = ["switch"]
        ap = c.put(base + f"/sites/{sid}/intent", json_body=INTENT_RAW).json()["apply"]
        assert ap["devices"][0]["result"] == "draft" and "jiný uživatel" in ap["devices"][0]["reason"], ap
    finally:
        drop_db(name)
