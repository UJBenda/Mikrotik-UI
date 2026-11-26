"""Nespravované switche (evidence, alert na nový, schválení) a cizí úseky měřené pingem."""
from test_wlink import FakeApi


def _host(mac, port):
    return {"mac-address": mac, "on-interface": port, "local": "false"}


def _eth(name, mac):
    return {"name": name, "type": "ether", "mac-address": mac}


PCS = ["50:00:00:00:00:01", "50:00:00:00:00:02", "50:00:00:00:00:03"]


def _sw():
    return {"interfaces": [_eth("ether1", "BB:00:00:00:00:01"), _eth("ether7", "BB:00:00:00:00:07")],
            "bridge_hosts": [_host("CC:00:00:00:00:01", "ether7")] + [_host(m, "ether7") for m in PCS],
            "addresses": [{"address": "10.9.0.2/24", "interface": "bridge"}]}


def _ap():
    return {"interfaces": [_eth("ether1", "CC:00:00:00:00:01")],
            "bridge_hosts": [_host("BB:00:00:00:00:07", "ether1")] + [_host(m, "ether1") for m in PCS],
            "addresses": [{"address": "10.9.0.3/24", "interface": "bridge"}]}


def test_find_foreign_and_measure():
    from mtcontroller import wlink as w

    devs = [{"id": "sw", "identity": "sw", "serial": "S1", "model": "CRS", "role": "gateway", "state": "adopted", "ros_version": "7.16"},
            {"id": "ap", "identity": "ap", "serial": "S2", "model": "cAP", "role": "ap", "state": "adopted", "ros_version": "7.16"}]
    invs = {"sw": _sw(), "ap": _ap()}
    assert w.find_foreign(devs, invs, set(), set()) == []                      # přímý kabel = žádný cizí úsek
    f = w.find_foreign(devs, invs, {("sw", "ether7")}, set())
    assert len(f) == 1 and f[0]["kind"] == "foreign" and f[0]["a"] == {"device": "sw", "iface": "ether7", "mac": "-"}
    assert f[0]["b"]["device"] == "ap" and f[0]["ping_ip"] == "10.9.0.3"
    assert w.find_foreign(devs, invs, {("sw", "ether7")}, {("sw", "ether7")}) == []   # bezdrátový spoj mezi našimi
    ln = {"kind": "foreign", "a_iface": "ether7", "b_iface": "ether1", "a_mac": "-", "b_mac": "-", "ping_ip": "10.9.0.3"}
    s = w.measure(FakeApi("-", -50, loss=2), None, ln)
    assert s["up"] and s["loss"] == 20.0 and s["rtt_avg"] == 2.4 and s["signal_a"] is None
    assert w.measure(FakeApi("-", -50, loss=10), None, ln)["up"] is False
    assert w.measure(FakeApi("-", -50, fail=True), None, ln) is None            # A nedostupné – řeší alert offline
    assert w.measure(FakeApi("-", -50), None, {**ln, "ping_ip": None}) is None
    us = {}
    assert w.state_of(w.score(s, us), w.deviations(s, us), 0) == "bad"


