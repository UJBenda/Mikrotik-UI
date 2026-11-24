"""Bezdrátové spoje: nalezení z registrací, měření (registrace obou stran + ping), hodnocení, alerty, API."""


def _ap():
    return {"interfaces": [{"name": "wlan1", "type": "wlan", "mac-address": "AA:AA:AA:00:00:01"}],
            "wireless": [{"name": "wlan1", "mac-address": "AA:AA:AA:00:00:01", "mode": "bridge"}],
            "wireless_clients": [{"interface": "wlan1", "mac-address": "BB:BB:BB:00:00:02", "signal-strength": "-58dBm@6Mbps"},
                                 {"interface": "wlan1", "mac-address": "CC:CC:CC:00:00:09", "signal-strength": "-70"}],
            "addresses": [{"address": "10.9.0.20/24", "interface": "bridge"}, {"address": "10.200.1.2/32", "interface": "mtc-sstp"}]}


def _sta():
    return {"interfaces": [{"name": "wlan1", "type": "wlan", "mac-address": "BB:BB:BB:00:00:02"}],
            "wireless": [{"name": "wlan1", "mac-address": "BB:BB:BB:00:00:02", "mode": "station-bridge"}],
            "wireless_clients": [{"interface": "wlan1", "mac-address": "AA:AA:AA:00:00:01", "signal-strength": "-60"}],
            "addresses": [{"address": "10.9.0.21/24", "interface": "bridge"}, {"address": "10.200.1.3/32", "interface": "mtc-sstp"}]}


class FakeApi:
    """REST strana spoje: registrace s daným signálem, monitor se šumem, ping se ztrátou."""

    def __init__(self, peer, signal, rate="130Mbps-20MHz/2S", loss=0, up=True, uptime="11h42m", fail=False):
        self.peer, self.signal, self.rate, self.loss, self.up, self.uptime, self.fail = peer, signal, rate, loss, up, uptime, fail
        self.calls = []

    def request(self, method, path, body=None, params=None):
        from mtcontroller.rosapi import RosApiError

        self.calls.append(path)
        if self.fail:
            raise RosApiError(None, "timeout")
        if path.endswith("registration-table"):
            return [{"interface": "wlan1", "mac-address": self.peer, "signal-strength": f"{self.signal}dBm@6Mbps",
                     "signal-to-noise": "40", "tx-ccq": "92", "tx-rate": self.rate, "rx-rate": self.rate,
                     "uptime": self.uptime}] if self.up else []
        if path.endswith("/monitor"):
            return [{"noise-floor": "-104", "status": "running-ap"}]
        if path == "/ping":
            ok = 10 - self.loss
            return [{"seq": str(i), "time": "2ms100us"} for i in range(ok)] + [
                {"sent": "10", "received": str(ok), "packet-loss": str(self.loss * 10), "avg-rtt": "2ms400us",
                 "min-rtt": "1ms900us", "max-rtt": "4ms"}]
        raise AssertionError(path)


def test_parsing_and_find_links():
    from mtcontroller import wlink as w

    assert w.parse_rate("433300000") == 433300000 and w.parse_rate("130Mbps-20MHz/2S") == 130000000
    assert w.parse_rate("433.3Mbps") == 433300000 and w.parse_rate("") is None
    assert w.parse_duration("11h42m") == 42120 and w.parse_duration("1d00:00:05") == 86405 and w.parse_duration("00:11:42") == 702
    s = w.side({"signal-strength": "-58dBm@6Mbps", "signal-to-noise": "40", "tx-ccq": "92", "tx-rate": "130Mbps-20MHz/2S"})
    assert s["signal"] == -58 and s["snr"] == 40 and s["ccq"] == 92 and s["rate_tx"] == 130000000
    assert w.side({"signal": "-61", "tx-rate": "866000000"})["signal"] == -61 and w.side(None) == {}

    links = w.find_links([{"id": "a"}, {"id": "b"}], {"a": _ap(), "b": _sta()})
    assert len(links) == 1                                     # obě strany se vidí = jeden spoj, cizí klient není spoj
    ln = links[0]
    assert ln["a"]["device"] == "a" and ln["b"]["device"] == "b" and ln["kind"] == "wireless"   # A = AP/bridge
    assert ln["b"]["mac"] == "BB:BB:BB:00:00:02" and ln["ping_ip"] == "10.9.0.21"               # ne tunelová adresa
    # pořadí zařízení nerozhoduje
    assert w.find_links([{"id": "b"}, {"id": "a"}], {"a": _ap(), "b": _sta()})[0]["a"]["device"] == "a"


