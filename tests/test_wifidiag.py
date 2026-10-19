"""Wi-Fi diagnostika klientů: události z logů, vzorky z registrační tabulky, časová osa a nálezy."""

from datetime import datetime, timedelta, timezone

from mtcontroller import devlog, wifidiag

T0 = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
MAC = "AA:BB:CC:00:00:01"


def test_parse_event():
    p = wifidiag.parse_event
    assert p("AA:BB:CC:00:00:01@wifi2 connected, signal strength -46") == {
        "mac": MAC, "iface": "wifi2", "kind": "connected", "signal": -46, "ft": False, "reason": ""}
    e = p("aa:bb:cc:00:00:01@cap-wifi3 disconnected, connection lost, signal strength -81")
    assert e["kind"] == "disconnected" and e["reason"] == "connection lost" and e["signal"] == -81 and e["iface"] == "cap-wifi3"
    assert p("AA:BB:CC:00:00:01@wifi1 connected, FT, signal strength -60")["ft"] is True
    assert p("AA:BB:CC:00:00:01@wlan1: disconnected, received deauth: sending station leaving (3), signal strength -70")["reason"] \
        == "received deauth: sending station leaving (3)"
    assert p("AA:BB:CC:00:00:01@wifi1 failed to connect, 4-way handshake timeout")["kind"] == "auth_fail"
    assert p("user admin logged in") is None


INVS = {
    # CAPsMAN: rozhraní CAP s MAC rádií AP
    "host": {"wifi": [{"name": "cap-wifi1", "mac-address": "48:A9:8A:00:00:01"}, {"name": "cap-wifi2", "mac-address": "48:A9:8A:00:00:11"}]},
    "ap1": {"wifi": [{"name": "wifi1", "mac-address": "48:A9:8A:00:00:01"}],
            "wifi_radios": [{"interface": "wifi1", "bands": "5ghz-a,5ghz-ax"}]},
    "ap2": {"wifi": [{"name": "wifi1", "mac-address": "48:A9:8A:00:00:11"}],
            "wifi_radios": [{"interface": "wifi1", "bands": "5ghz-ax"}]},
}
DEVS = [{"id": "host", "identity": "SW-core"}, {"id": "ap1", "identity": "AP-chodba"}, {"id": "ap2", "identity": "AP-sklad"}]


def test_iface_map_translates_cap_interfaces():
    m = wifidiag.iface_map(DEVS, INVS)
    assert m[("host", "cap-wifi2")] == {"ap": "AP-sklad", "ap_id": "ap2", "radio": "wifi1", "band": "5"}
    assert m[("ap1", "wifi1")]["ap"] == "AP-chodba"


def _ev(minute, dev, iface, kind, sig=None, ft=False, reason=""):
    return {"at": T0 + timedelta(minutes=minute), "device_id": dev, "iface": iface, "kind": kind, "signal": sig, "ft": ft,
            "reason": reason}


def _smp(minute, dev, iface, sig, rate="144Mbps"):
    return {"at": T0 + timedelta(minutes=minute), "device_id": dev, "iface": iface, "signal": sig, "tx_rate": rate,
            "rx_rate": rate, "ssid": "Firma"}


def test_journey_and_findings():
    m = wifidiag.iface_map(DEVS, INVS)
    names = {d["id"]: d["identity"] for d in DEVS}
    events = [_ev(0, "host", "cap-wifi1", "connected", -50),
              # ping-pong mezi AP (události od CAPsMANu, rozhraní CAP)
              *[_ev(10 + i, "host", "cap-wifi2" if i % 2 == 0 else "cap-wifi1", "roamed", -68, ft=True) for i in range(5)],
              _ev(30, "host", "cap-wifi1", "disconnected", -82, reason="connection lost"),
              _ev(31, "host", "cap-wifi1", "connected", -80),
              _ev(40, "host", "cap-wifi1", "disconnected", -83, reason="connection lost"),
              _ev(41, "host", "cap-wifi2", "connected", -60),
              _ev(50, "host", "cap-wifi2", "disconnected", -84, reason="extensive data loss")]
    samples = [_smp(55 + 5 * i, "ap1", "wifi1", -78 - i % 2, "6Mbps") for i in range(4)]
    j = wifidiag.journey(events, samples, m, names, T0 + timedelta(hours=2))
    assert {l["ap"] for l in j["lanes"]} == {"AP-chodba", "AP-sklad"}
    assert j["segments"][0]["lane"] == "ap1|wifi1" and j["segments"][0]["start"] == T0
    assert sum(1 for r in j["roams"] if r["how"] == "ft") >= 4
    assert any(r["how"] == "reconnect" for r in j["roams"])   # 40→41: plné přihlášení na jiném AP
    codes = {f["code"] for f in wifidiag.diagnose(j)}
    assert {"pingpong", "disconnects", "no_ft", "sticky", "low_rate"} <= codes
    # jen vzorky (bez logů): roaming odvozený, rada zapnout události
    j2 = wifidiag.journey([], [_smp(0, "ap1", "wifi1", -55), _smp(5, "ap2", "wifi1", -58)], m, names, T0 + timedelta(hours=1))
    assert j2["roams"][0]["how"] == "inferred"
    assert "no_logs" in {f["code"] for f in wifidiag.diagnose(j2)}


def test_config_body_wifi_events():
    s = devlog.config_body("off", "10.200.0.1", 5514, "10.200.1.2", wifi=True)
    assert 'topics="wireless,!debug"' in s and s.count("/system logging add") == 4
    assert 'topics="wireless,!debug"' not in devlog.config_body("info", "10.200.0.1", 5514, None, wifi=True)


def test_flow_events_samples_journey():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory

    db, name, settings, box, app, tid, sid = _env()
    try:
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "AP-chodba")
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        # Wi-Fi události na celé lokalitě (jen zařízení s Wi-Fi)
        inventory.store(db, tid, ap, {"wifi": [{"name": "wifi1", "mac-address": "48:A9:8A:00:00:01"}],
                                      "wifi_clients": [{"interface": "wifi1", "mac-address": MAC, "ssid": "Firma",
                                                        "signal": "-61", "tx-rate": "433.3Mbps-80MHz/1S"}]}, [])
        assert c.post(T + f"/sites/{sid}/syslog", json_body={"wifi": True}).json()["devices"] == 1
        assert c.get(T + f"/devices/{ap}/logs").json()["wifi"] is True
        col = devlog.Collector(db, settings)
        col.refresh(force=True)
        assert col.receive(b"<30>wireless,info AA:BB:CC:00:00:01@wifi1 disconnected, connection lost, signal strength -80", "10.200.1.3")
        assert col.receive(b"<30>wireless,info AA:BB:CC:00:00:01@wifi1 connected, signal strength -62", "10.200.1.3")
        col.flush()
        j = c.get(T + f"/sites/{sid}/wifi/journey?mac=aa-bb-cc-00-00-01&hours=6").json()
        assert [e["kind"] for e in j["events"]] == ["disconnected", "connected"]
        assert j["lanes"][0]["ap"] == "AP-chodba" and j["samples"][0]["signal"] == -61 and j["client"]["mac"] == MAC
        assert j["findings"]
        r = c.get(T + f"/sites/{sid}/wifi/roaming?hours=24").json()
        assert r["clients"][0]["mac"] == MAC and r["clients"][0]["disconnects"] == 1 and r["has_events"]
        assert r["logging"]["on_"] == 1
        assert c.get(T + f"/sites/{sid}/wifi/journey?mac=xx").status_code == 400
    finally:
        drop_db(name)
