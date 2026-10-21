"""Logy ze zařízení (syslog přes tunel) a test rychlosti mezi zařízeními."""

import copy
import re

import pytest

from test_routeros import _balanced

from mtcontroller import bwtest, devlog


def test_config_body():
    off = devlog.config_body("off", "10.200.0.1", 5514, "10.200.1.2")
    assert 'remove [find action="mtc"]' in off and "action add" not in off
    s = devlog.config_body("errors", "10.200.0.1", 5514, "10.200.1.2")
    assert ('/system logging action add name="mtc" target=remote remote=10.200.0.1 remote-port=5514 '
            'src-address=10.200.1.2') in s
    assert s.count("/system logging add") == 3 and "info" not in s
    s = devlog.config_body("info", "10.200.0.1", 5514, None)
    assert 'topics="info,!debug"' in s and "src-address" not in s
    with pytest.raises(ValueError):
        devlog.config_body("vse", "10.200.0.1", 5514, None)


def test_parse():
    assert devlog.parse(b"<30>system,info,account user admin logged in from 10.0.0.5 via winbox") == (
        6, "system,info,account", "user admin logged in from 10.0.0.5 via winbox")
    assert devlog.parse(b"<28>Oct 19 20:53:00 SW-core interface,warning ether3 link down") == (4, "interface,warning", "ether3 link down")
    sev, topics, msg = devlog.parse(b"<27>bez temat: zprava\x00\x1b[31m" + b"x" * 2000)
    assert sev == 3 and topics == "" and "\x00" not in msg and len(msg) == 1000
    assert devlog.parse("<14>dhcp,info přiřazeno".encode()) == (6, "dhcp,info", "přiřazeno")


class _S:  # nastavení pro Collector bez DB
    class syslog:
        max_lines_per_minute = 3
        forward = ""


def test_collector_rate_and_source():
    col = devlog.Collector(None, _S)
    col.devices = {"10.200.1.2": {"id": "d1", "tenant": "t1", "name": "SW"}}
    assert col.receive(b"<30>system,info x", "10.9.9.9", now=60.0) is False  # cizí zdroj
    assert [col.receive(b"<30>system,info x", "10.200.1.2", now=61.0 + i) for i in range(5)] == [True, True, True, False, False]
    assert col.receive(b"<30>system,info y", "10.200.1.2", now=125.0) is True   # nová minuta
    st = col.stats[("t1", "d1", "1970-01-01")]
    assert st[0] == 4 and st[2] == 2 and len(col.buf) == 4


def test_flow_level_logs_and_volume():
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_modules import _adopted, _env

    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "SW")
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        assert c.post(T + f"/devices/{sw}/syslog", json_body={"level": "vse"}).status_code == 400
        assert c.post(T + f"/devices/{sw}/syslog", json_body={"level": "errors"}).status_code == 200
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert "remote=10.200.0.1 remote-port=5514 src-address=10.200.1.2" in s
        s.encode("ascii")
        # příjem: jen od zařízení se zapnutými logy, zápis po tenantech, objem po dnech
        col = devlog.Collector(db, settings)
        col.refresh(force=True)
        assert "10.200.1.2" in col.devices
        assert col.receive(b"<28>interface,warning ether3 link down", "10.200.1.2")
        assert col.receive(b"<27>system,error,critical login failure for user admin", "10.200.1.2")
        assert col.flush() == 2
        v = c.get(T + f"/devices/{sw}/logs").json()
        assert v["level"] == "errors" and [x["topics"] for x in v["logs"]] == ["system,error,critical", "interface,warning"]
        assert v["volume"]["days"][0]["lines"] == 2 and v["volume"]["avg_bytes_per_day"] > 100
        assert [x["message"] for x in c.get(T + f"/devices/{sw}/logs?q=ether3").json()["logs"]] == ["ether3 link down"]
        assert len(c.get(T + f"/devices/{sw}/logs?sev=3").json()["logs"]) == 1
        # celá lokalita najednou; vypnutí
        assert c.post(T + f"/sites/{sid}/syslog", json_body={"level": "off"}).json()["devices"] == 1
        col.refresh(force=True)
        assert col.devices == {}
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'device.syslog'")["n"] == 2
        assert devlog.retention(db, 1) == 0
    finally:
        drop_db(name)


# --- test rychlosti ---------------------------------------------------------------------------

ROWS = [{"status": "running", "tx-current": "90.5Mbps", "rx-current": "40000000", "local-cpu-load": "35%", "remote-cpu-load": "60%"},
        {"status": "running", "tx-current": "94.1Mbps", "rx-current": "41.2Mbps", "local-cpu-load": "40%", "remote-cpu-load": "71%",
         "tx-total-average": "92.3Mbps", "rx-total-average": "40.6Mbps", "lost-packets": "0"}]


def test_summarize_and_targets():
    assert bwtest.parse_bps("94.2Mbps") == 94.2e6 and bwtest.parse_bps("512kbps") == 512e3 and bwtest.parse_bps("x") is None
    r = bwtest.summarize(ROWS)
    assert r["tx_avg"] == 92.3e6 and r["rx_max"] == 41.2e6 and r["cpu_remote"] == 71 and len(r["samples"]) == 2
    inv = {"addresses": [{"address": "192.168.77.5/24", "interface": "bridge"}, {"address": "10.200.1.2/32", "interface": "mtc-sstp"},
                         {"address": "169.254.1.1/16", "interface": "x"}, {"address": "10.0.0.1/24", "interface": "y", "disabled": "true"}]}
    assert bwtest.target_addresses(inv) == [{"address": "192.168.77.5", "interface": "bridge"}]


