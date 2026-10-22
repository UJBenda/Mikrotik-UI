"""Nástroje na portu (ping, traceroute, WoL, test kabelu, blikání), restart portu, hromadné úpravy,
LACP/MLAG přes API, PoE hlídač z profilu a detekce podvržených AP."""

import datetime as dt

import pytest
from helpers import drop_db
from test_flow import _login
from test_modules import _adopted, _env
from test_portcfg import INV
from test_routeros import _balanced

from mtcontroller import alerts, inventory, porttools, rogue, services, txpower
from mtcontroller.db import operator_scope


class Api:
    def __init__(self, log, answers=None, fail=()):
        self.log, self.answers, self.fail, self.timeout = log, answers or {}, set(fail), 10

    def post(self, path, body):
        self.log.append((path, dict(body)))
        key = (path, tuple(sorted(body)))
        if path in self.fail or key in self.fail:
            raise RuntimeError("unknown parameter")
        return self.answers.get(path, [])

    def close(self):
        pass


SW_INV = dict(INV, poe=[{"name": "ether3", "poe-out": "auto-on", "power-cycle-ping-enabled": "false",
                         "power-cycle-ping-timeout": "5s"}],
              neighbors=[{"interface": "ether3", "identity": "AP-1", "address": "192.168.88.31", "platform": "MikroTik",
                          "mac-address": "AA:00:00:00:00:31"}],
              bridge_hosts=[{"mac-address": "AA:BB:CC:00:00:01", "on-interface": "ether2", "bridge": "bridge"}])


def _setup(fail=()):
    db, name, settings, box, app, tid, sid = _env()
    sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
    inventory.store(db, tid, sw, SW_INV, [])
    log = []
    api = Api(log, {
        "/ping": [{"seq": "0", "host": "1.1.1.1", "time": "12ms", "ttl": "57"}, {"seq": "1", "host": "1.1.1.1", "status": "timeout"},
                  {"seq": "2", "host": "1.1.1.1", "time": "10ms500us", "sent": "3", "received": "2", "packet-loss": "33",
                   "avg-rtt": "11ms250us", "min-rtt": "10ms500us", "max-rtt": "12ms"}],
        "/tool/traceroute": [{"address": "10.0.0.1", "loss": "0%", "last": "1ms", "avg": "1ms", "status": ""},
                             {"address": "1.1.1.1", "loss": "0%", "last": "11ms", "avg": "11ms", "status": ""}],
        "/interface/ethernet/cable-test": [{"name": "ether2", "status": "no-link", "cable-pairs": "open:4,open:4,short:3,normal:30"}],
    }, fail)
    orig = porttools.device_api
    porttools.device_api = lambda tx, box, d, timeout=10.0: api
    return db, name, settings, box, app, tid, sid, sw, log, orig


def test_live_tools_and_audit():
    db, name, settings, box, app, tid, sid, sw, log, orig = _setup(fail={("/interface/ethernet/cable-test", ("duration", "numbers"))})
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}/tools"
        r = c.post(base + "/ping", json_body={"address": "1.1.1.1", "count": 3}).json()
        assert r["sent"] == 3 and r["received"] == 2 and r["loss_pct"] == 33 and r["avg_ms"] == 11.25
        assert [x["time_ms"] for x in r["replies"]] == [12.0, None, 10.5]
        assert log[-1] == ("/ping", {"address": "1.1.1.1", "count": "3"})
        assert c.post(base + "/ping", json_body={"address": "1.1.1.1; /system reset"}).status_code == 400
        assert c.post(base + "/ping", json_body={"address": "x.cz", "count": 99}).status_code == 400
        t = c.post(base + "/traceroute", json_body={"address": "1.1.1.1"}).json()
        assert [h["address"] for h in t["hops"]] == ["10.0.0.1", "1.1.1.1"] and t["hops"][1]["avg_ms"] == 11.0
        # Wake-on-LAN: rozhraní = bridge, kde je MAC vidět
        assert c.post(base + "/wol", json_body={"mac": "aa-bb-cc-00-00-01"}).json() == {"tool": "wol", "ok": True}
        assert log[-1] == ("/tool/wol", {"mac": "AA:BB:CC:00:00:01", "interface": "bridge"})
        # test kabelu: novější syntaxe selže → záložní "once"
        cb = c.post(base + "/cable_test", json_body={"port": "ether2"}).json()
        assert cb["status"] == "no-link" and cb["pairs"][2] == {"status": "short", "length_m": 3}
        assert log[-1] == ("/interface/ethernet/cable-test", {"numbers": "ether2", "once": ""})
        assert c.post(base + "/cable_test", json_body={"port": "ether9"}).status_code == 400
        assert c.post(base + "/blink", json_body={"port": "ether2", "seconds": 10}).status_code == 200
        assert log[-1] == ("/interface/ethernet/blink", {"numbers": "ether2", "duration": "10s"})
        assert c.post(base + "/nmap", json_body={}).status_code == 400
        with operator_scope(db) as tx:
            acts = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log WHERE action LIKE 'device.tool.%'")]
        assert {"device.tool.ping", "device.tool.wol", "device.tool.cable_test", "device.tool.blink"} <= set(acts)
    finally:
        porttools.device_api = orig
        drop_db(name)


