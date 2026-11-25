"""Provoz podle klientů: rozbor NetFlow v5 / v9 / IPFIX, určení klienta, součty, zápis, API a skript brány."""
import ipaddress
import struct

import pytest

from mtcontroller import flow


def ip4(a):
    return ipaddress.ip_address(a).packed


# šablona 256: src4, dst4, sport, dport, proto, bytes, pkts, adresa cíle po NATu, neznámé pole (přeskočit)
FIELDS = [(8, 4), (12, 4), (7, 2), (11, 2), (4, 1), (1, 4), (2, 4), (226, 4), (21, 4)]


def record(src, dst, sport, dport, proto, nbytes, pkts, nat_dst="0.0.0.0"):
    return ip4(src) + ip4(dst) + struct.pack("!HHBII", sport, dport, proto, nbytes, pkts) + ip4(nat_dst) + b"\0\0\0\0"


def v9(records, template=True, source_id=7):
    sets = b""
    if template:
        t = struct.pack("!HH", 256, len(FIELDS)) + b"".join(struct.pack("!HH", *f) for f in FIELDS)
        sets += struct.pack("!HH", 0, 4 + len(t)) + t
        # šablona možností (statistiky exportéru) – data k ní se nesmí číst jako toky
        opt = struct.pack("!HHH", 257, 4, 4) + struct.pack("!HH", 1, 4) + struct.pack("!HH", 41, 4)
        sets += struct.pack("!HH", 1, 4 + len(opt)) + opt
        sets += struct.pack("!HH", 257, 4 + 8) + b"\0" * 8
    if records:
        body = b"".join(records)
        pad = (-len(body)) % 4
        sets += struct.pack("!HH", 256, 4 + len(body) + pad) + body + b"\0" * pad
    return struct.pack("!HHIIII", 9, len(records) + 1, 1000, 1700000000, 1, source_id) + sets


def ipfix(records):
    t = struct.pack("!HH", 256, len(FIELDS) + 1) + b"".join(struct.pack("!HH", *f) for f in FIELDS)
    t += struct.pack("!HHI", 0x8000 | 99, 0xFFFF, 14988)         # pole výrobce s proměnnou délkou
    sets = struct.pack("!HH", 2, 4 + len(t)) + t
    body = b"".join(r + b"\x03abc" for r in records)
    sets += struct.pack("!HH", 256, 4 + len(body)) + body
    return struct.pack("!HHIII", 10, 16 + len(sets), 1700000000, 1, 5) + sets


UP = record("192.168.88.10", "142.250.1.1", 50000, 443, 6, 1500, 10)
DOWN = record("142.250.1.1", "193.165.120.202", 443, 50000, 6, 90000, 70, "192.168.88.10")


def test_parser_v9_ipfix_v5():
    p = flow.Parser()
    assert p.parse(v9([UP], template=False), "10.200.1.1") == []          # data před šablonou se zahodí
    recs = p.parse(v9([UP, DOWN]), "10.200.1.1")
    assert len(recs) == 2 and recs[0] == {"src": "192.168.88.10", "dst": "142.250.1.1", "sport": 50000, "dport": 443,
                                          "proto": 6, "bytes": 1500, "pkts": 10, "nat_dst": "0.0.0.0"}
    assert recs[1]["nat_dst"] == "192.168.88.10"
    assert len(p.parse(v9([UP], template=False), "10.200.1.1")) == 1      # šablona si pamatuje zdroj…
    assert p.parse(v9([UP], template=False), "10.200.1.9") == []          # …a jinému zdroji nepatří
    assert p.parse(v9([UP], template=False, source_id=8), "10.200.1.1") == []
    r = flow.Parser().parse(ipfix([UP, DOWN]), "10.200.1.1")
    assert [x["bytes"] for x in r] == [1500, 90000] and r[0]["dport"] == 443
    v5 = struct.pack("!HHIIIIBBH", 5, 1, 0, 0, 0, 0, 0, 0, 0) + ip4("192.168.1.5") + ip4("8.8.8.8") + ip4("0.0.0.0") \
        + struct.pack("!HHIIIIHHBBBBHHBBH", 1, 2, 3, 400, 0, 0, 1234, 53, 0, 0, 17, 0, 0, 0, 0, 0, 0)
    assert flow.Parser().parse(v5, "x") == [{"src": "192.168.1.5", "dst": "8.8.8.8", "sport": 1234, "dport": 53,
                                             "proto": 17, "bytes": 400, "pkts": 3}]
    for junk in (b"", b"\x00\x09", b"\x00\x09" + b"\xff" * 40, b"\x00\x0a" + b"\x00" * 30, bytes(range(256))):
        assert flow.Parser().parse(junk, "x") == []                       # nesmysly nic neshodí


