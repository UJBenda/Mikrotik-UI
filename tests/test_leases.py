"""DHCP leases, statické IP (rezervace), DHCP options, poznámky a výrobce podle MAC."""

import copy
from pathlib import Path

import pytest

from mtcontroller import leases, oui

oui.configure(Path(__file__).parent / "data")

INV = {
    "interfaces": [{"name": "vlan10", "type": "vlan", "mac-address": "4C:5E:0C:00:00:01"}],
    "addresses": [{"address": "192.168.10.1/24", "interface": "vlan10"}],
    "dhcp_servers": [{"name": "dhcp10", "interface": "vlan10", "address-pool": "pool10", "lease-time": "30m"}],
    "ip_pools": [{"name": "pool10", "ranges": "192.168.10.100-192.168.10.199"}],
    "dhcp_options": [{"name": "ntp", "code": "42", "value": "'192.168.10.1'"}],
    "dhcp_leases": [
        # rezervace (statická) s options
        {"server": "dhcp10", "mac-address": "00:11:32:AA:BB:01", "address": "192.168.10.10", "active-address": "192.168.10.10",
         "dynamic": "false", "status": "bound", "host-name": "nas", "comment": "NAS sklad", "dhcp-option": "ntp"},
        # dynamický
        {"server": "dhcp10", "mac-address": "B0:BE:76:12:34:56", "active-address": "192.168.10.150", "address": "192.168.10.150",
         "dynamic": "true", "status": "bound", "host-name": "tiskarna"},
        # náhodná MAC telefonu
        {"server": "dhcp10", "mac-address": "DA:A1:19:00:00:01", "active-address": "192.168.10.151", "dynamic": "true",
         "status": "bound", "host-name": "iPhone"},
        # jiný server – nepatří sem
        {"server": "dhcp20", "mac-address": "00:00:00:00:00:99", "address": "192.168.20.5", "dynamic": "false"},
    ],
    "arp": [
        {"address": "192.168.10.20", "mac-address": "B0:BE:76:00:00:01", "interface": "vlan10", "complete": "true"},  # ruční IP
        {"address": "192.168.10.120", "mac-address": "4C:5E:0C:99:00:01", "interface": "vlan10"},                     # ruční v poolu
        {"address": "192.168.10.150", "mac-address": "AA:AA:AA:00:00:01", "interface": "vlan10"},                     # konflikt
        {"address": "192.168.10.10", "mac-address": "00:11:32:AA:BB:01", "interface": "vlan10"},                      # už v leases
    ],
}
CLIENTS = {"B0:BE:76:12:34:56": {"hostname": "tiskarna", "device": "SW1", "port": "ether7", "ssid": None,
                                 "signal": None, "last_seen": "2026-10-18T07:00:00+02:00"}}


def test_oui():
    assert oui.vendor("4c-5e-0c-11-22-33") == "Routerboard.com"
    assert oui.vendor("B0:BE:76:12:34:56") == "Velmi Specificky s.r.o."   # MA-M má přednost
    assert oui.vendor("B0:BE:76:22:34:56") == "TP-LINK TECHNOLOGIES CO.,LTD."
    assert oui.vendor("DA:A1:19:00:00:01") == oui.RANDOM
    assert oui.vendor("00:00:5E:00:00:01") is None and oui.vendor("xx") is None


def test_server_view():
    v = leases.server_view(INV, "dhcp10", CLIENTS, {"00:11:32:AA:BB:01": "zálohy"})
    by = {r["mac"]: r for r in v["leases"]}
    assert [r["ip"] for r in v["leases"]] == ["192.168.10.10", "192.168.10.20", "192.168.10.120",
                                              "192.168.10.150", "192.168.10.150", "192.168.10.151"]
    nas = by["00:11:32:AA:BB:01"]
    assert nas["kind"] == "static" and nas["vendor"] == "Synology Incorporated" and nas["note"] == "zálohy"
    assert nas["options"] == ["ntp"] and nas["comment"] == "NAS sklad"
    t = by["B0:BE:76:12:34:56"]
    assert t["kind"] == "dynamic" and t["where"]["port"] == "ether7" and "konflikt" in t["warnings"][0]
    assert by["B0:BE:76:00:00:01"]["kind"] == "manual" and by["B0:BE:76:00:00:01"]["warnings"] == []
    assert "v rozsahu poolu" in by["4C:5E:0C:99:00:01"]["warnings"][0]
    sm = v["summary"]
    assert sm == {"static": 1, "dynamic": 2, "manual": 3, "pool_size": 100, "pool_free": 97, "conflicts": 1}
    assert v["gateway"] == "192.168.10.1" and v["pool"] == ["192.168.10.100-192.168.10.199"]
    assert v["suggest"] == "192.168.10.2"
    cells = v["map"]["cells"]
    assert len(cells) == 254 and cells[0] == "gateway" and cells[9] == "static" and cells[19] == "manual"
    assert cells[149] == "dynamic-conflict" and cells[99] == "pool" and cells[200] == "free"
    with pytest.raises(leases.LeaseError):
        leases.server_view(INV, "nic", {}, {})