def test_port_bounce_command():
    db, name, settings, box, app, tid, sid, sw, log, orig = _setup()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}"
        assert c.post(base + "/commands", json_body={"kind": "port_bounce", "port": "ether9"}).status_code == 400
        r = c.post(base + "/commands", json_body={"kind": "port_bounce", "port": "ether2"})
        assert r.status_code == 201, r.text
        with operator_scope(db) as tx:
            cmd = tx.fetchone("SELECT * FROM device_command WHERE kind = 'port_bounce'")
            dev = tx.fetchone("SELECT id, serial FROM device WHERE id = %(d)s", {"d": sw})
            script = services.render_command(tx, settings, box, cmd, dev)
        assert '/interface ethernet disable [find name="ether2"]' in script and ":delay 5s" in script
        assert script.index("disable") < script.index("enable [find") and _balanced(script) and script.isascii()
    finally:
        porttools.device_api = orig
        drop_db(name)


def test_bulk_profile_with_watchdog_and_site_ports():
    db, name, settings, box, app, tid, sid, sw, log, orig = _setup()
    try:
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        assert c.post(f"{T}/devices/{sw}/modules", json_body={"module": "ports", "mode": "manage"}).status_code == 200
        pid = c.post(T + "/port-profiles", json_body={"name": "AP", "pvid": 20, "tagged": ["40", "50", "60"], "poe": "auto-on",
                                                      "poe_watchdog": True, "poe_watchdog_timeout": 120}).json()["id"]
        # úprava profilu
        r = c.put(f"{T}/port-profiles/{pid}", json_body={"name": "AP trunk", "pvid": 20, "tagged": "40,50,60", "poe": "auto-on",
                                                         "poe_watchdog": True, "poe_watchdog_timeout": 90})
        assert r.status_code == 200 and r.json()["name"] == "AP trunk" and r.json()["poe_watchdog_timeout"] == 90
        st = c.get(f"{T}/devices/{sw}/ports/ether3").json()
        assert st["watchdog_suggest"] == "192.168.88.31" and st["poe_watchdog"]["enabled"] is False
        r = c.post(T + "/ports/bulk", json_body={"targets": [{"device_id": sw, "ports": ["ether2", "ether3"]}], "profile_id": pid})
        assert r.status_code == 201, r.text
        res = r.json()["results"][0]
        assert "error" not in res, res
        with operator_scope(db) as tx:
            plan = tx.fetchone("SELECT script, revert_script, changes FROM config_plan WHERE id = %(i)s", {"i": res["plan_id"]})
        sc = plan["script"]
        # PoE a hlídač jen na portu s PoE; hlídač míří na IP souseda (AP)
        assert 'power-cycle-ping-address="192.168.88.31" power-cycle-ping-timeout=90s' in sc
        assert sc.count("power-cycle-ping-enabled=yes") == 1 and "pvid=20" in sc
        assert 'vlan add bridge="bridge" vlan-ids=50 tagged="ether2"' in sc and 'vlan-ids=50 dynamic=no] tagged="ether2,ether3"' in sc
        assert _balanced(sc) and _balanced(plan["revert_script"])
        # s apply se rovnou schválí
        r = c.post(T + "/ports/bulk", json_body={"targets": [{"device_id": sw, "ports": ["ether4"]}],
                                                 "desired": {"disabled": True}, "apply": True})
        assert r.json()["results"][0]["command_id"]
        bad = c.post(T + "/ports/bulk", json_body={"targets": [{"device_id": sw, "ports": ["ether4"]}], "desired": {"comment": "x"}})
        assert bad.status_code == 400
        sp = c.get(f"{T}/sites/{sid}/ports").json()
        d = sp["devices"][0]
        assert d["manage"] and [p["port"] for p in d["ports"]][:3] == ["ether1", "ether2", "ether3"]
        assert next(p for p in d["ports"] if p["port"] == "ether3")["neighbor"] == "AP-1"
    finally:
        porttools.device_api = orig
        drop_db(name)