def test_unmanaged_record_alert_approve_and_foreign_link(monkeypatch):
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import alerts, clients, inventory, services, unmanaged, wlink as w
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
        key = lambda tx: {(r["port"], r["baseline"], r["approved"]) for r in tx.fetchall(  # noqa: E731
            "SELECT port, baseline, approved FROM unmanaged_port")}
        # první průchod: co v síti je, je výchozí stav a nehlásí se
        with tenant_scope(db, tid) as tx:
            assert unmanaged.record(tx, tid, sid, {}, have_clients=False) == 0
            assert unmanaged.record(tx, tid, sid, {(sw, "ether2"): {"clients": 4, "shared": False}}) == 1
            assert key(tx) == {("ether2", True, False)}
        with operator_scope(db) as tx:
            # po náběhu lokality už se nové nálezy hlásí
            tx.execute("UPDATE site SET unmanaged_scanned_at = now() - interval '2 days'")
            tx.execute("UPDATE device SET role = 'gateway' WHERE id = %(d)s", {"d": sw})
        inventory.store(db, tid, sw, _sw(), [])
        inventory.store(db, tid, ap, _ap(), [])
        clients.record(db)
        with tenant_scope(db, tid) as tx:
            assert ("ether7", False, False) in key(tx)
            assert unmanaged.alerts(tx) == []                                   # ještě nevydržel 10 minut
            tx.execute("UPDATE unmanaged_port SET first_seen = now() - interval '20 minutes' WHERE port = 'ether7'")
            found = unmanaged.alerts(tx)
            assert len(found) == 1 and found[0]["kind"] == "unmanaged_switch" and found[0]["subject"] == "ether7"
            assert str(found[0]["device_id"]) == sw and "za portem: 3 klienti a spravované zařízení" in found[0]["message"]
        alerts.reconcile(db)
        c = _login(app)
        rows = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/unmanaged").json()["ports"]
        assert [(r["port"], r["state"]) for r in rows] == [("ether7", "new"), ("ether2", "known")]
        uid = rows[0]["id"]
        r = c.put(f"/api/admin/tenants/{tid}/sites/{sid}/unmanaged/{uid}", json_body={"approved": True, "note": "D-Link ve skladu"})
        assert r.status_code == 200 and r.json()["ports"][-1]["state"] == "approved" and r.json()["ports"][-1]["note"] == "D-Link ve skladu"
        assert c.put(f"/api/admin/tenants/{tid}/sites/{sid}/unmanaged/00000000-0000-0000-0000-000000000001",
                     json_body={"approved": True}).status_code == 404
        with tenant_scope(db, tid) as tx:
            assert unmanaged.alerts(tx) == []
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'site.unmanaged_approve'")["n"] == 1
            assert tx.fetchone("SELECT count(*) AS n FROM alert WHERE kind = 'unmanaged_switch'")["n"] == 1
            # neschválený nález, který zmizel, se zapomene; schválený zůstává
            tx.execute("UPDATE unmanaged_port SET last_seen = now() - interval '2 days'")
        with tenant_scope(db, tid) as tx:
            unmanaged.record(tx, tid, sid, {})
            assert key(tx) == {("ether7", False, True)}
            tx.execute("UPDATE unmanaged_port SET last_seen = now()")

        # úsek přes nespravované zařízení se sám nezakládá – hlídat ho musí správce ručně
        state = {"loss": 0, "fail": False}
        monkeypatch.setattr(services, "device_api", lambda tx, b, dev, timeout=10.0: FakeApi("-", -50, loss=state["loss"]))
        assert w.sample(db, box, force=True) == 0
        url = f"/api/admin/tenants/{tid}/sites/{sid}/links"
        assert c.post(url, json_body={"a_device": sw, "a_iface": "ether7", "b_device": sw}).status_code == 400
        r = c.post(url, json_body={"a_device": sw, "a_iface": "ether7", "b_device": ap})
        assert r.status_code == 200 and [x["kind"] for x in r.json()["links"]] == ["foreign"]
        assert len(c.post(url, json_body={"a_device": sw, "a_iface": "ether7", "b_device": ap}).json()["links"]) == 1
        assert w.sample(db, box, force=True) == 1
        with tenant_scope(db, tid) as tx:
            v = w.site_links(tx, sid)
            assert len(v) == 1 and v[0]["kind"] == "foreign" and v[0]["a"]["iface"] == "ether7" and v[0]["state"] == "ok"
            assert v[0]["ping_ip"] == "10.9.0.3" and w.alerts(tx) == []
        state["loss"] = 10
        assert w.sample(db, box, force=True) == 1
        with tenant_scope(db, tid) as tx:
            found = w.alerts(tx)
            assert [a["kind"] for a in found] == ["link_down"] and "úsek přes nespravované zařízení sw-hala ↔ ap-sklad" in found[0]["message"]
        # dva úseky dole ke stejné protistraně = jedna příčina, jeden alert u protistrany
        with tenant_scope(db, tid) as tx:
            l2 = w.watch(tx, tid, sid, sw, "ether8", ap)
        assert w.sample(db, box, force=True) == 2
        with tenant_scope(db, tid) as tx:
            found = w.alerts(tx)
            assert len(found) == 1 and found[0]["kind"] == "link_down" and str(found[0]["device_id"]) == ap
            assert "ap-sklad (10.9.0.3) neodpovídá na ping na 2 hlídaných úsecích (z sw-hala)" in found[0]["message"]
            assert w.unwatch(tx, tid, l2)
        # mapa: uzel nespravovaného switche zůstává (není to bezdrátový spoj mezi našimi)
        tree = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/infra").json()
        assert any(n.get("kind") == "hub" and n.get("guess") for n in tree["nodes"])
        lid = c.get(url).json()["links"][0]["id"]
        assert c.delete(f"/api/admin/tenants/{tid}/links/{lid}").status_code == 200
        assert c.get(url).json()["links"] == [] and c.delete(f"/api/admin/tenants/{tid}/links/{lid}").status_code == 404
        with operator_scope(db) as tx:
            assert {r["action"] for r in tx.fetchall("SELECT action FROM audit_log WHERE action LIKE 'wlink.%%'")} == {
                "wlink.watch", "wlink.unwatch"}
    finally:
        drop_db(name)
