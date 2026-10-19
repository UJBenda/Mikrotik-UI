"""Klienti: popisek, ikona a loga; DHCP rezervace ze seznamu klientů; globální nastavení sítě DHCP;
klienti v infrastruktuře."""

import base64
import copy

import pytest

from test_leases import INV

from mtcontroller import clientmeta, leases
from mtcontroller.services import ServiceError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
NET = {"address": "192.168.10.0/24", "gateway": "192.168.10.1", "dns-server": "192.168.10.1", "dhcp-option": ""}


def test_vendor_key_and_sniff():
    assert clientmeta.vendor_key("Apple, Inc.") == "apple"
    assert clientmeta.vendor_key("Synology Incorporated") == "synology"
    assert clientmeta.vendor_key("TP-LINK TECHNOLOGIES CO.,LTD.") == "tp link"
    assert clientmeta.sniff(PNG) == "image/png" and clientmeta.sniff(b"\xff\xd8\xff\xe0xx") == "image/jpeg"
    assert clientmeta.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    for bad in (b"<svg onload=alert(1)>", b"GIF89a", b"<html>"):
        with pytest.raises(ServiceError):
            clientmeta.sniff(bad)


def test_network_plan():
    inv = dict(copy.deepcopy(INV), dhcp_networks=[NET])
    v = leases.server_view(inv, "dhcp10", {}, {})
    assert v["network"] == {"address": "192.168.10.0/24", "gateway": "192.168.10.1", "dns": ["192.168.10.1"],
                            "ntp": [], "domain": "", "options": []}
    p = leases.network_plan(inv, "dhcp10", {"dns": ["192.168.10.1", "1.1.1.1"], "domain": "firma.local", "options": ["ntp", "tftp"],
                                            "new_options": [{"name": "tftp", "code": 66, "type": "text", "value": "10.0.0.5"}]})
    assert '/ip dhcp-server option add name="tftp" code=66' in p["apply"]
    assert ('/ip dhcp-server network set [find address="192.168.10.0/24"] dns-server="192.168.10.1,1.1.1.1" '
            'domain="firma.local" dhcp-option="ntp,tftp"') in p["apply"]
    assert 'dns-server="192.168.10.1" domain="" dhcp-option=""' in p["revert"]
    assert p["revert"].index("network set") < p["revert"].index("option remove")
    p["apply"].encode("ascii")
    assert leases.network_plan(inv, "dhcp10", {"dns": ["192.168.10.1"]})["changes"] == []
    for bad in ({"dns": ["1.2.3"]}, {"domain": "a b"}, {"options": ["neni"]}, {"dns": ["1.1.1.1"] * 5}):
        with pytest.raises(leases.LeaseError):
            leases.network_plan(inv, "dhcp10", bad)
    with pytest.raises(leases.LeaseError):
        leases.network_plan(copy.deepcopy(INV), "dhcp10", {"dns": []})   # síť chybí


def test_server_for_client():
    invs = {"gw": copy.deepcopy(INV), "sw": {}}
    assert leases.server_for_client(invs, "b0-be-76-12-34-56", None) == ("gw", "dhcp10")
    assert leases.server_for_client(invs, "AA:AA:AA:00:00:09", "192.168.10.77") == ("gw", "dhcp10")   # podle podsítě
    assert leases.server_for_client(invs, "AA:AA:AA:00:00:09", "10.9.9.9") is None