def test_lacp_and_mlag_api():
    db, name, settings, box, app, tid, sid, sw, log, orig = _setup()
    try:
        sw2 = _adopted(db, box, tid, sid, "SW2", "10.200.1.3", "sw2")
        inv2 = dict(SW_INV, neighbors=[{"interface": "ether1", "identity": "sw"}])
        inventory.store(db, tid, sw2, inv2, [])
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET ros_version = '7.22.1 (stable)'")
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        r = c.post(f"{T}/devices/{sw}/bonds", json_body={"kind": "lacp", "ports": ["ether2", "ether4"]})
        assert r.status_code == 201, r.text
        assert "LACP bond-mtc1" in r.json()["summary"]
        assert c.post(f"{T}/devices/{sw}/bonds", json_body={"kind": "lacp", "ports": ["ether1", "ether2"]}).status_code == 400
        body = {"a": {"device_id": sw, "peer_port": "ether1", "members": [{"ports": ["ether2"], "mlag_id": 10}]},
                "b": {"device_id": sw2, "peer_port": "ether1", "members": [{"ports": ["ether2"], "mlag_id": 10}]}}
        r = c.post(T + "/mlag", json_body=body)
        assert r.status_code == 201, r.text
        pl = r.json()["plans"]
        assert len(pl) == 2 and "primární" in pl[0]["summary"] and "sekundární" in pl[1]["summary"]
        assert any("druhý switch" in w for w in pl[0]["warnings"])
        body["b"]["members"][0]["mlag_id"] = 11
        assert c.post(T + "/mlag", json_body=body).status_code == 400
    finally:
        porttools.device_api = orig
        drop_db(name)


def test_rogue_scan_classify_alert_ack():
    db, name, settings, box, app, tid, sid, sw, log, orig = _setup()
    try:
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.9", "ap1")
        inventory.store(db, tid, ap, {"wifi": [{"name": "wifi1", "mac-address": "48:A9:8A:10:20:30", "configuration.ssid": "Firma"},
                                                {"name": "wifi2", "master-interface": "wifi1", "mac-address": "4A:A9:8A:10:20:31"}],
                                       "interfaces": [{"name": "wifi1", "type": "wifi", "mac-address": "48:A9:8A:10:20:30"}]}, [])
        rows = [{"address": "4A:A9:8A:10:20:30", "ssid": "Firma", "channel": "5180/ax/Ceee", "signal": "-40"},
                {"address": "00:11:22:33:44:55", "ssid": "Firma", "channel": "2412/n", "sig": "-71"},
                {"address": "00:11:22:33:44:66", "ssid": "Soused", "channel": "2437/n", "signal": "-80"},
                {"address": "00:11:22:33:44:66", "ssid": "Soused", "channel": "2437/n", "signal": "-60"},
                {"address": "nesmysl", "ssid": "x"}]
        assert len(rogue.parse_scan(rows)) == 3
        api = Api(log, {"/interface/wifi/scan": rows})
        rogue.device_api = lambda tx, box, d, timeout=10.0: api
        c = _login(app)
        r = c.post(f"/api/admin/tenants/{tid}/sites/{sid}/wifi/rogue", json_body={"device_id": ap})
        assert r.status_code == 201, r.text
        assert r.json() | {} == {**r.json(), "found": 3, "own": 1, "rogue": 1, "neighbor": 1}
        assert log[-1] == ("/interface/wifi/scan", {"numbers": "wifi1", "duration": str(rogue.SCAN_SECONDS)})
        bss = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/wifi/rogue").json()["bss"]
        assert bss[0]["kind"] == "rogue" and bss[0]["bssid"] == "00:11:22:33:44:55" and bss[0]["seen_by_name"] == "ap1"
        assert next(b for b in bss if b["kind"] == "neighbor")["signal"] == -60
        with operator_scope(db) as tx:
            act = [a for a in alerts.conditions(tx) if a.kind == "rogue_ap"]
        assert len(act) == 1 and "Firma" in act[0].message
        r = c.post(f"/api/admin/tenants/{tid}/sites/{sid}/wifi/rogue", json_body={"bssid": "00:11:22:33:44:55", "ack": True})
        assert r.status_code == 200
        with operator_scope(db) as tx:
            assert not [a for a in alerts.conditions(tx) if a.kind == "rogue_ap"]
        # noční sken jen v okně 2–5 h a jen s politikou
        assert rogue.nightly(db, box, dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc), "UTC") == 0
    finally:
        rogue.device_api = orig
        porttools.device_api = orig
        drop_db(name)


def test_txpower_plan():
    r = [{"device_id": d, "interface": i, "band": b, "signals": s} for d, i, b, s in [
        ("a", "wifi1", "5ghz", [-45, -48, -40]), ("a", "wifi2", "2ghz", []),
        ("b", "wifi1", "5ghz", [-75, -78, -80]), ("b", "wifi2", "2ghz", []),
        ("c", "wifi1", "5ghz", []), ("c", "wifi2", "2ghz", [])]]
    p = txpower.plan(r)
    assert p[("a", "wifi1")]["tx_power"] == 17 and "blízko" in p[("a", "wifi1")]["reason"]
    assert p[("b", "wifi1")]["tx_power"] == 23 and p[("c", "wifi1")]["tx_power"] == 20
    assert p[("c", "wifi2")]["tx_power"] == 14  # 2,4 GHz o 6 dB níž
    assert all(v["tx_power"] is None for v in txpower.plan(r, "auto").values())
    single = txpower.plan([{"device_id": "a", "interface": "wifi1", "band": "5ghz"}])
    assert single[("a", "wifi1")]["tx_power"] is None
    assert txpower.signals({"wifi_clients": [{"interface": "wifi1", "signal": "-61dBm"}, {"interface": "wifi1", "signal": "x"}]}) == {"wifi1": [-61]}
