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
