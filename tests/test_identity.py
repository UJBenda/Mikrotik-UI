"""Jméno zařízení (/system identity) z controlleru: při schválení i později, CAPsMAN bez výpadku Wi-Fi."""

import re

from test_flow import _device_post, _drain_until, _login, _setup

from helpers import drop_db
from mtcontroller.db import operator_scope


def _ack(app, key, text):
    cid = int(re.search(r"ack\\\?id=(\d+)", text).group(1))
    _device_post(app, f"/api/checkin/ack?id={cid}&status=done", key)


def test_identity_at_approval_and_rename():
    db, name, settings, box, providers, app = _setup()
    try:
        c = _login(app)
        tid = c.post("/api/admin/tenants", json_body={"name": "A"}).json()["id"]
        sid = c.post(f"/api/admin/tenants/{tid}/sites", json_body={"name": "P"}).json()["id"]
        tok = c.post(f"/api/admin/tenants/{tid}/adoption-tokens", json_body={"site_id": sid}).json()["token"]
        r = _device_post(app, f"/adopt/{tok}/register", body={"serial": "ID1234", "model": "x", "version": "7.20", "mac": ""})
        key = re.search(r"Bearer ([A-Za-z0-9_-]+)", r.text).group(1)
        did = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]["id"]
        url = f"/api/admin/tenants/{tid}/devices/{did}"
        # neplatné jméno (diakritika, mezera) se odmítne už při schválení
        assert c.post(url + "/approve", json_body={"site_id": sid, "identity": "Sklad přízemí"}).status_code == 400
        assert c.post(url + "/approve", json_body={"site_id": sid, "identity": "SW-sklad"}).status_code == 200
        with operator_scope(db) as tx:
            kinds = [r["kind"] for r in tx.fetchall("SELECT kind FROM device_command WHERE device_id = %(d)s ORDER BY id", {"d": did})]
            tx.execute("UPDATE device SET state = 'adopted', adopted_at = now()")
            tx.execute("UPDATE device_command SET status = 'done' WHERE kind = 'provision'")
        assert kinds == ["provision", "set_identity"]
        row = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert row["identity_pending"] == "SW-sklad"
        s = _device_post(app, "/api/checkin", key, {"sstp": "up"}).text
        assert '/system identity set name="SW-sklad"' in s
        s.encode("ascii")
        _ack(app, key, s)
        row = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert row["identity"] == "SW-sklad" and row["identity_pending"] is None
        # přejmenování z UI; stejné jméno nic nepošle; audit
        assert c.post(url + "/identity", json_body={"name": "SW-sklad"}).json()["unchanged"] is True
        assert c.post(url + "/identity", json_body={"name": "x;/system reset"}).status_code == 400
        r = c.post(url + "/identity", json_body={"name": "SW-dilna"})
        assert r.status_code == 200 and r.json()["aps"] == []
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT 1 FROM audit_log WHERE action = 'device.identity'")
    finally:
        drop_db(name)


def test_rename_capsman_host_keeps_caps():
    """AP drží CAPsMAN podle jména: nejdřív dostanou staré i nové, po přejmenování jen nové."""
    from test_switchcfg import INTENT_RAW
    from test_wificfg import _wifi_env

    db, name, settings, box, app, tid, sid, host, ap, operator_scope = _wifi_env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET managed_at = now(), role = 'switch' WHERE id = %(h)s", {"h": host})
            tx.execute("UPDATE device SET managed_at = now(), role = 'ap' WHERE id = %(a)s", {"a": ap})
        assert c.put(base + f"/sites/{sid}/intent", json_body=dict(INTENT_RAW, wifi={"capsman_device_id": host})).status_code == 200
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET identity = 'AP-chodba' WHERE id = %(a)s", {"a": ap})
            old = tx.fetchone("SELECT identity FROM device WHERE id = %(h)s", {"h": host})["identity"]
        # jméno jiného zařízení lokality nejde (bez ohledu na velikost písmen)
        assert c.post(base + f"/devices/{host}/identity", json_body={"name": "ap-chodba"}).status_code == 400
        r = c.post(base + f"/devices/{host}/identity", json_body={"name": "SW-hlavni"})
        assert r.status_code == 200 and r.json()["aps"] == [ap]
        s = _drain_until(app, "key-AP1", f'caps-man-names="{old},SW-hlavni"')
        _ack(app, "key-AP1", s)
        s = _drain_until(app, "key-SW1", '/system identity set name="SW-hlavni"')
        _ack(app, "key-SW1", s)
        s = _drain_until(app, "key-AP1", 'caps-man-names="SW-hlavni"')
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT identity FROM device WHERE id = %(h)s", {"h": host})["identity"] == "SW-hlavni"
    finally:
        drop_db(name)
