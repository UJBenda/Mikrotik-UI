"""Okamžité doručení příkazu (kick přes REST v tunelu) a stav průběhu změny pro UI."""

import threading

from helpers import drop_db
from test_flow import _device_post, _login
from test_modules import _adopted, _env

from mtcontroller import services
from mtcontroller.db import tenant_scope


class FakeApi:
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    def request(self, method, path, body=None, params=None):
        FakeApi.calls.append((method, path, body))
        return {"ret": "*1"}

    def close(self):
        pass


def _kick_sync(db, settings, box, tid, did):
    """kick() běží ve vlákně – v testu počkáme na dokončení."""
    before = set(threading.enumerate())
    services.kick(db, settings, box, tid, did)
    for t in set(threading.enumerate()) - before:
        t.join(5)


def test_kick():
    db, name, settings, box, app, tid, sid = _env()
    orig = services.device_api
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        services.KICK_ENABLED = True
        services.device_api = lambda tx, box, d, timeout=10.0: FakeApi()
        FakeApi.calls = []
        # nic ve frontě → nic se neposílá
        _kick_sync(db, settings, box, tid, sw)
        assert FakeApi.calls == []
        with tenant_scope(db, tid) as tx:
            services.enqueue(tx, tid, sw, "reboot", {})
            assert services.kick_needed(tx, sw)
        _kick_sync(db, settings, box, tid, sw)
        (m, path, body), = FakeApi.calls
        assert m == "POST" and path == "/execute"
        src = body["script"]
        assert 'dst-path="mtc-kick.rsc"' in src and "mtc-cmd.rsc" not in src and "Bearer key-SW1" in src
        src.encode("ascii")
        # provision mění heslo vlastního účtu – jen schedulerem
        with tenant_scope(db, tid) as tx:
            tx.execute("UPDATE device_command SET status = 'done' WHERE device_id = %(d)s", {"d": sw})
            services.enqueue(tx, tid, sw, "provision", {})
            assert not services.kick_needed(tx, sw)
        # offline zařízení se nešťouchá
        FakeApi.calls = []
        with tenant_scope(db, tid) as tx:
            tx.execute("UPDATE device_command SET status = 'done' WHERE device_id = %(d)s", {"d": sw})
            services.enqueue(tx, tid, sw, "reboot", {})
            tx.execute("UPDATE device SET state = 'offline' WHERE id = %(d)s", {"d": sw})
        _kick_sync(db, settings, box, tid, sw)
        assert FakeApi.calls == []
    finally:
        services.KICK_ENABLED = False
        services.device_api = orig
        drop_db(name)


def test_activity_and_confirm_kick():
    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}"
        a = c.get(base + "/activity").json()
        assert a["busy"] is False and a["plans"] == [] and a["commands"] == []
        r = c.post(base + "/commands", json_body={"kind": "reboot"})
        assert r.status_code == 201
        a = c.get(base + "/activity").json()
        assert a["busy"] is True and a["commands"][0]["status"] == "queued"
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert c.get(base + "/activity").json()["commands"][0]["status"] == "sent"
        import re

        cid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        a = c.get(base + "/activity").json()
        assert a["busy"] is False and a["commands"][0]["status"] == "done"
        # potvrzení změny s návratem čeká na check-in → kick ho má doručit
        with tenant_scope(db, tid) as tx:
            pid = tx.fetchone(
                """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                            confirm_minutes, basis_sha, status)
                   VALUES (%(t)s, %(d)s, 'ports', 'x', '{}', '/x', '/y', 5, repeat('a', 64), 'confirming') RETURNING id""",
                {"t": tid, "d": sw})["id"]
            assert services.kick_needed(tx, sw)
            tx.execute("UPDATE config_plan SET module = 'takeover' WHERE id = %(i)s", {"i": pid})
            assert not services.kick_needed(tx, sw)  # převzetí se potvrzuje až po resetu
    finally:
        drop_db(name)
