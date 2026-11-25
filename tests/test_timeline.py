"""Časová osa lokality: události z alertů, portů, klientů, Wi-Fi, plánů a auditu na jedné ose."""

import datetime as dt


def test_timeline_api():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
        now = dt.datetime.now(dt.timezone.utc)
        ago = lambda m: now - dt.timedelta(minutes=m)  # noqa: E731
        mac = "AA:BB:CC:00:00:10"
        with operator_scope(db) as tx:
            uid = tx.fetchone("SELECT id FROM app_user LIMIT 1")["id"]
            tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, severity, message, opened_at, resolved_at)
                          VALUES (%(t)s, %(d)s, 'device_offline', 'critical', 'zařízení se neozývá', %(o)s, %(r)s)""",
                       {"t": tid, "d": ap, "o": ago(50), "r": ago(40)})
            tx.execute("""INSERT INTO port_event (tenant_id, device_id, at, port, field, old, new)
                          VALUES (%(t)s, %(d)s, %(a)s, 'ether5', 'link', 'up', 'down')""", {"t": tid, "d": sw, "a": ago(52)})
            tx.execute("""INSERT INTO port_event (tenant_id, device_id, at, port, field, old, new)
                          VALUES (%(t)s, %(d)s, %(a)s, 'ether5', 'link', 'down', 'up')""", {"t": tid, "d": sw, "a": ago(41)})
            tx.execute("""INSERT INTO client (tenant_id, site_id, mac, hostname) VALUES (%(t)s, %(s)s, %(m)s, 'tiskarna')""",
                       {"t": tid, "s": sid, "m": mac})
            tx.execute("""INSERT INTO client_session (tenant_id, site_id, mac, started_at, seen_at, ended_at, wifi, device, ssid)
                          VALUES (%(t)s, %(s)s, %(m)s, %(a)s, %(b)s, %(b)s, true, 'ap-sklad', 'Firma')""",
                       {"t": tid, "s": sid, "m": mac, "a": ago(300), "b": ago(51)})
            tx.execute("""INSERT INTO wifi_event (tenant_id, site_id, device_id, mac, at, kind, iface, reason)
                          VALUES (%(t)s, %(s)s, %(d)s, %(m)s, %(a)s, 'auth_fail', 'wifi1', 'spatne heslo')""",
                       {"t": tid, "s": sid, "d": ap, "m": mac, "a": ago(30)})
            tx.execute("""INSERT INTO wifi_event (tenant_id, site_id, device_id, mac, at, kind, iface)
                          VALUES (%(t)s, %(s)s, %(d)s, %(m)s, %(a)s, 'connected', 'wifi1')""",
                       {"t": tid, "s": sid, "d": ap, "m": mac, "a": ago(29)})
            tx.execute("""INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, basis_sha, status,
                                                  created_by, finished_at)
                          VALUES (%(t)s, %(d)s, 'ports', 'ether5: PoE vypnuto', '{}', '', %(b)s, 'applied', %(u)s, %(a)s)""",
                       {"t": tid, "d": sw, "b": "0" * 64, "u": uid, "a": ago(55)})
            for action, target, at in (("device.reboot", f"device:{sw}", ago(20)), ("site.intent", f"site:{sid}", ago(10)),
                                       ("auth.login", f"site:{sid}", ago(5)), ("device.reboot", f"device:{sw}", ago(60 * 30))):
                tx.execute("INSERT INTO audit_log (at, user_id, tenant_id, action, target) VALUES (%(a)s, %(u)s, %(t)s, %(x)s, %(g)s)",
                           {"a": at, "u": uid, "t": tid, "x": action, "g": target})
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/timeline"
        r = c.get(url + "?hours=2").json()
        ev = r["events"]
        kinds = [(e["cat"], e["kind"]) for e in ev]
        # od nejnovější; wifi „connected“ ze syslogu se neopakuje (připojení/odpojení jde z relací), přihlášení do auditu nepatří
        assert kinds == [("audit", "site.intent"), ("audit", "device.reboot"), ("wifi", "auth_fail"), ("alert", "resolved"),
                         ("port", "link"), ("alert", "opened"), ("client", "disconnected"), ("port", "link"),
                         ("change", "applied")], kinds
        assert r["total"] == 9 and len(r["buckets"]) == 48 and sum(b["n"] for b in r["buckets"]) == 9
        assert max(b["sev"] for b in r["buckets"] if b["n"]) == "warning" or any(b["sev"] == "critical" for b in r["buckets"])
        by = {(e["cat"], e["kind"], e.get("b")): e for e in ev}
        down = by[("port", "link", "down")]
        assert down["sev"] == "warning" and down["device"] == "sw-hala" and down["port"] == "ether5" and down["text"] == "linka dole"
        off = next(e for e in ev if e["cat"] == "alert" and e["kind"] == "opened")
        assert off["sev"] == "critical" and off["device"] == "ap-sklad" and off["a"] == "device_offline"
        cl = next(e for e in ev if e["cat"] == "client")
        assert cl["client"] == "tiskarna" and cl["via"] == "ap-sklad" and cl["port"] == "Firma" and cl["wifi"] is True
        ch = next(e for e in ev if e["cat"] == "change")
        assert ch["text"] == "ether5: PoE vypnuto" and ch["user"] and ch["a"] == "ports"
        assert [d["name"] for d in r["devices"]] == ["ap-sklad", "sw-hala"]
        # filtry: kategorie, zařízení, klient
        assert {e["cat"] for e in c.get(url + "?hours=2&cats=port,alert").json()["events"]} == {"port", "alert"}
        r = c.get(url + f"?hours=2&device={sw}").json()
        assert {e.get("device") for e in r["events"]} == {"sw-hala"} and len(r["events"]) == 4
        r = c.get(url + f"?hours=2&mac={mac.lower()}").json()
        assert [(e["cat"], e["kind"]) for e in r["events"]] == [("wifi", "auth_fail"), ("client", "disconnected")]
        # úzké okno kolem výpadku: ±5 min kolem pádu linky → co se dělo kolem
        until = (ago(52) + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        r = c.get(url + f"?minutes=10&until={until}").json()
        assert [(e["cat"], e["kind"]) for e in r["events"]] == [("alert", "opened"), ("client", "disconnected"),
                                                                 ("port", "link"), ("change", "applied")], r["events"]
        assert r["minutes"] == 10
        assert c.get(url + "?until=vcera").status_code == 400 and c.get(url + "?mac=xx").status_code == 400
        assert c.get(f"/api/admin/tenants/{tid}/sites/{tid}/timeline").status_code == 404
    finally:
        drop_db(name)


def test_state_at_for_map_replay():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
        now = dt.datetime.now(dt.timezone.utc)
        ago = lambda m: now - dt.timedelta(minutes=m)  # noqa: E731
        with operator_scope(db) as tx:
            tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, severity, message, opened_at, resolved_at)
                          VALUES (%(t)s, %(d)s, 'offline', 'critical', 'zařízení se neozývá', %(o)s, %(r)s)""",
                       {"t": tid, "d": ap, "o": ago(50), "r": ago(40)})
            tx.execute("""INSERT INTO alert (tenant_id, device_id, kind, subject, severity, message, opened_at)
                          VALUES (%(t)s, %(d)s, 'port_errors', 'ether5', 'warning', 'chyby na portu', %(o)s)""",
                       {"t": tid, "d": sw, "o": ago(45)})
            lid = tx.fetchone("""INSERT INTO wlink (tenant_id, site_id, kind, a_device, a_iface, a_mac, b_device, b_iface, b_mac)
                                 VALUES (%(t)s, %(s)s, 'foreign', %(a)s, 'ether7', '-', %(b)s, 'ether1', '-') RETURNING id""",
                              {"t": tid, "s": sid, "a": sw, "b": ap})["id"]
            for m, up, loss in ((48, False, 100), (30, True, 4), (2, True, 0)):
                tx.execute("INSERT INTO wlink_sample (tenant_id, link_id, at, up, loss) VALUES (%(t)s, %(l)s, %(a)s, %(u)s, %(x)s)",
                           {"t": tid, "l": lid, "a": ago(m), "u": up, "x": loss})
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/infra-at"
        from urllib.parse import quote

        get = lambda v: c.get(url + "?at=" + quote(v))  # noqa: E731
        at = lambda m: ago(m).isoformat()  # noqa: E731
        r = get(at(46)).json()
        assert r["offline"] == [ap] and [a["kind"] for a in r["alerts"]] == ["offline"] and r["links"] == {str(lid): "down"}
        r = get(at(29)).json()                                   # AP zpět, chyby na portu trvají, spoj ztrácí
        assert r["offline"] == [] and [(a["kind"], a["device"]) for a in r["alerts"]] == [("port_errors", "sw-hala")]
        assert r["links"] == {str(lid): "degraded"}
        r = get(at(60)).json()                                   # před tím vším klid, spoj bez měření
        assert r["alerts"] == [] and r["links"] == {}
        assert get(at(1)).json()["links"] == {str(lid): "ok"}
        assert get("včera").status_code == 400
    finally:
        drop_db(name)
