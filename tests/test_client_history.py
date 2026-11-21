"""Historie klientů: zápis workerem, poprvé/naposledy, přesun, hledání, retence, izolace."""

from helpers import drop_db
from test_flow import _login
from test_modules import _adopted, _env

from mtcontroller import clients, inventory, services
from mtcontroller.db import operator_scope


GW, SW, AP = "gw", "sw", "ap"


def _iface(name, mac):
    return {"name": name, "type": "ether", "mac-address": mac}


def _site():
    """gw ── sw (ether5 = PC) ── ap (wifi telefon); stejná topologie jako test_clients."""
    gw = {"interfaces": [_iface("ether1", "AA:00:00:00:00:01")],
          "neighbors": [{"interface": "ether1", "mac-address": "BB:00:00:00:00:01", "identity": "sw"}],
          "bridge_hosts": [{"mac-address": "AA:AA:AA:AA:AA:01", "on-interface": "ether1", "vid": "10", "local": "false"}],
          "dhcp_leases": [{"mac-address": "AA:AA:AA:AA:AA:01", "address": "192.168.10.50", "host-name": "pc",
                           "status": "bound"},
                          {"mac-address": "AA:AA:AA:AA:AA:09", "address": "192.168.10.99", "status": "waiting"}]}
    sw = {"interfaces": [_iface("ether1", "BB:00:00:00:00:01")],
          "neighbors": [{"interface": "ether1", "mac-address": "AA:00:00:00:00:01", "identity": "gw"},
                        {"interface": "ether2", "mac-address": "CC:00:00:00:00:01", "identity": "ap"}],
          "bridge_hosts": [{"mac-address": "AA:AA:AA:AA:AA:01", "on-interface": "ether5", "vid": "10", "local": "false"},
                           {"mac-address": "AA:AA:AA:AA:AA:03", "on-interface": "ether2", "vid": "20", "local": "false"}]}
    ap = {"interfaces": [_iface("ether1", "CC:00:00:00:00:01")],
          "neighbors": [{"interface": "ether1", "mac-address": "BB:00:00:00:00:01", "identity": "sw"}],
          "wifi_clients": [{"interface": "wifi1", "mac-address": "AA:AA:AA:AA:AA:03", "ssid": "Firma", "signal": "-58"}]}
    return None, {GW: gw, SW: sw, AP: ap}


def test_record_history_search_and_purge():
    db, name, settings, box, app, tid, sid = _env()
    try:
        devs, invs = _site()
        ids = {}
        for i, (key, ident) in enumerate(((GW, "gw"), (SW, "sw"), (AP, "ap")), start=1):
            ids[key] = _adopted(db, box, tid, sid, f"S{i}", f"10.200.1.{i}", ident)
        # inventury pod skutečnými id zařízení (MAC infrastruktury zůstávají)
        for key, did in ids.items():
            inventory.store(db, tid, did, invs[key], [])
        n = clients.record(db)
        assert n == 2  # PC a telefon; lease ve stavu waiting není přítomný klient
        with operator_scope(db) as tx:
            rows = {r["mac"]: r for r in tx.fetchall("SELECT * FROM client")}
        pc = rows["AA:AA:AA:AA:AA:01"]
        assert pc["device"] == "sw" and pc["port"] == "ether5" and pc["ip"] == "192.168.10.50"
        assert rows["AA:AA:AA:AA:AA:03"]["ssid"] == "Firma"  # wifi klient
        first = pc["first_seen"]

        # klient se přesune na jiný port; poprvé viděn zůstává
        invs[SW]["bridge_hosts"] = [dict(h, **{"on-interface": "ether7"}) if h.get("mac-address") == "AA:AA:AA:AA:AA:01"
                                    else h for h in invs[SW]["bridge_hosts"]]
        inventory.store(db, tid, ids[SW], invs[SW], [])
        with operator_scope(db) as tx:
            tx.execute("UPDATE client SET last_seen = now() - interval '1 hour'")
        clients.record(db)
        with operator_scope(db) as tx:
            pc = tx.fetchone("SELECT * FROM client WHERE mac = 'AA:AA:AA:AA:AA:01'")
            assert pc["port"] == "ether7" and pc["first_seen"] == first
            assert tx.fetchone("SELECT now() - last_seen < interval '1 minute' AS fresh FROM client "
                               "WHERE mac = 'AA:AA:AA:AA:AA:01'")["fresh"]

        c = _login(app)
        live = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/clients").json()
        assert all(x["online"] and x["first_seen"] for x in live if x["mac"] == "AA:AA:AA:AA:AA:01")
        hist = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/clients?history=1").json()
        assert any(h["mac"] == "AA:AA:AA:AA:AA:01" and h["online"] for h in hist)
        found = c.get(f"/api/admin/tenants/{tid}/clients?q=192.168.10.50").json()
        assert found and found[0]["site"] == "Praha" and found[0]["port"] == "ether7"
        assert c.get(f"/api/admin/tenants/{tid}/clients?q=%25").json() == []  # % je jen znak, ne zástupný
        assert c.get(f"/api/admin/tenants/{tid}/clients?q=a").json() == []  # krátký dotaz
        # jiný tenant klienty nevidí
        other = services.create_tenant(db, settings, "Jina")
        assert c.get(f"/api/admin/tenants/{other['id']}/clients?q=AA:AA").json() == []

        with operator_scope(db) as tx:
            tx.execute("UPDATE client SET last_seen = now() - interval '91 days' WHERE mac = 'AA:AA:AA:AA:AA:01'")
        assert clients.purge(db) == 1
    finally:
        drop_db(name)


