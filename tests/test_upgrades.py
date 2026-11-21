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


def _fw_cmd(db, did):
    with operator_scope(db) as tx:
        return tx.fetchone("""SELECT id, status::text AS status, params FROM device_command
                              WHERE device_id = %(d)s AND kind = 'firmware' ORDER BY id DESC LIMIT 1""", {"d": did})


def test_firmware_follows_routeros_upgrade():
    """RouterBOOT se dotáhne hned po RouterOS: příkaz firmware po ohlášení nové verze, úloha na něj počká."""
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _dev(db, box, tid, sid, "GW1", "10.200.1.1", "gw", "gateway")
        ap = _dev(db, box, tid, sid, "AP1", "10.200.1.3", "ap", "ap")
        now = dt.datetime.now(dt.timezone.utc)

        def report(did, version, state="adopted"):
            with tenant_scope(db, tid) as tx:
                dev = tx.fetchone("SELECT id, ros_version FROM device WHERE id = %(d)s", {"d": did})
                services._firmware_follow(tx, tid, dev, version, state)
                tx.execute("UPDATE device SET ros_version = %(v)s, last_checkin_at = now() WHERE id = %(d)s",
                           {"v": version, "d": did})

        # změna verze bez aktualizace od controlleru (někdo ji udělal ručně) → nic se nevynucuje
        report(gw, "7.19.1 (stable)")
        assert _fw_cmd(db, gw) is None
        jid = upgrades.start(db, tid, sid)["job_id"]
        upgrades.advance(db, now=now)
        with operator_scope(db) as tx:
            tx.execute("""UPDATE device_command SET status = 'done', finished_at = now()
                          WHERE device_id = %(d)s AND kind = 'upgrade'""", {"d": ap})
        report(ap, "7.20.5 (stable)")
        fw = _fw_cmd(db, ap)
        assert fw and fw["status"] == "queued" and fw["params"]["after_upgrade"] == _cmd(db, ap)["id"]
        report(ap, "7.20.5 (stable)")                      # další check-in nic nepřidá
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM device_command WHERE kind = 'firmware'")["n"] == 1
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'device.firmware_follow'")["n"] == 1
        # úloha čeká, dokud firmware neproběhne a zařízení se po něm znovu neohlásí
        assert upgrades.advance(db, now=now) == 0
        st = _job(db, jid)["steps"]
        assert st[0]["status"] == "running" and "RouterBOOT" in st[0]["message"] and _cmd(db, gw) is None
        with operator_scope(db) as tx:
            tx.execute("UPDATE device_command SET status = 'done', finished_at = now() WHERE id = %(i)s", {"i": fw["id"]})
        assert upgrades.advance(db, now=now) == 0          # potvrzeno, ale restart ještě neproběhl
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET last_checkin_at = now() + interval '2 minutes' WHERE id = %(d)s", {"d": ap})
        assert upgrades.advance(db, now=now) == 1
        assert [s["status"] for s in _job(db, jid)["steps"]] == ["done", "running"]
        # skript příkazu firmware restartuje jen při rozdílu verzí
        from mtcontroller import routeros
        assert "current-firmware" in routeros.FIRMWARE_BODY and "/system reboot" in routeros.FIRMWARE_TAIL
    finally:
        drop_db(name)


def test_firmware_only_candidates_and_step():
    """Zařízení s aktuálním RouterOS a starším RouterBOOT jde do úlohy jako krok „jen firmware“."""
    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _dev(db, box, tid, sid, "SW1", "10.200.1.2", "sw", "switch", latest="7.19")   # RouterOS aktuální
        ok = _dev(db, box, tid, sid, "SW2", "10.200.1.4", "sw2", "switch", latest="7.19")
        inventory.store(db, tid, sw, {"routerboard": {"current-firmware": "7.15", "upgrade-firmware": "7.19"}}, [])
        inventory.store(db, tid, ok, {"routerboard": {"current-firmware": "7.19", "upgrade-firmware": "7.19"}}, [])
        with tenant_scope(db, tid) as tx:
            c = upgrades.candidates(tx, sid)
        assert [(x["identity"], x["kind"], x["fw_from"], x["fw_to"]) for x in c] == [("sw", "firmware", "7.15", "7.19")]
        now = dt.datetime.now(dt.timezone.utc)
        res = upgrades.start(db, tid, sid)
        assert res["steps"][0]["kind"] == "firmware" and res["steps"][0]["fw"] == "7.15 → 7.19"
        assert upgrades.advance(db, now=now) == 1
        fw = _fw_cmd(db, sw)
        assert fw and fw["status"] == "queued" and _cmd(db, sw) is None
        assert upgrades.advance(db, now=now) == 0
        with operator_scope(db) as tx:
            tx.execute("UPDATE device_command SET status = 'done', finished_at = now() WHERE id = %(i)s", {"i": fw["id"]})
            tx.execute("UPDATE device SET last_checkin_at = now() + interval '2 minutes' WHERE id = %(d)s", {"d": sw})
        assert upgrades.advance(db, now=now) == 1
        upgrades.advance(db, now=now)
        assert _job(db, res["job_id"])["status"] == "done"
        c2 = _login(app)
        v = c2.get(f"/api/admin/tenants/{tid}/sites/{sid}/upgrade").json()
        assert v["candidates"][0]["kind"] == "firmware" and v["candidates"][0]["fw"] == "7.15 → 7.19"
        assert v["auto"]["enabled"] is False and v["auto"]["delay_days"] == 7
    finally:
        drop_db(name)