def test_w60g_link():
    """60 GHz: protistrana a hodnoty jsou v monitoru rozhraní, ne v registrační tabulce."""
    from mtcontroller import wlink as w
    from mtcontroller.rosapi import RosApiError

    def inv(mac, peer, mode, ip):
        return {"w60g": [{"name": "wlan60-1", "mac-address": mac, "mode": mode}],
                "w60g_monitor": [{"name": "wlan60-1", "connected": "true", "remote-address": peer, "rssi": "-52"}],
                "addresses": [{"address": ip + "/24", "interface": "bridge"}]}

    A, B = "C4:AD:34:00:00:0A", "C4:AD:34:00:00:0B"
    links = w.find_links([{"id": "st"}, {"id": "ap"}], {"ap": inv(A, B, "bridge", "10.5.0.1"), "st": inv(B, A, "station-bridge", "10.5.0.2")})
    assert len(links) == 1 and links[0]["kind"] == "w60g" and links[0]["a"]["device"] == "ap" and links[0]["ping_ip"] == "10.5.0.2"

    class Api:
        def __init__(self, peer, rssi, per, connected="true"):
            self.m = {"connected": connected, "remote-address": peer, "rssi": str(rssi), "tx-mcs": "8", "tx-phy-rate": "2310000000",
                      "tx-packet-error-rate": str(per), "distance": "220.3m"}

        def request(self, method, path, body=None, params=None):
            if path == "/interface/w60g/monitor":
                return [self.m]
            if path == "/ping":
                return [{"sent": "10", "received": "10", "avg-rtt": "400us", "min-rtt": "300us", "max-rtt": "1ms"}]
            raise RosApiError(404, "no such command")

    link = {"kind": "w60g", "a_iface": "wlan60-1", "a_mac": A, "b_iface": "wlan60-1", "b_mac": B, "ping_ip": "10.5.0.2"}
    s = w.measure(Api(B, -52, 3), Api(A, -54, 7), link)
    assert s["up"] and s["signal_a"] == -52 and s["signal_b"] == -54 and s["rate_ab"] == 2310000000 and s["rate_ba"] == 2310000000
    assert s["ccq_a"] == 97 and s["ccq_b"] == 93 and s["loss"] == 0.0 and s["rtt_avg"] == 0.4 and s["noise"] is None
    assert w.measure(Api(B, -52, 3, connected="false"), None, link)["up"] is False
    assert w.measure(Api("00:00:00:00:00:01", -52, 3), None, link)["up"] is False      # připojený někdo jiný


def test_measure_and_evaluate():
    from mtcontroller import wlink as w

    link = {"kind": "wireless", "a_iface": "wlan1", "a_mac": "AA:AA:AA:00:00:01", "b_iface": "wlan1",
            "b_mac": "BB:BB:BB:00:00:02", "ping_ip": "10.9.0.21"}
    a, b = FakeApi("BB:BB:BB:00:00:02", -58, loss=1), FakeApi("AA:AA:AA:00:00:01", -60)
    s = w.measure(a, b, link)
    assert s["up"] and s["signal_a"] == -58 and s["signal_b"] == -60 and s["rate_ab"] == 130000000 and s["ccq_a"] == 92
    assert s["loss"] == 10.0 and s["rtt_avg"] == 2.4 and s["rtt_max"] == 4 and s["noise"] == -104 and s["uptime_s"] == 42120
    assert "/ping" in a.calls and "/ping" not in b.calls
    # protistrana nedostupná (je za spojem) – měření z A stačí; A nedostupná = žádné měření
    assert w.measure(a, FakeApi("x", -60, fail=True), link)["signal_b"] is None
    assert w.measure(FakeApi("x", -58, fail=True), b, link) is None
    down = w.measure(FakeApi("BB:BB:BB:00:00:02", -58, up=False), b, link)
    assert down["up"] is False and down["loss"] is None

    us = {"n": 100, "signal_a": -58.0, "signal_b": -60.0, "rate_ab": 130e6, "rate_ba": 130e6, "noise": -104.0}
    ok = {"up": True, "signal_a": -59, "signal_b": -60, "rate_ab": 130e6, "rate_ba": 130e6, "loss": 0.0, "noise": -104}
    assert w.deviations(ok, us) == [] and w.score(ok, us) == 100 and w.state_of(100, [], 0) == "ok"
    bad = dict(ok, signal_a=-71, signal_b=-69, rate_ab=52e6, loss=4.0, noise=-94)
    codes = [d["code"] for d in w.deviations(bad, us)]
    assert codes == ["signal", "signal", "rate", "loss", "noise"]
    sc = w.score(bad, us)
    assert 20 < sc < 60 and w.state_of(sc, w.deviations(bad, us), 0) in ("bad", "degraded")
    assert w.state_of(0, [{"code": "down"}], 0) == "down" and w.score({"up": False}, us) == 0
    assert w.state_of(70, [{"code": "loss", "now": 12.0}], 0) == "bad"
    # bez známého normálu se hlásí jen slabý signál a ztrátovost
    assert [d["code"] for d in w.deviations({"up": True, "signal_a": -80, "loss": 0.0}, {"n": 3})] == ["weak"]
    assert "signál -71 dBm (obvykle -58)" in w.why_text(w.deviations(bad, us)) and "ztrátovost 4,0 %" in w.why_text(w.deviations(bad, us))

    pts = [dict(ok, t=i) for i in range(4)] + [dict(bad, t=4), dict(ok, t=5), dict(bad, t=6)] + [dict(ok, t=i) for i in range(7, 10)]
    pts.append(dict(ok, t=10, reconnect=True))
    ep = w.episodes(pts, us)
    assert [(e["from"], e["to"]) for e in ep] == [(4, 6), (10, 10)] and ep[0]["worst"] == {"signal": -71, "loss": 4.0}
    assert ep[1]["codes"] == ["reconnect"]


