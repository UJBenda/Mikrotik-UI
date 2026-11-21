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


def test_canary_soak():
    """První zařízení se po aktualizaci sleduje; když není v pořádku, zbytek zůstane beze změny."""
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _dev(db, box, tid, sid, "GW1", "10.200.1.1", "gw", "gateway")
        ap = _dev(db, box, tid, sid, "AP1", "10.200.1.3", "ap", "ap")
        now = dt.datetime.now(dt.timezone.utc)
        jid = upgrades.start(db, tid, sid, canary=10)["job_id"]
        assert _job(db, jid)["steps"][0]["canary"] == 10 and "canary" not in _job(db, jid)["steps"][1]
        upgrades.advance(db, now=now)
        _finish_cmd(db, ap)
        assert upgrades.advance(db, now=now) == 1
        st = _job(db, jid)["steps"]
        assert [s["status"] for s in st] == ["soak", "pending"] and "10 min" in st[0]["message"]
        assert upgrades.advance(db, now=now + dt.timedelta(minutes=4)) == 0 and _cmd(db, gw) is None  # ještě se sleduje
        # po uplynutí doby v pořádku → pokračuje bránou
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET last_checkin_at = %(t)s WHERE id = %(d)s", {"t": now + dt.timedelta(minutes=10), "d": ap})
        assert upgrades.advance(db, now=now + dt.timedelta(minutes=11)) == 1
        assert [s["status"] for s in _job(db, jid)["steps"]] == ["done", "running"] and _cmd(db, gw)
        upgrades.cancel(db, tid, jid)
        with operator_scope(db) as tx:
            tx.execute("UPDATE device_command SET status = 'failed', finished_at = now() WHERE device_id = %(d)s", {"d": gw})
            tx.execute("UPDATE device SET ros_version = '7.19 (stable)' WHERE id = %(d)s", {"d": ap})

        # kanárek během sledování vypadne → stop, brána beze změny
        jid = upgrades.start(db, tid, sid, canary=10)["job_id"]
        upgrades.advance(db, now=now)
        _finish_cmd(db, ap)
        upgrades.advance(db, now=now)
        with operator_scope(db) as tx:
            n = tx.fetchone("SELECT count(*) AS n FROM device_command WHERE device_id = %(d)s AND kind = 'upgrade'", {"d": gw})["n"]
            tx.execute("UPDATE device SET state = 'offline' WHERE id = %(d)s", {"d": ap})
        assert upgrades.advance(db, now=now + dt.timedelta(minutes=2)) == 1
        j = _job(db, jid)
        assert j["status"] == "stopped" and "první zařízení" in j["message"] and "vypadlo" in j["message"]
        assert [s["status"] for s in j["steps"]] == ["failed", "pending"]
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM device_command WHERE device_id = %(d)s AND kind = 'upgrade'", {"d": gw})["n"] == n
            tx.execute("UPDATE device SET state = 'adopted', ros_version = '7.19 (stable)' WHERE id = %(d)s", {"d": ap})

        # kritický alert otevřený po aktualizaci → stop
        jid = upgrades.start(db, tid, sid, canary=10)["job_id"]
        upgrades.advance(db, now=now)
        _finish_cmd(db, ap)
        upgrades.advance(db, now=now)
        with operator_scope(db) as tx:
            tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, subject, severity, message, opened_at)
                          VALUES (%(t)s, %(d)s, 'test', 'x', 'critical', 'ap: rádia neběží', %(o)s)""",
                       {"t": tid, "d": ap, "o": now + dt.timedelta(minutes=1)})
        upgrades.advance(db, now=now + dt.timedelta(minutes=2))
        j = _job(db, jid)
        assert j["status"] == "stopped" and "rádia neběží" in j["message"]
        # jediné zařízení kanárka nepotřebuje; nesmyslná hodnota se odmítne
        with pytest.raises(services.ServiceError):
            upgrades.start(db, tid, sid, canary="x")
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


def test_topology_order_and_manual():
    """Od konce sítě podle sousedů (gateway → sw-core → sw-sklad → ap); ručně vlastní výběr a pořadí."""
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _dev(db, box, tid, sid, "GW1", "10.200.1.1", "gw", "gateway")
        core = _dev(db, box, tid, sid, "SW1", "10.200.1.2", "sw-core", "switch")
        sklad = _dev(db, box, tid, sid, "SW2", "10.200.1.3", "sw-sklad", "switch")
        ap = _dev(db, box, tid, sid, "AP1", "10.200.1.4", "ap", "ap")

        def link(a, b, port):
            with operator_scope(db) as tx:
                cur = tx.fetchone("SELECT data FROM device_inventory WHERE device_id = %(d)s AND section = 'neighbors'",
                                  {"d": a})
            rows = (cur["data"] if cur else []) + [{"interface": port, "identity": b}]
            inventory.store(db, tid, a, {"neighbors": rows}, [])

        link(gw, "sw-core", "ether2")
        link(core, "sw-sklad", "ether5")
        link(sklad, "ap", "ether3")
        with tenant_scope(db, tid) as tx:
            c = upgrades.candidates(tx, sid)
        assert [(x["identity"], x["depth"]) for x in c] == [("ap", 3), ("sw-sklad", 2), ("sw-core", 1), ("gw", 0)]
        # ručně: jen dvě zařízení v mém pořadí
        res = upgrades.start(db, tid, sid, order=[core, ap])
        assert [s["label"] for s in res["steps"]] == ["sw-core", "ap"]
        with operator_scope(db) as tx:
            tx.execute("UPDATE upgrade_job SET status = 'stopped'")
        with pytest.raises(services.ServiceError):
            upgrades.start(db, tid, sid, order=[core, core])
        with pytest.raises(services.ServiceError):
            upgrades.start(db, tid, sid, order=["00000000-0000-0000-0000-000000000000"])
        # API: pořadí v těle POST
        c2 = _login(app)
        r = c2.post(f"/api/admin/tenants/{tid}/sites/{sid}/upgrade", json_body={"order": [ap]})
        assert r.status_code == 201 and [s["label"] for s in r.json()["steps"]] == ["ap"]
        g = c2.get(f"/api/admin/tenants/{tid}/sites/{sid}/upgrade").json()
        assert [x["depth"] for x in g["candidates"]] == [3, 2, 1, 0]
    finally:
        drop_db(name)
