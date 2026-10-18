"""Výchozí konfigurace podle role, ohlášení klíčem lokality, zónový firewall, výchozí hesla."""

import copy
import re

import pytest

from mtcontroller import defaults, firewall, fwmatrix, routeros, switchcfg
from test_routeros import _balanced

OPTS = routeros.FetchOpts("yes-without-crl")


def _ok(s):
    assert _balanced(s), s
    s.encode("ascii")


def test_default_scripts():
    for role in defaults.ROLES:
        s = defaults.script(role, "https://c.example/enroll/KEY?role=" + role, OPTS)
        _ok(s)
        assert f'comment="mtc-default:{role}"' in s and 'name="mtc-enroll"' in s
        assert "vlan-filtering=yes" in s and "routerboard get serial-number" in s
        assert "time-zone-name=" in s and 'mac-server set allowed-interface-list=none' in s
        # skript nesmí obsahovat nic specifického pro jeden kus (Netinstall pro všechny)
        assert "mtc-checkin" in s  # jen kontrola v enroll skriptu
    sw = defaults.script("switch", "https://c/enroll/K?role=switch", OPTS)
    assert ':foreach p in=[/interface ethernet find] do={ /interface bridge port add bridge=bridge' in sw
    assert "dhcp-client add interface=bridge" in sw and "firewall" not in sw
    ap = defaults.script("ap", "https://c/enroll/K?role=ap", OPTS)
    assert "/interface wifi disable [find]" in ap
    fw = defaults.script("firewall", "https://c/enroll/K?role=firewall", OPTS)
    assert 'default-name!="ether1"' in fw and "dhcp-client add interface=ether1" in fw
    assert 'interface list add name="WAN"' in fw and 'interface list add name="LAN"' in fw
    assert "action=masquerade out-interface-list=WAN" in fw and "dhcp-server add name=mtc-lan" in fw
    assert fw.index("mtc-fw: LAN -> WAN") < fw.index("mtc-fw: forward vychozi zakaz")
    assert "mac-winbox set allowed-interface-list=LAN" in fw
    with pytest.raises(ValueError):
        defaults.script("router", "https://c/x", OPTS)
    one = defaults.one_liner("https://c/enroll/K/default/switch", OPTS)
    one.encode("ascii")
    assert _balanced(one) and "reset-configuration no-defaults=yes keep-users=yes" in one and "flash/" in one
    # enroll skript se po adopci sám uklidí
    e = defaults.enroll_source("https://c/enroll/K?role=switch", OPTS)
    assert 'script find name="mtc-checkin"' in e and 'scheduler remove [find name="mtc-enroll"]' in e


def test_stage1_role():
    s = routeros.adopt_stage1("https://c/enroll/K/register", OPTS, "switch_ap")
    _ok(s)
    assert '\\"role\\":\\"switch_ap\\"' in s
    with pytest.raises(ValueError):
        routeros.adopt_stage1("https://c/x", OPTS, 'x"; /system reset')


def test_fwmatrix():
    zones = {"WAN": ["ether1"], "LAN": ["bridge"], "HOSTE": ["vlan20"]}
    pol = [{"src": "LAN", "dst": "WAN", "action": "accept"}, {"src": "HOSTE", "dst": "WAN", "action": "accept"},
           {"src": "LAN", "dst": "HOSTE", "action": "accept"}]
    s = fwmatrix.render(zones, pol, ["192.168.88.0/24"])
    _ok(s)
    assert "in-interface-list=\"HOSTE\" out-interface-list=\"LAN\"" not in s  # hosté do LAN nesmí (výchozí zákaz)
    m = fwmatrix.matrix(zones, pol)
    assert m["LAN"]["HOSTE"] == "accept" and m["HOSTE"]["LAN"] == "drop" and m["WAN"]["LAN"] == "drop"
    assert m["LAN"]["LAN"] == "accept" and m["WAN"]["WAN"] == "drop"
    for bad in (({"LAN": ["bridge"]}, [], []),
                ({"WAN": ["ether1"], "lan": ["bridge"]}, [], []),
                ({"WAN": ["ether1"], "LAN": ["bridge"]}, [{"src": "LAN", "dst": "DMZ", "action": "accept"}], []),
                ({"WAN": ["ether1"], "LAN": ["bridge"]}, [{"src": "LAN", "dst": "WAN", "action": "allow"}], []),
                ({"WAN": ["ether1"], "LAN": ["bridge; /x"]}, [], []),
                ({"WAN": ["ether1"], "LAN": ["bridge"]}, [], ["10.0.0.0/33"])):
        with pytest.raises(fwmatrix.MatrixError):
            fwmatrix.render(*bad)