class FakeApi:
    def __init__(self, name, log, fail=None):
        self.name, self.log, self.fail, self.timeout = name, log, fail, 10

    def get(self, path, **_):
        self.log.append((self.name, "GET", path))
        if path == "/tool/bandwidth-server":
            return {"enabled": "false", "authenticate": "true"}
        return [{"name": "admin"}]

    def put(self, path, body):
        self.log.append((self.name, "PUT", path, body.get("name"), body.get("address")))

    def post(self, path, body):
        self.log.append((self.name, "POST", path, dict(body)))
        if path == "/ip/route/check":
            return [{"src": "192.168.77.2"}]
        if path == "/tool/bandwidth-test":
            if self.fail:
                raise RuntimeError(self.fail)
            return copy.deepcopy(ROWS)
        return []

    def close(self):
        pass


def _bw_env(fail=None):
    from test_modules import _adopted, _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    a = _adopted(db, box, tid, sid, "A1", "10.200.1.2", "SW-a")
    b = _adopted(db, box, tid, sid, "B1", "10.200.1.3", "SW-b")
    inventory.store(db, tid, b, {"addresses": [{"address": "192.168.77.5/24", "interface": "bridge"}]}, [])
    log = []
    apis = {"10.200.1.2": FakeApi("A", log, fail), "10.200.1.3": FakeApi("B", log)}
    orig = bwtest.device_api
    bwtest.device_api = lambda tx, box, d, timeout=10.0: apis[d["tunnel_ip"]]
    return db, name, settings, box, app, tid, sid, a, b, log, orig, operator_scope


def test_bwtest_runs_and_cleans_up():
    from helpers import drop_db
    from test_flow import _login

    db, name, settings, box, app, tid, sid, a, b, log, orig, operator_scope = _bw_env()
    try:
        with pytest.raises(Exception):
            bwtest.start(db, box, tid, a, b, "10.9.9.9", run_async=False)   # adresa nepatří cíli
        out = bwtest.start(db, box, tid, a, b, "192.168.77.5", 10, "both", "tcp", run_async=False)
        test_call = next(x for x in log if x[2] == "/tool/bandwidth-test")
        assert test_call[0] == "A" and test_call[3]["address"] == "192.168.77.5" and test_call[3]["duration"] == "10s"
        pw = test_call[3]["password"]
        # dočasný uživatel jen ze zdrojové adresy, server zapnutý jen na dobu testu a vrácený
        assert ("B", "PUT", "/user", "mtc-btest", "192.168.77.2/32") in log
        b_posts = [x for x in log if x[0] == "B" and x[1] == "POST"]
        assert b_posts[-2] == ("B", "POST", "/user/remove", {"numbers": "mtc-btest"})
        assert b_posts[-1] == ("B", "POST", "/tool/bandwidth-server/set", {"enabled": "no", "authenticate": "yes"})
        c = _login(app)
        v = c.get(f"/api/admin/tenants/{tid}/devices/{a}/bwtest").json()
        t = v["tests"][0]
        assert t["id"] == out["id"] and t["status"] == "done" and t["result"]["tx_avg"] == 92.3e6
        assert pw not in str(v) and v["targets"][0]["addresses"] == [{"address": "192.168.77.5", "interface": "bridge"}]
        with operator_scope(db) as tx:
            assert pw not in str(tx.fetchall("SELECT after FROM audit_log WHERE action = 'device.bwtest'"))
        r = c.post(f"/api/admin/tenants/{tid}/devices/{a}/bwtest", json_body={"to": a, "address": "192.168.77.5"})
        assert r.status_code == 400
    finally:
        bwtest.device_api = orig
        drop_db(name)


def test_bwtest_failure_still_cleans_up():
    from helpers import drop_db

    db, name, settings, box, app, tid, sid, a, b, log, orig, operator_scope = _bw_env(fail="connection refused")
    try:
        bwtest.start(db, box, tid, a, b, "192.168.77.5", run_async=False)
        assert ("B", "POST", "/user/remove", {"numbers": "mtc-btest"}) in log
        with operator_scope(db) as tx:
            r = tx.fetchone("SELECT status, error FROM bw_test")
        assert r["status"] == "failed" and "firewall" in r["error"]
    finally:
        bwtest.device_api = orig
        drop_db(name)


def test_render_local_mode_targets_controller(tmp_path):
    """Lokální režim: syslog rovnou na adresu controlleru (bez koncentrátoru)."""
    from helpers import make_settings

    cmd = {"params": {"level": "errors"}}
    loc = devlog.render(make_settings(tmp_path, local=True), cmd, {"tunnel_ip": "192.168.88.2"})
    assert "remote=192.168.88.10 " in loc and "src-address=192.168.88.2" in loc
    tun = devlog.render(make_settings(tmp_path), cmd, {"tunnel_ip": "10.200.1.2"})
    assert "remote=10.200.0.1 " in tun