def test_autoupdate_window_delay_and_schedule():
    from mtcontroller import autoupdate

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _dev(db, box, tid, sid, "GW1", "10.200.1.1", "gw", "gateway")
        ap = _dev(db, box, tid, sid, "AP1", "10.200.1.3", "ap", "ap")
        now = dt.datetime.now(dt.timezone.utc)
        wed_night = dt.datetime(2026, 11, 25, 2, 30)     # středa 02:30 místního času
        wed_day = dt.datetime(2026, 11, 25, 14, 0)
        assert autoupdate.run(db, now, wed_night) == 0   # bez politiky nic

        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/autoupdate"
        for bad in ({"enabled": True, "days": []}, {"days": [7]}, {"from": "25:00"}, {"from": "02:00", "to": "02:00"},
                    {"delay_days": 99}, {"scheduled": {"at": "2020-01-01T00:00:00Z"}}, {"scheduled": {"at": "x"}}):
            assert c.put(url, json_body=bad).status_code == 400, bad
        r = c.put(url, json_body={"enabled": True, "days": [2], "from": "02:00", "to": "05:00", "delay_days": 7})
        assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["canary"] == 10
        # okno přes půlnoc patří ke dni začátku
        pol = {"days": [2], "from": "22:00", "to": "04:00"}
        assert autoupdate.in_window(pol, dt.datetime(2026, 11, 25, 23, 0)) and autoupdate.in_window(pol, dt.datetime(2026, 11, 26, 3, 0))
        assert not autoupdate.in_window(pol, dt.datetime(2026, 11, 25, 3, 0)) and not autoupdate.in_window(pol, dt.datetime(2026, 11, 26, 23, 0))

        # verze je nabízená poprvé → odklad 7 dní: zatím nic, jen se zapamatuje
        assert autoupdate.run(db, now, wed_night) == 0
        with tenant_scope(db, tid) as tx:
            assert list(autoupdate.get(tx, sid)["seen"]) == ["7.20.5"]
        # po 8 dnech mimo okno nic, v okně se spustí (od konce sítě, s kanárkem)
        later = now + dt.timedelta(days=8)
        assert autoupdate.run(db, later, wed_day) == 0
        assert autoupdate.run(db, later, wed_night) == 1
        with operator_scope(db) as tx:
            job = tx.fetchone("SELECT id, steps, created_by FROM upgrade_job ORDER BY id DESC LIMIT 1")
            acts = [x["action"] for x in tx.fetchall("SELECT action FROM audit_log ORDER BY id")]
        assert [s["label"] for s in job["steps"]] == ["ap", "gw"] and job["steps"][0]["canary"] == 10 and job["created_by"] is None
        assert "site.autoupdate" in acts and "site.autoupdate_run" in acts
        assert autoupdate.run(db, later, wed_night) == 0           # běží → znovu ne
        upgrades.cancel(db, tid, job["id"])
        assert autoupdate.run(db, later + dt.timedelta(hours=1), wed_night) == 0   # nejvýš jednou za 20 h
        v = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/upgrade").json()["auto"]
        assert "automatická aktualizace spuštěna (2 zařízení)" in v["last"]["result"]

        # jednorázově naplánovaná aktualizace jen pro vybrané zařízení (automatika vypnutá)
        at = (now + dt.timedelta(hours=2)).isoformat()
        r = c.put(url, json_body={"enabled": False, "scheduled": {"at": at, "order": [gw]}})
        assert r.status_code == 200 and r.json()["scheduled"]["order"] == [gw]
        assert autoupdate.run(db, now + dt.timedelta(hours=1), wed_day) == 0
        assert autoupdate.run(db, now + dt.timedelta(hours=2, minutes=1), wed_day) == 1
        with operator_scope(db) as tx:
            job = tx.fetchone("SELECT id, steps, created_by FROM upgrade_job ORDER BY id DESC LIMIT 1")
        assert [s["label"] for s in job["steps"]] == ["gw"] and job["created_by"] is not None
        with tenant_scope(db, tid) as tx:
            pol = autoupdate.get(tx, sid)
        assert pol["scheduled"] is None and "naplánovaná aktualizace spuštěna" in pol["last"]["result"]
        upgrades.cancel(db, tid, job["id"])
        # propadlý termín (controller neběžel) se nespustí
        c.put(url, json_body={"enabled": False, "scheduled": {"at": at}})
        assert autoupdate.run(db, now + dt.timedelta(hours=9), wed_day) == 0
        with tenant_scope(db, tid) as tx:
            pol = autoupdate.get(tx, sid)
        assert pol["scheduled"] is None and "propadla" in pol["last"]["result"]
    finally:
        drop_db(name)