def test_client_meta_logos_and_dhcp_flow():
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "SN-GW", "10.200.1.2", "GW")
        inventory.store(db, tid, gw, dict(copy.deepcopy(INV), dhcp_networks=[NET]), [])
        with operator_scope(db) as tx:
            tx.execute("""INSERT INTO client (tenant_id, site_id, mac, ip, hostname, device_id, device, port)
                          VALUES (%(t)s, %(s)s, 'B0:BE:76:12:34:56', '192.168.10.150', 'tiskarna', %(d)s, 'GW', 'ether7')""",
                       {"t": tid, "s": sid, "d": gw})
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        # popisek a ikona; poznámka zůstane
        assert c.put(T + "/client-notes/B0:BE:76:12:34:56", json_body={"note": "u okna"}).status_code == 200
        r = c.put(T + "/client-notes/B0:BE:76:12:34:56", json_body={"label": "Tiskárna 2. patro", "icon": "printer"})
        assert r.status_code == 200 and r.json() == {"mac": "B0:BE:76:12:34:56", "note": "u okna", "label": "Tiskárna 2. patro",
                                                     "icon": "printer"}
        assert c.put(T + "/client-notes/B0:BE:76:12:34:56", json_body={"icon": "<script>"}).status_code == 400
        assert c.put(T + "/client-notes/B0:BE:76:12:34:56", json_body={"label": "x\ny"}).status_code == 400
        # logo výrobce: jen rastr do 64 KiB; GET s vlastní přísnou CSP a soukromou cache
        b64 = base64.b64encode(PNG).decode()
        r = c.put(T + "/client-logos/vendor/Velmi Specificky s.r.o.", json_body={"data": b64})
        assert r.status_code == 200, r.text
        vk = r.json()["key"]
        assert c.put(T + "/client-logos/vendor/x", json_body={"data": base64.b64encode(b"<svg/>").decode()}).status_code == 400
        assert c.put(T + "/client-logos/vendor/x", json_body={"data": base64.b64encode(PNG + b"\x00" * 70000).decode()}).status_code in (400, 413)
        assert c.put(T + "/client-logos/nic/x", json_body={"data": b64}).status_code == 400
        g = c.get(T + f"/client-logos/vendor/{vk}")
        assert g.status_code == 200 and g.content == PNG
        assert g.headers["content-type"] == "image/png" and "sandbox" in g.headers["content-security-policy"]
        assert g.headers["cache-control"].startswith("private") and g.headers["x-content-type-options"] == "nosniff"
        # klient v infrastruktuře: s MAC, popiskem, ikonou a logem výrobce
        inf = c.get(T + f"/sites/{sid}/infra").json()
        node = next(n for n in inf["nodes"] if n["id"] == gw)
        cl = node["clients"][0]
        assert cl["mac"] == "B0:BE:76:12:34:56" and cl["label"] == "Tiskárna 2. patro" and cl["icon"] == "printer"
        assert cl["logo"].startswith(f"vendor/{vk}?v=")
        # logo klienta má přednost
        assert c.put(T + "/client-logos/client/b0-be-76-12-34-56", json_body={"data": b64}).status_code == 200
        cl = c.get(T + f"/sites/{sid}/infra").json()["nodes"][0]["clients"][0]
        assert cl["logo"].startswith("client/B0:BE:76:12:34:56?v=")
        assert c.delete(T + "/client-logos/client/B0:BE:76:12:34:56").status_code == 200
        assert {x["key"] for x in c.get(T + "/client-logos").json()["logos"]} == {vk}
        # DHCP ze seznamu klientů: najde server, globální nastavení sítě jako plán s návratem
        d = c.get(T + f"/sites/{sid}/clients/B0:BE:76:12:34:56/dhcp").json()
        assert d == {"device_id": gw, "server": "dhcp10", "device": "GW", "mac": "B0:BE:76:12:34:56"}
        assert c.get(T + f"/sites/{sid}/clients/AA:00:00:00:00:01/dhcp").status_code == 404
        assert c.post(T + f"/devices/{gw}/modules", json_body={"module": "dhcp", "mode": "manage"}).status_code == 200
        r = c.post(T + f"/devices/{gw}/dhcp/dhcp10/network", json_body={"desired": {"dns": ["1.1.1.1"], "options": ["ntp"]}, "apply": True})
        assert r.status_code == 201, r.text
        s = _device_post(app, "/api/checkin", "key-SN-GW", {"sstp": "up"}).text
        assert 'dns-server="1.1.1.1" dhcp-option="ntp"' in s and 'name="mtc-revert"' in s
        with operator_scope(db) as tx:
            acts = {r["action"] for r in tx.fetchall("SELECT action FROM audit_log")}
        assert {"client.meta", "client.logo", "client.logo_delete", "plan.create"} <= acts
    finally:
        drop_db(name)