def test_discover_sample_alerts_api(monkeypatch):
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import alerts, inventory, services, wlink as w
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.2", "spoj-hala")
        sta = _adopted(db, box, tid, sid, "ST1", "10.200.1.3", "spoj-sklad")
        inventory.store(db, tid, ap, _ap(), [])
        inventory.store(db, tid, sta, _sta(), [])
        state = {"signal": -58, "loss": 0, "up": True, "uptime": "11h42m"}

        def fake_api(tx, box_, dev, timeout=10.0):
            if str(dev["id"]) == ap:
                return FakeApi("BB:BB:BB:00:00:02", state["signal"], loss=state["loss"], up=state["up"], uptime=state["uptime"])
            return FakeApi("AA:AA:AA:00:00:01", state["signal"] - 2)

        monkeypatch.setattr(services, "device_api", fake_api)
        assert w.sample(db, box, force=True) == 1
        assert w.discover(db) == 1                              # opakované nalezení spoj nezdvojí
        with operator_scope(db) as tx:
            ln = tx.fetchone("SELECT id, a_device, b_device, host(ping_ip) AS ip FROM wlink")
            assert str(ln["a_device"]) == ap and str(ln["b_device"]) == sta and ln["ip"] == "10.9.0.21"
            lid = str(ln["id"])
            assert tx.fetchone("SELECT count(*) AS n FROM wlink_sample")["n"] == 1
            # týden historie v normálu
            tx.execute("""INSERT INTO wlink_sample (tenant_id, link_id, at, up, signal_a, signal_b, rate_ab, rate_ba, loss, rtt_avg, noise)
                          SELECT %(t)s, %(l)s, now() - make_interval(mins => 40 + m * 30), true, -58, -60, 130000000, 130000000, 0, 2.4, -104
                          FROM generate_series(1, 120) m""", {"t": tid, "l": lid})
        assert w.sample(db, box) == 0                           # ještě není na řadě
        with tenant_scope(db, tid) as tx:
            v = w.site_links(tx, sid)[0]
            assert v["name"] == "spoj-hala ↔ spoj-sklad" and v["state"] == "ok" and v["score"] == 100 and v["usual"]["signal_a"] == -58.0
            assert w.alerts(tx) == []
        # propad signálu a ztrátovost, k tomu přepojení (doba spojení klesla)
        state.update(signal=-72, loss=3, uptime="2m")
        for _ in range(3):
            assert w.sample(db, box, force=True) == 1
            with operator_scope(db) as tx:
                tx.execute("UPDATE wlink_sample SET at = at - interval '6 minutes' WHERE at > now() - interval '30 minutes'")
        with tenant_scope(db, tid) as tx:
            v = w.site_links(tx, sid)[0]
            assert v["state"] in ("degraded", "bad") and {d["code"] for d in v["why"]} >= {"signal", "loss"} and v["flaps"] == 1
            found = w.alerts(tx)
            assert found[0]["kind"] == "link_degraded" and str(found[0]["device_id"]) == ap and "ztrátovost" in found[0]["message"]
        alerts.reconcile(db)
        c = _login(app)
        r = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/links").json()["links"]
        assert len(r) == 1 and r[0]["id"] == lid and r[0]["a"]["iface"] == "wlan1"
        d = c.get(f"/api/admin/tenants/{tid}/links/{lid}?hours=72").json()
        assert len(d["points"]) > 100 and d["episodes"] and "signal" in d["episodes"][-1]["codes"] and d["bucket"] == 900
        # výpadek spoje → link_down; ztlumený spoj nehlásí nic
        state.update(up=False)
        assert w.sample(db, box, force=True) == 1
        with tenant_scope(db, tid) as tx:
            assert [a["kind"] for a in w.alerts(tx)] == ["link_down"]
        r = c.put(f"/api/admin/tenants/{tid}/links/{lid}", json_body={"muted": True})
        assert r.status_code == 200 and r.json()["muted"] is True and r.json()["state"] == "down"
        with tenant_scope(db, tid) as tx:
            assert w.alerts(tx) == []
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'wlink.mute'")["n"] == 1
            kinds = {r["kind"] for r in tx.fetchall("SELECT kind FROM alert")}
        assert "link_degraded" in kinds
    finally:
        drop_db(name)
