"""Adopce na jeden krok: řetězec po schválení až do řízeného stavu."""

import copy

from test_switchcfg import INTENT_RAW, INV, _managed_inv, _takeover_env

from mtcontroller import adoptflow, inventory
from mtcontroller.db import operator_scope, tenant_scope


def _flow(db, did):
    with operator_scope(db) as tx:
        return tx.fetchone("SELECT adopt_flow, managed_at FROM device WHERE id = %(d)s", {"d": did})


def _start(db, tid, sw, target="switch"):
    with tenant_scope(db, tid) as tx:
        adoptflow.start(tx, tid, sw, target, None, None, True)


def test_simple_switch_goes_to_takeover_plan():
    from helpers import drop_db
    from test_flow import _login

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        c = _login(app)
        _start(db, tid, sw)
        assert _flow(db, sw)["adopt_flow"]["step"] == "provision"
        assert adoptflow.step(db, settings, box, tid, sw) == "inventory"
        # stará inventura (před schválením) nestačí – čeká na novou
        assert adoptflow.step(db, settings, box, tid, sw) is None
        inventory.store(db, tid, sw, copy.deepcopy(INV), [])
        # lokalita bez sítí: čeká s vysvětlením
        assert adoptflow.step(db, settings, box, tid, sw) == "takeover"
        assert "nemá sítě" in _flow(db, sw)["adopt_flow"]["msg"]
        assert c.put(f"/api/admin/tenants/{tid}/sites/{sid}/intent", json_body=INTENT_RAW).status_code == 200
        # simple → převzetí načisto bez ptaní: plán schválený a ve frontě
        assert adoptflow.run_all(db, settings, box) == 1
        f = _flow(db, sw)["adopt_flow"]
        assert f["step"] == "takeover_wait" and f["plan_id"], f
        with operator_scope(db) as tx:
            p = tx.fetchone("SELECT module, status::text AS s FROM config_plan WHERE id = %(i)s", {"i": f["plan_id"]})
        assert p == {"module": "takeover", "s": "queued"}
        # převzetí potvrzené (zařízení řízené) → Wi-Fi → hotovo
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET managed_at = now() WHERE id = %(d)s", {"d": sw})
        assert adoptflow.step(db, settings, box, tid, sw) == "wifi"
        assert adoptflow.step(db, settings, box, tid, sw) == "done"
        assert adoptflow.step(db, settings, box, tid, sw) is None
        devs = c.get(f"/api/admin/tenants/{tid}/devices").json()
        assert devs[0]["adopt_flow"]["step"] == "done"
        with operator_scope(db) as tx:
            steps = [r["after"]["step"] for r in tx.fetchall(
                "SELECT after FROM audit_log WHERE action = 'device.adopt_flow' ORDER BY id")]
        assert steps == ["inventory", "takeover", "takeover_wait", "wifi", "done"], steps
    finally:
        drop_db(name)


def test_custom_needs_confirm_and_conforming_without_reset():
    from helpers import drop_db
    from test_flow import _login

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        assert c.put(base + f"/sites/{sid}/intent", json_body=INTENT_RAW).status_code == 200
        _start(db, tid, sw)
        adoptflow.step(db, settings, box, tid, sw)
        inv = copy.deepcopy(INV)
        inv["fw_filter"] = [{"chain": "input", "action": "drop", "comment": "moje pravidlo"}]
        inventory.store(db, tid, sw, inv, [])
        assert adoptflow.step(db, settings, box, tid, sw) == "needs_confirm"
        f = _flow(db, sw)["adopt_flow"]
        assert "vlastní konfiguraci" in f["msg"] and f["removed"]
        assert adoptflow.run_all(db, settings, box) == 0  # čeká na uživatele
        # „ponechat ke sledování“ řetězec ukončí; „přepsat“ pokračuje převzetím
        r = c.post(base + f"/devices/{sw}/adopt", json_body={"action": "overwrite"})
        assert r.status_code == 200, r.text
        assert r.json()["adopt_flow"]["step"] == "takeover_wait"
        r = c.post(base + f"/devices/{sw}/adopt", json_body={"action": "monitor"})
        assert r.json()["adopt_flow"] is None
        # conforming (VLAN na jednom bridgi): řízené hned, bez resetu, rozdíl doladí sladění
        with operator_scope(db) as tx:
            tx.execute("UPDATE config_plan SET status = 'discarded' WHERE device_id = %(d)s", {"d": sw})
        _start(db, tid, sw)
        adoptflow.step(db, settings, box, tid, sw)
        inventory.store(db, tid, sw, _managed_inv(), [])
        assert adoptflow.step(db, settings, box, tid, sw) == "wifi"
        assert _flow(db, sw)["managed_at"] is not None
        with operator_scope(db) as tx:
            assert not tx.fetchone("SELECT 1 FROM config_plan WHERE device_id = %(d)s AND module = 'takeover' "
                                   "AND status = 'queued'", {"d": sw})
    finally:
        drop_db(name)


def test_approve_with_mode_starts_flow():
    from helpers import drop_db
    from test_flow import _login

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        c = _login(app)
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'pending', site_id = NULL WHERE id = %(d)s", {"d": sw})
        base = f"/api/admin/tenants/{tid}"
        r = c.post(base + f"/devices/{sw}/approve", json_body={"site_id": sid, "mode": "ap", "ap_group": "g-x"})
        assert r.status_code == 400 and "skupina" in r.json()["detail"]
        r = c.post(base + f"/devices/{sw}/approve", json_body={"site_id": sid, "mode": "switch", "identity": "SW-nove"})
        assert r.status_code == 200, r.text
        d = c.get(base + "/devices").json()[0]
        assert d["role"] == "switch" and d["adopt_flow"]["step"] == "provision" and d["adopt_flow"]["target"] == "switch"
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT role FROM device_intent WHERE device_id = %(d)s", {"d": sw})["role"] == "switch"
    finally:
        drop_db(name)


def test_monitored_device_can_be_taken_into_management():
    from helpers import drop_db
    from test_flow import _login

    db, name, settings, box, app, tid, sid, sw = _takeover_env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        assert c.post(base + f"/devices/{sw}/adopt", json_body={"action": "manage", "target": "x"}).status_code == 400
        r = c.post(base + f"/devices/{sw}/adopt", json_body={"action": "manage", "target": "switch"})
        assert r.status_code == 200 and r.json()["adopt_flow"]["step"] == "inventory", r.text
        assert c.post(base + f"/devices/{sw}/adopt", json_body={"action": "retry"}).json()["adopt_flow"]["step"] == "inventory"
        with operator_scope(db) as tx:
            acts = {x["action"] for x in tx.fetchall("SELECT action FROM audit_log")}
        assert {"device.manage_start", "device.adopt_flow", "device.adopt_decide"} <= acts
    finally:
        drop_db(name)