def test_plan_dynamic_to_static_with_new_option():
    p = leases.plan(INV, "dhcp10", "b0:be:76:12:34:56", {
        "address": "192.168.10.30", "comment": "Tiskarna kancl", "options": ["ntp", "tftp"],
        "new_options": [{"name": "tftp", "code": 66, "type": "text", "value": "10.0.0.5"}]})
    a = p["apply"].splitlines()
    assert a[0] == """/ip dhcp-server option add name="tftp" code=66 value="s'10.0.0.5'" comment="mtc\""""
    assert a[1] == '/ip dhcp-server lease make-static [find server="dhcp10" mac-address="B0:BE:76:12:34:56" dynamic=yes]'
    assert a[2].startswith('/ip dhcp-server lease set [find server="dhcp10" mac-address="B0:BE:76:12:34:56" dynamic=no] address=192.168.10.30')
    assert 'comment="Tiskarna kancl"' in a[2] and 'dhcp-option="ntp,tftp"' in a[2]
    r = p["revert"].splitlines()
    assert r[0].startswith("/ip dhcp-server lease remove") and r[1].startswith("/ip dhcp-server option remove")
    assert "obnovení leasu" in p["warnings"][0]
    assert p["summary"].startswith("DHCP dhcp10 B0:BE:76:12:34:56: rezervace dynamická 192.168.10.150 → statická 192.168.10.30")


def test_plan_static_change_and_remove():
    p = leases.plan(INV, "dhcp10", "00:11:32:AA:BB:01", {"address": "192.168.10.11", "options": [], "block_access": True})
    assert p["apply"].strip() == ('/ip dhcp-server lease set [find server="dhcp10" mac-address="00:11:32:AA:BB:01" dynamic=no] '
                                  'address=192.168.10.11 dhcp-option="" block-access=yes')
    assert p["revert"].strip().endswith('address=192.168.10.10 dhcp-option="ntp" block-access=no')
    p = leases.plan(INV, "dhcp10", "00:11:32:AA:BB:01", {"remove": True})
    assert p["apply"].strip().endswith('remove [find server="dhcp10" mac-address="00:11:32:AA:BB:01" dynamic=no]')
    assert p["revert"].strip() == ('/ip dhcp-server lease add server="dhcp10" mac-address="00:11:32:AA:BB:01" '
                                   'address=192.168.10.10 comment="NAS sklad" dhcp-option="ntp"')
    assert leases.plan(INV, "dhcp10", "00:11:32:AA:BB:01", {"comment": "NAS sklad"})["changes"] == []


def test_plan_new_reservation_for_manual_ip_and_in_pool_warning():
    p = leases.plan(INV, "dhcp10", "B0:BE:76:00:00:01", {"address": "192.168.10.20"})
    assert p["apply"].strip() == ('/ip dhcp-server lease add server="dhcp10" mac-address="B0:BE:76:00:00:01" '
                                  'address=192.168.10.20')
    p = leases.plan(INV, "dhcp10", "02:00:00:00:00:01", {"address": "192.168.10.130"})
    assert "rozsahu poolu" in p["warnings"][0]