def _rules(script: str) -> list[dict]:
    """Pravidla z vygenerovaného skriptu ve tvaru inventury (pro pohled Matice zón)."""
    out = []
    for ln in script.splitlines():
        if not ln.startswith("/ip firewall filter add "):
            continue
        r = {}
        for k, v in re.findall(r'([a-z-]+)=("[^"]*"|\S+)', ln[len("/ip firewall filter add "):]):
            r[k] = v.strip('"')
        out.append(r)
    return out


def test_default_firewall_in_zone_view():
    fw = defaults.script("firewall", "https://c/enroll/K?role=firewall", OPTS)
    inv = {"if_lists": [{"name": "WAN"}, {"name": "LAN"}],
           "if_list_members": [{"list": "WAN", "interface": "ether1"}, {"list": "LAN", "interface": "bridge"}],
           "fw_filter": _rules(fw)}
    a = firewall.analyze(inv)
    assert a is not None
    text = str(a)
    assert "LAN" in text and "WAN" in text


def test_role_from_inventory():
    assert defaults.role_from_inventory({"bridges": [{"name": "bridge", "comment": "mtc-default:switch_ap"}]}) == "switch_ap"
    assert defaults.role_from_inventory({"bridges": [{"name": "bridge", "comment": "defconf"}]}) is None
    assert defaults.role_from_inventory({"bridges": [{"comment": "mtc-default:hacker"}]}) is None


def test_reconcile_mgmt_static_and_pvid():
    from test_switchcfg import INTENT, _managed_inv

    inv = _managed_inv()
    inv["bridges"] = [{"name": "bridge", "vlan-filtering": "true", "pvid": "1"}]
    m = switchcfg.build(inv, INTENT, {"mgmt": {"mode": "static", "address": "192.168.77.11/24"}})
    r = switchcfg.reconcile(inv, m)
    a, rv = r["apply"], r["revert"]
    _ok(a + rv)
    assert "/interface bridge set bridge pvid=99" in a and "/interface bridge set bridge pvid=1" in rv
    assert 'ip address add address="192.168.77.11/24" interface=bridge' in a
    assert 'ip route add dst-address=0.0.0.0/0 gateway="192.168.77.1" comment="mtc mgmt"' in a
    assert a.index("ip address add") < a.index("dhcp-client remove")  # nová adresa dřív, než zmizí DHCP
    # návrat: nejdřív DHCP zpět, nakonec pryč statická adresa
    assert rv.index("dhcp-client add") < rv.index("ip address remove")
    assert r["additive"] is False and any("statická adresa" in c for c in r["changes"])
    # zpět na DHCP
    inv2 = copy.deepcopy(inv)
    inv2["dhcp_client"] = []
    inv2["addresses"] = [{"address": "192.168.77.11/24", "interface": "bridge"}]
    inv2["routes_default"].append({"dst-address": "0.0.0.0/0", "gateway": "192.168.77.1", "comment": "mtc mgmt"})
    r = switchcfg.reconcile(inv2, switchcfg.build(inv2, INTENT, {"mgmt": {"mode": "dhcp"}}))
    assert "dhcp-client add interface=bridge" in r["apply"] and '/ip route remove [find comment="mtc mgmt"]' in r["apply"]
    assert r["revert"].index("ip route add") < r["revert"].index("dhcp-client remove")


# --- tok přes API (DB) -----------------------------------------------------------------------------