def test_classify_and_names():
    c = flow.classify
    assert c({"src": "192.168.88.10", "dst": "1.1.1.1", "sport": 5, "dport": 443, "proto": 6, "bytes": 10}) == \
        ("192.168.88.10", "1.1.1.1", 6, 443, 10, 0)
    # příchozí: cíl je veřejná adresa brány, klient je v adrese po NATu
    assert c({"src": "1.1.1.1", "dst": "193.165.120.202", "nat_dst": "192.168.88.10", "sport": 443, "dport": 5,
              "proto": 6, "bytes": 99}) == ("192.168.88.10", "1.1.1.1", 6, 443, 0, 99)
    assert c({"src": "1.1.1.1", "dst": "10.0.0.5", "nat_dst": "0.0.0.0", "sport": 53, "dport": 7, "proto": 17,
              "bytes": 9}) == ("10.0.0.5", "1.1.1.1", 17, 53, 0, 9)
    assert c({"src": "1.1.1.1", "dst": "8.8.8.8", "proto": 1, "sport": 7, "dport": 8, "bytes": 4}) == \
        ("1.1.1.1", "8.8.8.8", 1, 0, 4, 0)                                # ICMP nemá porty
    assert c({"src": "1.1.1.1"}) is None
    assert flow.service_name(6, 443) == "HTTPS" and flow.service_name(17, 40000) == "UDP/40000"
    assert flow.service_name(1, 0) == "ICMP" and flow.service_name(0, 0) == "ostatní"


def test_gateway_script():
    from test_routeros import OPTS, _balanced

    from mtcontroller import routeros

    on = flow.config_body(True, "10.200.0.1", 2055, "10.200.1.2")
    assert "/ip traffic-flow target remove [find dst-address=10.200.0.1 port=2055]" in on
    assert "target add dst-address=10.200.0.1 port=2055 version=9 src-address=10.200.1.2" in on and "enabled=yes" in on
    off = flow.config_body(False, "10.200.0.1", 2055, None)
    assert "target add" not in off and "[:len [/ip traffic-flow target find]] = 0" in off and "enabled=no" in off
    for body in (on, off):
        s = routeros.wrap_command("https://c.example/api/checkin/ack?id=13", "KEY", body, OPTS)
        assert _balanced(s), s
        s.encode("ascii")
        assert "mtc" in s
    with pytest.raises(ValueError):
        flow.config_body(True, '10.0.0.1"; /system reset', 2055, None)


