"""Rozbor připojení klienta: přenesená data ze vzorků, cesta sítí, stav AP a odpověď API."""

from datetime import datetime, timedelta, timezone

from mtcontroller import clientview

T0 = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)
MAC = "AA:BB:CC:00:00:01"


def test_activity_from_counters():
    s = lambda m, dev, iface, tx, rx: {"at": T0 + timedelta(minutes=m), "device_id": dev, "iface": iface, "tx_bytes": tx, "rx_bytes": rx}
    out = clientview.activity([s(0, "a", "wifi1", 1000, 100), s(5, "a", "wifi1", 1600, 150),
                               s(10, "a", "wifi1", 200, 20),            # čítač klesl = nové připojení
                               s(15, "b", "wifi1", 900, 90),            # jiné AP = nové připojení
                               {"at": T0 + timedelta(minutes=20), "device_id": "b", "iface": "wifi1", "tx_bytes": None, "rx_bytes": None},
                               s(25, "b", "wifi1", 5000, 500), s(30, "b", "wifi1", 5500, 600)])
    assert [(x["down"], x["up"]) for x in out] == [(600, 50), (200, 20), (900, 90), (500, 100)]
    assert clientview.activity([]) == []


def test_path_and_channel():
    view = {"nodes": [{"id": "gw", "label": "gw-hala", "role": "gateway", "state": "adopted", "model": "RB5009", "parent": None},
                      {"id": "sw", "label": "sw-hala", "role": "switch", "state": "adopted", "model": "CRS326", "parent": "gw",
                       "uplink_port": "sfp-sfpplus1", "parent_port": "ether2"},
                      {"id": "ap", "label": "ap-kancelar", "role": "ap", "state": "adopted", "model": "cAP ax", "parent": "sw",
                       "uplink_port": "ether1", "parent_port": "ether5"}],
            "wans": [{"interface": "ether1", "address": "193.165.120.202", "active": True}]}
    probe = {"targets": [{"target": "1.1.1.1", "rtt_avg": 5.4, "loss": 0}], "availability": 100.0}
    cl = {"mac": MAC, "hostname": "A55-Martina", "ip": "10.0.10.60", "device_id": "ap", "ssid": "novak", "port": "wifi2"}
    hops = clientview._path(view, cl, {"signal": -56}, probe)
    assert [h["name"] for h in hops] == ["A55-Martina", "ap-kancelar", "sw-hala", "gw-hala", "Internet"]
    assert hops[0]["link"] == "-56 dBm" and hops[0]["wireless"] and hops[1]["link"] == "ether1 → ether5"
    assert hops[2]["link"] == "sfp-sfpplus1 → ether2" and hops[3]["link"] == "5 ms" and hops[4]["availability"] == 100.0
    # klient bez známého zařízení: jen klient a internet; smyčka v rodičích neuvázne
    assert [h["kind"] for h in clientview._path(view, {"mac": MAC}, None, {})] == ["client", "wan"]
    loop = {"nodes": [{"id": "a", "label": "a", "parent": "b"}, {"id": "b", "label": "b", "parent": "a"}], "wans": []}
    assert len(clientview._path(loop, {"mac": MAC, "device_id": "a"}, None, {})) == 4
    # kanál: monitor na AP, jinak monitor rozhraní CAP na správci
    invs = {"ap": {"wifi_monitor": [{"name": "wifi2", "channel": "5180/ax/Ce"}]}, "gw": {"wifi_monitor": [{"name": "cap-wifi1", "channel": "2437/n"}]},
            "ap2": {}}
    cap = {("gw", "cap-wifi1"): {"ap_id": "ap2", "radio": "wifi1"}}
    assert clientview._channel(invs, cap, "ap", "wifi2") == {"freq": 5180, "channel": 36, "width": 40, "band": "5"}
    assert clientview._channel(invs, cap, "ap2", "wifi1")["channel"] == 6
    assert clientview._channel(invs, cap, "ap", "wifi9")["freq"] is None


