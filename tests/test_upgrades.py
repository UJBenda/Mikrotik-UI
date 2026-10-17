"""Postupná aktualizace lokality: pořadí, čekání na návrat, zastavení při chybě."""

import datetime as dt

import pytest
from helpers import drop_db
from test_flow import _login
from test_modules import _adopted, _env

from mtcontroller import inventory, services, upgrades
from mtcontroller.db import operator_scope, tenant_scope


def _dev(db, box, tid, sid, serial, ip, ident, role, latest="7.20.5"):
    did = _adopted(db, box, tid, sid, serial, ip, ident)
    with tenant_scope(db, tid) as tx:
        tx.execute("UPDATE device SET role = %(r)s WHERE id = %(d)s", {"r": role, "d": did})
    inventory.store(db, tid, did, {"update": {"channel": "stable", "latest-version": latest}}, [])
    return did


def _cmd(db, did):
    with operator_scope(db) as tx:
        return tx.fetchone("""SELECT id, status::text AS status, params FROM device_command
                              WHERE device_id = %(d)s AND kind = 'upgrade' ORDER BY id DESC LIMIT 1""", {"d": did})


def _finish_cmd(db, did, status="done", version="7.20.5 (stable)"):
    with operator_scope(db) as tx:
        tx.execute("""UPDATE device_command SET status = %(s)s, finished_at = now()
                      WHERE device_id = %(d)s AND kind = 'upgrade' AND status IN ('queued', 'sent')""",
                   {"s": status, "d": did})
        if status == "done":
            tx.execute("UPDATE device SET ros_version = %(v)s, last_checkin_at = now() WHERE id = %(d)s",
                       {"v": version, "d": did})


def _job(db, jid):
    with operator_scope(db) as tx:
        return tx.fetchone("SELECT status::text AS status, steps, message FROM upgrade_job WHERE id = %(j)s", {"j": jid})


def test_rolling_upgrade_order_wait_and_stop():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _dev(db, box, tid, sid, "GW1", "10.200.1.1", "gw", "gateway")
        sw = _dev(db, box, tid, sid, "SW1", "10.200.1.2", "sw", "switch")
        ap = _dev(db, box, tid, sid, "AP1", "10.200.1.3", "ap", "ap")
        _dev(db, box, tid, sid, "AP2", "10.200.1.4", "ap-bad", "ap", latest="7.24.2")  # kritický problém
        res = upgrades.start(db, tid, sid)
        jid = res["job_id"]
        assert [s["label"] for s in res["steps"]] == ["ap", "sw", "gw"]  # okraj první, gateway poslední
        with pytest.raises(services.ServiceError):
            upgrades.start(db, tid, sid)  # jen jedna běžící v lokalitě

        assert upgrades.advance(db) == 1
        assert _cmd(db, ap)["params"]["version"] == "7.20.5" and _cmd(db, sw) is None
        assert upgrades.advance(db) == 0  # čeká na návrat AP
        _finish_cmd(db, ap)
        assert upgrades.advance(db) == 1
        steps = _job(db, jid)["steps"]
        assert [s["status"] for s in steps] == ["done", "running", "pending"]
        # switch aktualizaci odmítne → zastavit, gateway nechat být
        _finish_cmd(db, sw, status="failed")
        upgrades.advance(db)
        j = _job(db, jid)
        assert j["status"] == "stopped" and "sw" in j["message"]
        assert [s["status"] for s in j["steps"]] == ["done", "failed", "pending"] and _cmd(db, gw) is None

        # nová úloha: zbývá sw a gw; sw se nevrátí → timeout
        jid2 = upgrades.start(db, tid, sid)["job_id"]
        upgrades.advance(db)
        assert upgrades.advance(db, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)) == 0
        upgrades.advance(db, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=31))
        j = _job(db, jid2)
        assert j["status"] == "stopped" and "nevrátilo" in j["message"] and _cmd(db, gw) is None
    finally:
        drop_db(name)


def test_api_skip_cancel_and_isolation():
    db, name, settings, box, app, tid, sid = _env()
    try:
        ap = _dev(db, box, tid, sid, "AP1", "10.200.1.3", "ap", "ap")
        gw = _dev(db, box, tid, sid, "GW1", "10.200.1.1", "gw", "gateway")
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/sites/{sid}/upgrade"
        prev = c.get(base).json()
        assert [x["label"] for x in prev["candidates"]] == ["ap", "gw"] and prev["jobs"] == []
        r = c.post(base)
        assert r.status_code == 201, r.text
        jid = r.json()["job_id"]
        # zařízení mezitím aktualizované ručně → krok se přeskočí
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET ros_version = '7.20.5 (stable)' WHERE id = %(d)s", {"d": ap})
        upgrades.advance(db)
        assert _job(db, jid)["steps"][0]["status"] == "skipped"
        upgrades.advance(db)
        assert _cmd(db, gw) is not None
        assert c.post(f"/api/admin/tenants/{tid}/upgrade-jobs/{jid}/cancel").status_code == 200
        assert _job(db, jid)["status"] == "cancelled"
        _finish_cmd(db, gw)
        assert upgrades.advance(db) == 0  # zrušená úloha se už nehýbe
        # jiný tenant úlohu nevidí ani nezruší
        other = services.create_tenant(db, settings, "Jina")
        with tenant_scope(db, other["id"]) as tx:
            assert tx.fetchall("SELECT id FROM upgrade_job") == []
        with operator_scope(db) as tx:
            acts = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log ORDER BY id")]
        assert "site.upgrade_start" in acts and "site.upgrade_cancel" in acts
    finally:
        drop_db(name)