def test_collector_and_api():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        url = base + f"/sites/{sid}/traffic"
        r = c.get(url).json()
        assert r["gateways"] == [] and r["total"]["up"] == 0 and r["clients"] == []
        assert c.post(base + f"/devices/{sw}/flow", json_body={"enabled": True}).status_code == 400   # jen brána
        assert c.post(base + f"/devices/{gw}/flow", json_body={"enabled": "ano"}).status_code == 400
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET role = 'gateway' WHERE id = %(d)s", {"d": gw})
            tx.execute("""INSERT INTO client (tenant_id, site_id, mac, hostname, ip, device_id, device, port)
                          VALUES (%(t)s, %(s)s, '50:00:00:00:00:01', 'pc-vera', '192.168.88.10', %(g)s, 'gw', 'ether5')""",
                       {"t": tid, "s": sid, "g": gw})
        col = flow.Collector(db, settings)
        col.refresh(force=True)
        assert col.receive(v9([UP]), "10.200.1.1") == 0                    # sběr vypnutý → zdroj neznámý
        r = c.post(base + f"/devices/{gw}/flow", json_body={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True, r.text
        with operator_scope(db) as tx:
            cmd = tx.fetchone("SELECT kind, params, status FROM device_command WHERE device_id = %(d)s AND kind = 'flow'",
                              {"d": gw})
            aud = tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'device.flow'")["n"]
        assert cmd["params"] == {"enabled": True} and aud == 1
        col.refresh(force=True)
        assert col.receive(v9([UP]), "10.9.9.9") == 0                      # cizí adresa
        now = 1_700_000_000.0
        other = record("192.168.88.11", "8.8.8.8", 40000, 53, 17, 200, 2)
        assert col.receive(v9([UP, DOWN, UP, other]), "10.200.1.1", now) == 4
        settings.flow.max_rows_per_hour = 20
        assert col.flush() == 2 and col.flush() == 0
        with operator_scope(db) as tx:
            tx.execute("UPDATE flow_hourly SET hour = date_trunc('hour', now())")
        r = c.get(url + "?hours=24").json()
        assert r["gateways"][0]["id"] == gw and r["gateways"][0]["enabled"] is True and r["gateways"][0]["last_hour"]
        assert r["total"] == {"up": 3200, "down": 90000, "flows": 4, "clients": 2}
        top = r["clients"][0]
        assert top["ip"] == "192.168.88.10" and top["hostname"] == "pc-vera" and top["mac"] == "50:00:00:00:00:01"
        assert top["up"] == 3000 and top["down"] == 90000 and r["clients"][1]["hostname"] is None
        assert r["peers"][0]["ip"] == "142.250.1.1" and r["peers"][0]["service"] == "HTTPS"
        assert [s["name"] for s in r["services"]] == ["HTTPS", "DNS"] and len(r["series"]) == 1
        one = c.get(url + "?client=192.168.88.11").json()
        assert one["total"]["up"] == 200 and [p["ip"] for p in one["peers"]] == ["8.8.8.8"]
        assert c.get(url + "?client=nesmysl").status_code == 400

        # limit řádků: zbytek se sečte do „ostatní“; stejně tak úklid uzavřených hodin
        many = [record("192.168.88.10", f"9.9.{i // 200}.{i % 200 + 1}", 40000, 443, 6, 100 + i, 1) for i in range(30)]
        for i in range(0, 30, 10):
            col.receive(v9(many[i:i + 10]), "10.200.1.1", now + 7200)
        assert col.flush() == 21                                           # 20 největších + ostatní
        with operator_scope(db) as tx:
            o = tx.fetchone("""SELECT bytes_up, flows FROM flow_hourly WHERE peer = '0.0.0.0'::inet AND port = 0""")
            assert o["flows"] == 10 and o["bytes_up"] == sum(100 + i for i in range(10))
            tx.execute("UPDATE flow_hourly SET hour = date_trunc('hour', now()) - interval '1 hour' WHERE peer <<= '9.9.0.0/16'::inet OR port = 0")
        assert flow.compact(db, 5) == 15
        with operator_scope(db) as tx:
            n = tx.fetchone("""SELECT count(*) AS n, sum(bytes_up) AS b FROM flow_hourly
                               WHERE hour = date_trunc('hour', now()) - interval '1 hour'""")
        assert n["n"] == 6 and n["b"] == sum(100 + i for i in range(30))   # nic se neztratilo
        # pojistka proti zahlcení a vypnutí
        settings.flow.max_packets_per_minute = 2
        assert [col.receive(v9([UP]), "10.200.1.1", now) for _ in range(3)] == [1, 1, 0] and col.dropped == 1
        assert c.post(base + f"/devices/{gw}/flow", json_body={"enabled": False}).json()["enabled"] is False
        col.refresh(force=True)
        assert col.receive(v9([UP]), "10.200.1.1", now + 600) == 0
        with operator_scope(db) as tx:
            tx.execute("UPDATE flow_hourly SET hour = now() - interval '40 days'")
        assert flow.retention(db, 30) > 0
    finally:
        drop_db(name)
