"""Dostupnost z alertů offline a odchylky od vlastního normálu zařízení."""

import datetime as dt


def _env2():
    from test_modules import _adopted, _env

    db, name, settings, box, app, tid, sid = _env()
    a = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
    b = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
    return db, name, app, tid, sid, a, b


def test_availability():
    from helpers import drop_db
    from test_flow import _login

    from mtcontroller.db import operator_scope

    db, name, app, tid, sid, sw, ap = _env2()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET adopted_at = %(a)s WHERE id = %(d)s", {"a": now - dt.timedelta(days=100), "d": sw})
            tx.execute("UPDATE device SET adopted_at = %(a)s WHERE id = %(d)s", {"a": now - dt.timedelta(days=10), "d": ap})
            for dev, a, b in ((sw, now - dt.timedelta(days=3, hours=6), now - dt.timedelta(days=3)),         # 6 h
                              (sw, now - dt.timedelta(days=45), now - dt.timedelta(days=44)),                 # mimo okno
                              (sw, now - dt.timedelta(days=31), now - dt.timedelta(days=29, hours=12)),       # přesah: 12 h
                              (ap, now - dt.timedelta(hours=2), None)):                                       # trvá
                tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, severity, message, opened_at, resolved_at)
                              VALUES (%(t)s, %(d)s, 'offline', 'critical', 'offline', %(a)s, %(b)s)""",
                           {"t": tid, "d": dev, "a": a, "b": b})
            tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, severity, message, opened_at, resolved_at)
                          VALUES (%(t)s, %(d)s, 'cpu_high', 'warning', 'cpu', %(a)s, %(b)s)""",
                       {"t": tid, "d": sw, "a": now - dt.timedelta(days=2), "b": now - dt.timedelta(days=1)})
            tx.execute("UPDATE device SET state = 'offline' WHERE id = %(d)s", {"d": ap})
        c = _login(app)
        r = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/availability").json()
        assert r["days"] == 30 and [d["name"] for d in r["devices"]] == ["ap-sklad", "sw-hala"]
        a, s = r["devices"]
        # switch: 18 h výpadků z 30 dní; alert jiného druhu se nepočítá, výpadek mimo okno taky ne
        assert s["outages"] == 2 and abs(s["down_seconds"] - 18 * 3600) < 5 and abs(s["longest_seconds"] - 12 * 3600) < 5
        assert abs(s["pct"] - 100 * (1 - 18 / 720)) < 0.01
        assert len(s["daily"]) == 30 and min(x for x in s["daily"] if x is not None) < 100 and s["daily"][-1] == 100
        assert s["last"][0]["seconds"] == 6 * 3600 and s["last"][0]["to"]
        # AP: adoptováno před 10 dny (dřívější dny bez hodnoty), výpadek pořád trvá
        assert a["outages"] == 1 and a["last"][0]["to"] is None and abs(a["down_seconds"] - 7200) < 5
        assert abs(a["pct"] - 100 * (1 - 2 / 240)) < 0.01 and a["daily"][:15] == [None] * 15 and a["daily"][-1] < 100
        assert a["state"] == "offline" and r["avg_pct"] is not None
        assert len(c.get(f"/api/admin/tenants/{tid}/sites/{sid}/availability?days=7").json()["devices"][1]["daily"]) == 7
        assert c.get(f"/api/admin/tenants/{tid}/sites/{tid}/availability").status_code == 404
    finally:
        drop_db(name)