def test_plan_guards():
    bad = [
        ("xx", {"address": "192.168.10.30"}, "MAC"),
        ("02:00:00:00:00:01", {"address": "192.168.11.5"}, "podsíti"),
        ("02:00:00:00:00:01", {"address": "192.168.10.1"}, "brány"),
        ("02:00:00:00:00:01", {"address": "192.168.10.255"}, "podsíti"),
        ("02:00:00:00:00:01", {"address": "192.168.10.10"}, "už má"),
        ("02:00:00:00:00:01", {"address": "192.168.10.20"}, "ARP"),
        ("02:00:00:00:00:01", {"address": "192.168.10.31", "options": ["neni"]}, "není"),
        ("02:00:00:00:00:01", {"address": "192.168.10.31", "comment": "čeština"}, "ASCII"),
        ("02:00:00:00:00:01", {"remove": True}, "rezervaci"),
        ("02:00:00:00:00:01", {"address": "192.168.10.31", "new_options": [{"name": "ntp", "code": 42, "type": "ip", "value": "1.1.1.1"}]}, "existuje"),
        ("02:00:00:00:00:01", {"address": "192.168.10.31", "new_options": [{"name": "x", "code": 300, "type": "ip", "value": "1.1.1.1"}]}, "1–254"),
    ]
    for mac, d, msg in bad:
        with pytest.raises(leases.LeaseError, match=msg):
            leases.plan(INV, "dhcp10", mac, d)


def test_option_value():
    assert leases.option_value("ip", "10.0.0.1, 10.0.0.2") == "'10.0.0.1''10.0.0.2'"
    assert leases.option_value("text", "http://wpad/wpad.dat") == "s'http://wpad/wpad.dat'"
    assert leases.option_value("hex", "0xC0A8") == "0xc0a8"
    for k, v in (("ip", "1.2.3"), ("text", "it's"), ("hex", "abc"), ("x", "1")):
        with pytest.raises(leases.LeaseError):
            leases.option_value(k, v)


def test_basis_ignores_dynamic():
    b = leases.config_basis(INV)
    inv = copy.deepcopy(INV)
    inv["dhcp_leases"][1]["active-address"] = "192.168.10.160"
    assert leases.config_basis(inv) == b
    inv["dhcp_leases"][0]["comment"] = "jiny"
    assert leases.config_basis(inv) != b


def test_api_flow():
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "SN-GW", "10.200.1.2", "GW")
        inventory.store(db, tid, gw, copy.deepcopy(INV), [])
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{gw}/dhcp/dhcp10"
        assert c.put(f"/api/admin/tenants/{tid}/client-notes/00-11-32-aa-bb-01", json_body={"note": "zálohy, klíč u Pepy"}).status_code == 200
        assert c.put(f"/api/admin/tenants/{tid}/client-notes/xx", json_body={"note": "a"}).status_code == 400
        v = c.get(base).json()
        nas = next(r for r in v["leases"] if r["mac"] == "00:11:32:AA:BB:01")
        assert nas["note"] == "zálohy, klíč u Pepy" and v["module_mode"] == "observe" and v["oui"] is True
        assert c.get(f"/api/admin/tenants/{tid}/devices/{gw}/dhcp/neni").status_code == 404
        # observe: plán se uloží, schválení odmítne
        r = c.post(base + "/lease", json_body={"mac": "B0:BE:76:12:34:56", "desired": {"address": "192.168.10.30"}, "apply": True})
        assert r.status_code == 400 and "observe" in r.json()["detail"]
        assert c.get(base).json()["pending"][0]["status"] == "draft"
        assert c.post(f"/api/admin/tenants/{tid}/devices/{gw}/modules", json_body={"module": "dhcp", "mode": "manage"}).status_code == 200
        r = c.post(base + "/lease", json_body={"mac": "B0:BE:76:12:34:56", "desired": {"address": "192.168.10.30"}, "apply": True})
        assert r.status_code == 201, r.text
        assert len(c.get(base).json()["pending"]) == 1          # předchozí draft nahrazen
        s = _device_post(app, "/api/checkin", "key-SN-GW", {"sstp": "up"}).text
        assert "make-static" in s and 'name="mtc-revert"' in s
        r = c.post(base + "/lease", json_body={"mac": "B0:BE:76:12:34:56", "desired": {"address": "192.168.10.1"}})
        assert r.status_code == 400 and "brány" in r.json()["detail"]
        # poznámka a výrobce i v seznamu klientů lokality
        cl = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/clients").json()
        assert any(x["mac"] == "00:11:32:AA:BB:01" and x["note"] and x["vendor"] == "Synology Incorporated" for x in cl)
    finally:
        drop_db(name)