def test_insight_endpoint():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "AP-chodba")
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET role = 'gateway' WHERE id = %(d)s", {"d": gw})
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/client-insight"
        r = c.get(url + "?mac=" + MAC).json()                    # neznámý klient: prázdná, ale platná odpověď
        assert r["client"]["mac"] == MAC and r["ap"] is None and r["activity"] == [] and r["path"][-1]["kind"] == "wan"
        # CAPsMAN na bráně drží registrační tabulku; klient patří AP
        inventory.store(db, tid, ap, {"wifi": [{"name": "wifi1", "mac-address": "48:A9:8A:00:00:01"}],
                                      "wifi_monitor": [{"name": "wifi1", "channel": "5180/ax/Ceee"}],
                                      "interfaces": [{"name": "ether1", "type": "ether", "mac-address": "48:A9:8A:00:00:00"}]}, [])
        reg = lambda b: {"wifi": [{"name": "cap-wifi1", "mac-address": "48:A9:8A:00:00:01"}],
                         "interfaces": [{"name": "ether1", "type": "ether", "mac-address": "AA:00:00:00:00:01"}],
                         "wifi_clients": [{"interface": "cap-wifi1", "mac-address": MAC, "ssid": "Firma", "signal": "-58",
                                           "tx-rate": "286Mbps", "rx-rate": "240Mbps", "bytes": b},
                                          {"interface": "cap-wifi1", "mac-address": "AA:BB:CC:00:00:02", "ssid": "Firma", "signal": "-70"}]}
        inventory.store(db, tid, gw, reg("1000,200"), [])
        with operator_scope(db) as tx:
            tx.execute("UPDATE wifi_sample SET at = now() - interval '6 minutes'")
        inventory.store(db, tid, gw, reg("51000,1200"), [])
        with operator_scope(db) as tx:
            tx.execute("""INSERT INTO client (tenant_id, site_id, mac, hostname, ip, device_id, device, port, ssid, signal)
                          VALUES (%(t)s, %(s)s, %(m)s, 'A55-Martina', '10.0.10.60', %(a)s, 'AP-chodba', 'wifi1', 'Firma', '-58')
                          ON CONFLICT (site_id, mac) DO UPDATE SET device_id = EXCLUDED.device_id, device = EXCLUDED.device,
                              port = EXCLUDED.port, ssid = EXCLUDED.ssid, signal = EXCLUDED.signal, hostname = EXCLUDED.hostname,
                              ip = EXCLUDED.ip, last_seen = now()""", {"t": tid, "s": sid, "m": MAC, "a": ap})
            tx.execute("""INSERT INTO wifi_bss (tenant_id, site_id, bssid, ssid, frequency, signal, kind)
                          VALUES (%(t)s, %(s)s, 'DE:AD:BE:EF:00:01', 'soused', 5180, -80, 'neighbor'),
                                 (%(t)s, %(s)s, 'DE:AD:BE:EF:00:02', 'jinde', 5500, -80, 'neighbor')""", {"t": tid, "s": sid})
        r = c.get(url + "?mac=aa-bb-cc-00-00-01&hours=24").json()
        assert r["client"]["hostname"] == "A55-Martina" and r["client"]["online"] is True and r["wifi"]["signal"] == -58
        assert r["wifi"]["tx_rate"] == "286Mbps"
        assert [(a["down"], a["up"]) for a in r["activity"]] == [(50000, 1000)]
        a = r["ap"]
        assert a["id"] == ap and a["name"] == "AP-chodba" and a["radio"] == "wifi1"
        assert (a["channel"], a["width"], a["band"]) == (36, 80, "5") and a["foreign"] == 1 and a["clients"] == 1
        assert a["series"] and max(x["clients"] for x in a["series"]) == 2          # oba klienti z tabulky správce patří AP
        assert [h["kind"] for h in r["path"]][0] == "client" and r["path"][1]["name"] == "AP-chodba" and r["path"][-1]["kind"] == "wan"
        assert r["journey"]["lanes"][0]["ap"] == "AP-chodba" and len(r["journey"]["samples"]) == 2
        assert r["site"]["points"] == [] and r["hours"] == 24
        assert c.get(url + "?mac=xx").status_code == 400
        assert c.get(url.replace(sid, "00000000-0000-0000-0000-000000000000") + "?mac=" + MAC).status_code == 404
    finally:
        drop_db(name)