def test_sessions_timeline_and_time_travel():
    """Relace klientů: kolik klientů bylo připojeno v daný čas, kdo to byl, jen Wi-Fi."""
    import datetime as dt

    from mtcontroller.db import tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        _devs, invs = _site()
        ids = {key: _adopted(db, box, tid, sid, f"S{i}", f"10.200.1.{i}", ident)
               for i, (key, ident) in enumerate(((GW, "gw"), (SW, "sw"), (AP, "ap")), start=1)}
        for key, did in ids.items():
            inventory.store(db, tid, did, invs[key], [])
        clients.record(db)
        clients.record(db)  # druhý běh relace jen potvrdí, nezdvojí
        with operator_scope(db) as tx:
            ses = {r["mac"]: r for r in tx.fetchall("SELECT * FROM client_session")}
            assert len(ses) == 2 and all(r["ended_at"] is None for r in ses.values())
            assert ses["AA:AA:AA:AA:AA:03"]["wifi"] is True and ses["AA:AA:AA:AA:AA:03"]["ssid"] == "Firma"
            assert ses["AA:AA:AA:AA:AA:01"]["wifi"] is False and ses["AA:AA:AA:AA:AA:01"]["port"] == "ether5"
            # relace běží už 3 hodiny
            tx.execute("UPDATE client_session SET started_at = now() - interval '3 hours'")
        # telefon se odpojí
        invs[AP]["wifi_clients"] = []
        invs[SW]["bridge_hosts"] = [h for h in invs[SW]["bridge_hosts"] if h["mac-address"] != "AA:AA:AA:AA:AA:03"]
        inventory.store(db, tid, ids[AP], invs[AP], [])
        inventory.store(db, tid, ids[SW], invs[SW], [])
        clients.record(db)
        now = dt.datetime.now(dt.timezone.utc)
        with tenant_scope(db, tid) as tx:
            before = clients.at(tx, sid, now - dt.timedelta(hours=1))
            assert {c["mac"] for c in before} == {"AA:AA:AA:AA:AA:01", "AA:AA:AA:AA:AA:03"}
            phone = next(c for c in before if c["mac"] == "AA:AA:AA:AA:AA:03")
            assert phone["was_wifi"] and phone["ssid"] == "Firma" and phone["online"] is False and phone["ended_at"]
            assert [c["mac"] for c in clients.at(tx, sid, now + dt.timedelta(seconds=1))] == ["AA:AA:AA:AA:AA:01"]
            assert clients.at(tx, sid, now - dt.timedelta(hours=5)) == []
            tl = clients.timeline(tx, sid, 6, now=now)
            assert tl["step_min"] == 5 and len(tl["points"]) == 73 and tl["since"]
            assert (tl["points"][0]["total"], tl["points"][0]["wifi"]) == (0, 0)          # před 6 h nikdo
            assert (tl["points"][-13]["total"], tl["points"][-13]["wifi"]) == (2, 1)      # před hodinou oba
            assert clients.timeline(tx, sid, 24 * 7, now=now)["step_min"] == 60
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/sites/{sid}/clients"
        r = c.get(base + "/timeline?hours=6").json()
        assert len(r["points"]) == 73 and r["points"][-13]["total"] == 2
        when = (now - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        got = c.get(base + f"?at={when}").json()
        assert len(got) == 2 and any(x["hostname"] == "pc" for x in got)
        assert c.get(base + "?at=nesmysl").status_code == 400
        # worker stál: nepotvrzená relace skončí posledním potvrzením, ne až teď
        with operator_scope(db) as tx:
            tx.execute("UPDATE client_session SET seen_at = now() - interval '2 hours' WHERE ended_at IS NULL")
        clients.record(db)
        with operator_scope(db) as tx:
            rows = tx.fetchall("SELECT ended_at, now() - ended_at > interval '1 hour' AS old FROM client_session "
                               "WHERE mac = 'AA:AA:AA:AA:AA:01' ORDER BY started_at")
            assert len(rows) == 2 and rows[0]["old"] is True and rows[1]["ended_at"] is None
            tx.execute("UPDATE client_session SET ended_at = now() - interval '91 days' WHERE ended_at IS NOT NULL")
        clients.purge(db)
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM client_session")["n"] == 1
        # jiný tenant relace nevidí
        other = services.create_tenant(db, settings, "Jina")
        with tenant_scope(db, other["id"]) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM client_session")["n"] == 0
    finally:
        drop_db(name)
