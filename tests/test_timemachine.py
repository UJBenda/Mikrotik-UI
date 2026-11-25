"""Cestování v čase: snímky topologie, poloha klientů v čase a přeskakování MAC mezi porty."""
import datetime as dt

from test_unmanaged import _ap, _host, _sw


def test_slim_and_signature():
    from mtcontroller import timemachine as tm

    view = {"nodes": [{"id": "a", "label": "gw", "state": "adopted", "cpu": 12, "mem": 40, "poe_w": 3.2, "parent": None,
                       "ports": [{"name": "ether1", "cls": "gbe"}], "clients": [{"mac": "x"}]}], "externals": [], "warnings": ["x"]}
    s = tm.slim(view)
    assert "cpu" not in s["nodes"][0] and "clients" not in s["nodes"][0] and s["nodes"][0]["ports"][0]["cls"] == "gbe"
    sig = tm.signature(s)
    view["nodes"][0].update(cpu=90, poe_w=9.9, clients=[])                 # vytížení a klienti snímek nemění
    assert tm.signature(tm.slim(view)) == sig
    view["nodes"][0]["ports"][0]["cls"] = "down"                           # výpadek linky ano
    assert tm.signature(tm.slim(view)) != sig


def test_time_travel_and_mac_flap(monkeypatch):
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import alerts, clients, inventory, timemachine as tm
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET role = 'gateway' WHERE id = %(d)s", {"d": sw})
        pc = "50:00:00:00:00:01"
        sw_inv = lambda port: {"interfaces": [{"name": f"ether{i}", "type": "ether", "mac-address": f"BB:00:00:00:00:0{i}", "running": "true"} for i in (1, 2, 3, 7)],  # noqa: E731
                               "bridge_hosts": [_host("CC:00:00:00:00:01", "ether7"), _host(pc, port)]}
        ap_inv = {"interfaces": [{"name": "ether1", "type": "ether", "mac-address": "CC:00:00:00:00:01", "running": "true"}],
                  "bridge_hosts": [_host("BB:00:00:00:00:07", "ether1")]}

        def cycle(port):
            inventory.store(db, tid, sw, sw_inv(port), [])
            inventory.store(db, tid, ap, ap_inv, [])
            clients.record(db)

        cycle("ether2")
        assert tm.snapshot(db, force=True) == 1 and tm.snapshot(db, force=True) == 0     # beze změny druhý snímek není
        with tenant_scope(db, tid) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM client_move")["n"] == 1
            assert tm.at(tx, sid, dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)) is None
        with operator_scope(db) as tx:
            # posunout první stav o hodinu zpět, ať je na co se vracet
            tx.execute("UPDATE infra_snapshot SET at = at - interval '60 minutes'")
            tx.execute("UPDATE client_move SET at = at - interval '60 minutes'")
            tx.execute("UPDATE client_session SET started_at = started_at - interval '60 minutes'")
            tx.execute("UPDATE device SET state = 'offline' WHERE id = %(d)s", {"d": ap})
        cycle("ether3")                                                    # klient přepojen, AP mezitím offline
        assert tm.snapshot(db, force=True) == 1
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/infra-at?at="
        from urllib.parse import quote

        now = dt.datetime.now(dt.timezone.utc)
        past = c.get(url + quote((now - dt.timedelta(minutes=30)).isoformat())).json()["infra"]
        nodes = {n["label"]: n for n in past["nodes"]}
        assert nodes["ap-sklad"]["state"] == "adopted" and nodes["ap-sklad"]["parent"] == sw
        assert [(x["mac"], x["port"]) for x in nodes["sw-hala"]["clients"]] == [(pc, "ether2")]
        cur = c.get(url + quote(now.isoformat())).json()["infra"]
        nodes = {n["label"]: n for n in cur["nodes"]}
        assert nodes["ap-sklad"]["state"] == "offline" and [(x["mac"], x["port"]) for x in nodes["sw-hala"]["clients"]] == [(pc, "ether3")]
        assert "vendor" in nodes["sw-hala"]["clients"][0] and nodes["sw-hala"]["cpu"] is None

        # přeskakování: klient střídá ether2 a ether3
        with tenant_scope(db, tid) as tx:
            assert tm.alerts(tx) == []
        for port in ("ether2", "ether3", "ether2"):
            cycle(port)
        with tenant_scope(db, tid) as tx:
            found = tm.alerts(tx)
            assert [a["kind"] for a in found] == ["mac_flap"] and found[0]["subject"] == pc
            assert "sw-hala ether2" in found[0]["message"] and "sw-hala ether3" in found[0]["message"] and "4× za hodinu" in found[0]["message"]
            # klient, který se jen jednou přestěhoval, nepřeskakuje; Wi-Fi roaming se nepočítá vůbec
            tx.execute("DELETE FROM client_move WHERE at > now() - interval '50 minutes' AND port = 'ether2'")
            assert tm.alerts(tx) == []
        alerts.reconcile(db)
    finally:
        drop_db(name)
