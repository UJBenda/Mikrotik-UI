"""Přehled lokality: měření internetu z brány a souhrn pro úvodní obrazovku."""
from test_wlink import FakeApi


def test_dash_and_wan_probe(monkeypatch):
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory, services, wanprobe
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw-hala")
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/dash"
        r = c.get(url).json()                                         # bez brány a bez dat: nic nespadne
        assert r["gateway"] is None and r["probe"]["points"] == [] and r["probe"]["availability"] is None
        assert r["counts"]["clients"] == 0 and r["todo"] == [] and r["top_aps"] == []

        state = {"loss": 0}
        monkeypatch.setattr(services, "device_api", lambda tx, b, dev, timeout=10.0: FakeApi("-", -50, loss=state["loss"]))
        assert wanprobe.sample(db, box) == 0                          # žádné zařízení nemá roli brány
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET role = 'gateway' WHERE id = %(d)s", {"d": gw})
            tx.execute("UPDATE device SET role = 'ap' WHERE id = %(d)s", {"d": ap})
        inventory.store(db, tid, gw, {
            "resource": [{"uptime": "1w2d5h", "version": "7.16"}],
            "addresses": [{"address": "193.165.120.202/24", "interface": "ether1"}],
            "routes_default": [{"dst-address": "0.0.0.0/0", "gateway": "193.165.120.1", "immediate-gw": "193.165.120.1%ether1",
                                "active": "true", "distance": "1"}]}, [])
        assert wanprobe.sample(db, box) == 1 and wanprobe.sample(db, box) == 0   # další až za 5 minut
        state["loss"] = 10
        with operator_scope(db) as tx:
            tx.execute("UPDATE wan_probe SET at = at - interval '10 minutes'")
        assert wanprobe.sample(db, box) == 1
        with operator_scope(db) as tx:
            hour = "date_trunc('hour', now())"
            tx.execute(f"""INSERT INTO interface_hourly (tenant_id, device_id, hour, name, rx_bps_avg, tx_bps_avg, samples)
                           VALUES (%(t)s, %(g)s, {hour}, 'ether1', 8000000, 800000, 12),
                                  (%(t)s, %(a)s, {hour}, 'wifi1', 1600000, 0, 12), (%(t)s, %(g)s, {hour}, 'ether5', 800000, 0, 12)""",
                       {"t": tid, "g": gw, "a": ap})
            tx.execute("""INSERT INTO client (tenant_id, site_id, mac, hostname, device_id, device, port, ssid, signal)
                          VALUES (%(t)s, %(s)s, '50:00:00:00:00:01', 'pc-vera', %(g)s, 'gw-hala', 'ether5', NULL, NULL),
                                 (%(t)s, %(s)s, '50:00:00:00:00:02', 'telefon', %(a)s, 'ap-sklad', 'wifi1', 'novak', '-61'),
                                 (%(t)s, %(s)s, '50:00:00:00:00:03', 'tablet', %(a)s, 'ap-sklad', 'wifi1', 'novak', '-71@HE')""",
                       {"t": tid, "s": sid, "g": gw, "a": ap})
            tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, severity, message)
                          VALUES (%(t)s, %(a)s, 'offline', 'critical', 'zařízení se neozývá')""", {"t": tid, "a": ap})
            tx.execute("""INSERT INTO wifi_event (tenant_id, site_id, device_id, mac, kind)
                          VALUES (%(t)s, %(s)s, %(a)s, '50:00:00:00:00:02', 'connected'),
                                 (%(t)s, %(s)s, %(a)s, '50:00:00:00:00:02', 'connected'),
                                 (%(t)s, %(s)s, %(a)s, '50:00:00:00:00:09', 'auth_fail')""", {"t": tid, "s": sid, "a": ap})
        r = c.get(url + "?hours=24").json()
        g = r["gateway"]
        assert g["name"] == "gw-hala" and g["uptime"] == "1w2d5h" and g["wans"][0]["interface"] == "ether1"
        assert g["wans"][0]["address"] == "193.165.120.202" and g["wans"][0]["month_bytes"] == 8800000 * 450
        assert r["counts"]["gateway"] == 1 and r["counts"]["ap"] == 1 and r["counts"]["clients"] == 3
        pr = r["probe"]
        assert pr["samples"] == 2 and pr["availability"] == 50.0 and len(pr["points"]) >= 1
        assert {t["target"] for t in pr["targets"]} == {"1.1.1.1", "8.8.8.8"} and pr["targets"][0]["loss"] == 100.0
        assert r["top_aps"] == [{"device_id": ap, "name": "ap-sklad", "bytes": 1600000 * 450, "clients": 2}]
        assert [(x["name"], x["port"], x["bytes"]) for x in r["top_clients"]] == [("pc-vera", "ether5", 800000 * 450)]
        assert r["signal"] == [{"device_id": ap, "name": "ap-sklad", "signal": -66, "clients": 2}]
        assert r["wifi_auth"] == {"ok": 2, "fail": 1}
        assert r["todo_total"] == 1 and r["todo"][0]["device"] == "ap-sklad" and r["todo"][0]["kind"] == "offline"
        with tenant_scope(db, tid) as tx:
            assert wanprobe.series(tx, sid, 24 * 7)["samples"] == 2
    finally:
        drop_db(name)
