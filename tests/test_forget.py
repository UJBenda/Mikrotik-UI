"""Zapomenout zařízení: odebrání jen v controlleru, když zařízení úklid neprovede."""

from helpers import drop_db
from test_flow import _device_post, _login
from test_modules import _adopted, _env

from mtcontroller import auth, services
from mtcontroller.db import operator_scope, tenant_scope


def test_forget_and_readopt():
    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}"
        # Odebrat jen zařadí příkaz – zařízení neodpovídá, zůstává spravované
        assert c.post(base + "/commands", json_body={"kind": "deprovision"}).status_code == 201
        with tenant_scope(db, tid) as tx:
            services.enqueue(tx, tid, sw, "reboot", {})
            tx.execute("""INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, basis_sha)
                          VALUES (%(t)s, %(d)s, 'ntp', 'x', '{}', '/x', repeat('a', 64))""", {"t": tid, "d": sw})
        r = c.post(base + "/forget", json_body={})
        assert r.status_code == 200, r.text
        assert r.json() == {"serial": "SW1", "previous_state": "adopted"}
        with operator_scope(db) as tx:
            d = tx.fetchone("SELECT state, checkin_key_hash, tunnel_ip FROM device WHERE id = %(d)s", {"d": sw})
            assert d == {"state": "removed", "checkin_key_hash": None, "tunnel_ip": None}
            assert tx.fetchone("SELECT count(*) AS n FROM device_secret WHERE device_id = %(d)s", {"d": sw})["n"] == 0
            assert {r["status"] for r in tx.fetchall("SELECT status::text AS status FROM device_command WHERE device_id = %(d)s",
                                                      {"d": sw})} == {"cancelled"}
            assert tx.fetchone("SELECT status::text AS s FROM config_plan WHERE device_id = %(d)s", {"d": sw})["s"] == "discarded"
            assert tx.fetchone("SELECT 1 FROM audit_log WHERE action = 'device.forgotten'")
        # starý klíč už neplatí, druhé zapomenutí nejde
        assert _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).status_code == 401
        assert c.post(base + "/forget", json_body={}).status_code == 400
        # znovu adoptovat jde
        with operator_scope(db) as tx:
            uid = tx.fetchone("SELECT id FROM app_user LIMIT 1")["id"]
        tok = services.create_adoption_token(db, settings, tid, sid, uid)
        reg = services.register_device(db, box, tok.token, {"serial": "SW1", "model": "CRS326", "version": "7.20.1"})
        assert str(reg.device_id) == sw
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT state FROM device WHERE id = %(d)s", {"d": sw})["state"] == "pending"
    finally:
        drop_db(name)


def test_forget_needs_admin():
    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        with operator_scope(db) as tx:
            uid = auth.create_user(tx, "tech@example.cz", "Technik-heslo-123456")
            tx.execute("INSERT INTO role_binding (user_id, tenant_id, role) VALUES (%(u)s, %(t)s, 'technician')",
                       {"u": uid, "t": tid})
        c = _login(app, "tech@example.cz", "Technik-heslo-123456")
        assert c.post(f"/api/admin/tenants/{tid}/devices/{sw}/forget", json_body={}).status_code == 404
    finally:
        drop_db(name)