def test_enroll_flow():
    from helpers import AsgiClient, drop_db
    from test_flow import _login
    from test_modules import _env

    from mtcontroller import inventory
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        c = _login(app)
        S = f"/api/admin/tenants/{tid}/sites/{sid}"
        d = c.get(S + "/defaults").json()
        roles = {r["role"]: r for r in d["roles"]}
        assert set(roles) == set(defaults.ROLES) and roles["switch_ap"]["device_role"] == "ap"
        key = re.search(r"/enroll/([A-Za-z0-9_-]+)/default/switch", roles["switch"]["one_liner"]).group(1)
        assert c.get(S + "/defaults").json() == d  # stejný klíč při dalším zobrazení
        f = c.get(S + "/defaults/firewall.rsc")
        assert f.status_code == 200 and f"/enroll/{key}" in f.text and "role=firewall" in f.text and "attachment" in f.headers["content-disposition"]
        with operator_scope(db) as tx:
            pol = str(tx.fetchone("SELECT data FROM site_policy WHERE module = 'enroll'")["data"])
        assert key not in pol  # klíč jen jako hash a šifrovaně
        dev = AsgiClient(app, "checkin.test", client_ip="203.0.113.7")
        # admin host enroll neobslouží, checkin host ano
        assert c.get(f"/enroll/{key}?role=switch").status_code == 404
        assert dev.get(f"/enroll/{key}/default/switch").status_code == 200
        assert dev.get(f"/enroll/spatny-klic?role=switch").status_code == 404
        assert dev.get(f"/enroll/{key}?role=hacker").status_code == 404
        s1 = dev.get(f"/enroll/{key}?role=switch_ap").text
        assert f"/enroll/{key}/register" in s1 and '\\"role\\":\\"switch_ap\\"' in s1
        r = dev.post(f"/enroll/{key}/register", json_body={"serial": "HG409X1", "model": "cAP ax", "version": "7.20.1",
                                                            "role": "switch_ap"})
        assert r.status_code == 200 and 'name="mtc-checkin"' in r.text
        with operator_scope(db) as tx:
            row = tx.fetchone("SELECT id, state, role::text AS role, site_id FROM device WHERE serial = 'HG409X1'")
            assert row["state"] == "pending" and row["role"] == "ap" and str(row["site_id"]) == sid
            assert tx.fetchone("SELECT role FROM device_intent WHERE device_id = %(d)s", {"d": row["id"]})["role"] == "ap"
        did = str(row["id"])
        # klíč je víceúčelový (další kus), čekající lze ohlásit znovu
        assert dev.post(f"/enroll/{key}/register", json_body={"serial": "HG409X1", "model": "cAP ax",
                                                              "version": "7.20.1", "role": "switch_ap"}).status_code == 200
        # rotace: starý klíč přestane platit
        d2 = c.post(S + "/defaults", json_body={}).json()
        assert d2 != d and dev.get(f"/enroll/{key}?role=switch").status_code == 404
        # po adopci: inventura s naší značkou = řízené zařízení bez převzetí resetem
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'adopted', tunnel_ip = '10.200.1.9' WHERE id = %(d)s", {"d": did})
        inventory.store(db, tid, did, {"bridges": [{"name": "bridge", "comment": "mtc-default:switch_ap"}]}, [])
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT managed_at FROM device WHERE id = %(d)s", {"d": did})["managed_at"] is not None
            assert tx.fetchone("SELECT 1 FROM audit_log WHERE action = 'device.managed_default'")

    finally:
        drop_db(name)


def test_factory_passwords():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _env

    from mtcontroller import auth, services
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        c = _login(app)
        T = f"/api/admin/tenants/{tid}"
        assert c.post(T + "/factory-passwords", json_body={"serial": "hg409b1", "password": "QWE7RT9ZUI",
                                                           "note": "cAP ax sklad"}).status_code == 201
        assert c.post(T + "/factory-passwords", json_body={"serial": "x", "password": "a"}).status_code == 400
        r = c.post(T + "/factory-passwords/import",
                   json_body={"csv": "serial;heslo;poznamka\nAB12CD;Heslo1234;hAP ac2\nKK;zkratke\nEF34GH;Pass5678\n"}).json()
        assert r["imported"] == 2 and len(r["errors"]) == 1 and "řádek 3" in r["errors"][0]
        lst = c.get(T + "/factory-passwords").json()
        assert [x["serial"] for x in lst] == ["AB12CD", "EF34GH", "HG409B1"] and "password" not in str(lst).lower()
        assert c.post(T + "/factory-passwords/HG409B1/reveal", json_body={}).json() == {"password": "QWE7RT9ZUI"}
        with operator_scope(db) as tx:
            blob = bytes(tx.fetchone("SELECT ciphertext FROM factory_password WHERE serial = 'HG409B1'")["ciphertext"])
            aud = str(tx.fetchall("SELECT action, after FROM audit_log WHERE action LIKE 'factory_password%%'"))
        assert b"QWE7RT9ZUI" not in blob and "QWE7RT9ZUI" not in aud and "factory_password.reveal" in aud
        # jiný tenant heslo nevidí; technik seznam ano, heslo ne
        other = services.create_tenant(db, settings, "Jina")
        assert c.get(f"/api/admin/tenants/{other['id']}/factory-passwords").json() == []
        with operator_scope(db) as tx:
            uid = auth.create_user(tx, "tech@example.cz", "Technik-heslo-123456")
            tx.execute("INSERT INTO role_binding (user_id, tenant_id, role) VALUES (%(u)s, %(t)s, 'technician')",
                       {"u": uid, "t": tid})
        t = _login(app, "tech@example.cz", "Technik-heslo-123456")
        assert len(t.get(T + "/factory-passwords").json()) == 3
        assert t.post(T + "/factory-passwords/HG409B1/reveal", json_body={}).status_code == 404
        assert t.post(T + "/factory-passwords", json_body={"serial": "ZZ99ZZ", "password": "x1234567"}).status_code == 404
        assert c.delete(T + "/factory-passwords/HG409B1").status_code == 200
        assert c.post(T + "/factory-passwords/HG409B1/reveal", json_body={}).status_code == 404
    finally:
        drop_db(name)