def test_anomalies_and_sampling():
    from helpers import drop_db

    from mtcontroller import alerts, baseline, inventory
    from mtcontroller.db import operator_scope

    db, name, app, tid, sid, sw, ap = _env2()
    try:
        with operator_scope(db) as tx:
            hour = tx.fetchone("SELECT date_trunc('hour', now()) - interval '1 hour' AS h")["h"]

            def put(dev, h, cpu, mem):
                tx.execute("""INSERT INTO device_hourly (tenant_id, device_id, hour, cpu_sum, cpu_max, mem_sum, samples, last_at)
                              VALUES (%(t)s, %(d)s, %(h)s, %(c)s, %(m)s, %(e)s, 4, %(h)s)""",
                           {"t": tid, "d": dev, "h": h, "c": cpu * 4, "m": cpu, "e": mem * 4})

            def traffic(dev, h, bps):
                tx.execute("""INSERT INTO interface_hourly (tenant_id, device_id, hour, name, rx_bps_avg, tx_bps_avg, samples)
                              VALUES (%(t)s, %(d)s, %(h)s, 'ether1', %(r)s, %(x)s, 10)""",
                           {"t": tid, "d": dev, "h": h, "r": bps // 2, "x": bps // 2})

            # 8 dní historie ve stejnou hodinu: switch kolem 12 % CPU, 40 % paměti, 60 Mb/s; AP jen 3 dny (málo)
            for n in range(1, 9):
                put(sw, hour - dt.timedelta(days=n), 11 + n % 3, 40)
                put(sw, hour - dt.timedelta(days=n, hours=5), 95, 90)       # jiná hodina dne se nepočítá
                traffic(sw, hour - dt.timedelta(days=n), 60_000_000 + n * 1_000_000)
            for n in range(1, 4):
                put(ap, hour - dt.timedelta(days=n), 5, 30)
            put(sw, hour, 78, 44)
            put(ap, hour, 90, 95)
            traffic(sw, hour, 2_000_000)
            got = {(str(a["device_id"]), a["metric"]): a for a in baseline.anomalies(tx)}
            assert set(got) == {(sw, "cpu"), (sw, "traffic_low")}, got
            assert got[(sw, "cpu")]["message"] == "vytížení CPU 78 % – v tuto hodinu bývá kolem 12 %"
            assert "jen 2.0 Mb/s" in got[(sw, "traffic_low")]["message"] and "64 Mb/s" in got[(sw, "traffic_low")]["message"]
            # alert s jménem zařízení; malý skok (12 → 30 %) ani skok provozu pod prahem se nehlásí
            act = [a for a in alerts.conditions(tx) if a.kind == "anomaly"]
            assert sorted(a.subject for a in act) == ["cpu", "traffic_low"] and act[0].severity == "warning"
            assert any(a.message.startswith("sw-hala: nezvyklý stav – vytížení CPU 78 %") for a in act)
            tx.execute("UPDATE device_hourly SET cpu_sum = 30 * 4 WHERE device_id = %(d)s AND hour = %(h)s", {"d": sw, "h": hour})
            tx.execute("UPDATE interface_hourly SET rx_bps_avg = 100000000, tx_bps_avg = 100000000 WHERE device_id = %(d)s AND hour = %(h)s",
                       {"d": sw, "h": hour})
            got = {a["metric"]: a for a in baseline.anomalies(tx)}
            assert set(got) == {"traffic"} and "200 Mb/s" in got["traffic"]["message"]
        # vzorkování: každá inventura se započte jednou; stará inventura (z minulé hodiny) vůbec
        inventory.store(db, tid, ap, {"resource": {"cpu-load": "20", "free-memory": "300", "total-memory": "1000"}}, [])
        assert baseline.sample(db) == 1 and baseline.sample(db) == 0
        inventory.store(db, tid, ap, {"resource": {"cpu-load": "40", "free-memory": "100", "total-memory": "1000"}}, [])
        assert baseline.sample(db) == 1
        with operator_scope(db) as tx:
            r = tx.fetchone("""SELECT cpu_sum, cpu_max, mem_sum, samples FROM device_hourly
                               WHERE device_id = %(d)s AND hour = date_trunc('hour', now())""", {"d": ap})
            assert (r["cpu_sum"], r["cpu_max"], r["mem_sum"], r["samples"]) == (60, 40, 160, 2), r
            tx.execute("UPDATE device SET inventory_at = now() - interval '3 hours' WHERE id = %(d)s", {"d": sw})
        inventory.retention(db)
    finally:
        drop_db(name)
