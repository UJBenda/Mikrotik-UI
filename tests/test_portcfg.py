"""Změny portů s automatickým návratem (commit-confirm)."""

import re

import pytest
from helpers import drop_db
from test_flow import _device_post, _login
from test_modules import _adopted, _env

from mtcontroller import inventory, portcfg, routeros
from mtcontroller.db import operator_scope
from test_routeros import _balanced

INV = {
    "interfaces": [{"name": f"ether{i}", "type": "ether", "disabled": "false", "comment": "kamera" if i == 3 else ""}
                   for i in range(1, 6)],
    "ethernet": [{"name": "ether1"}, {"name": "ether2"}, {"name": "ether3", "poe-out": "auto-on"},
                 {"name": "ether4", "poe-out": "off"}, {"name": "ether5"}],
    "bridges": [{"name": "bridge", "vlan-filtering": "true"}],
    "bridge_ports": [{"bridge": "bridge", "interface": f"ether{i}", "pvid": "1", "edge": "auto"} for i in range(1, 5)],
    "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "10", "tagged": "ether1,bridge"},
                     {"bridge": "bridge", "vlan-ids": "20,30", "tagged": "ether1"},
                     {"bridge": "bridge", "vlan-ids": "40", "tagged": "ether1,ether3"}],
    "stp_ports": [{"interface": "ether1", "role": "root-port"}],
}


def test_port_state_and_plan():
    st = portcfg.port_state(INV, "ether3")
    assert st["tagged"] == ["40"] and st["poe"] == "auto-on" and st["pvid"] == 1 and st["comment"] == "kamera"
    p = portcfg.plan(INV, "ether3", {"pvid": 10, "tagged": ["10", "50"], "poe": "off", "comment": "kamera vjezd"})
    a, r = p["apply"], p["revert"]
    assert "pvid=10" in a and "poe-out=off" in a and 'comment="kamera vjezd"' in a
    assert 'vlan-ids=10 dynamic=no] tagged="ether1,bridge,ether3"' in a  # přidání do existujícího řádku
    assert 'vlan add bridge="bridge" vlan-ids=50 tagged="ether3" comment="mtc"' in a  # nový řádek
    assert 'vlan-ids=40 dynamic=no] tagged="ether1"' in a  # odebrání z 40
    # návrat vrací vše a v opačném pořadí
    assert 'vlan remove [find bridge="bridge" vlan-ids=50 comment="mtc"]' in r and "pvid=1" in r
    assert 'poe-out=auto-on' in r and 'comment="kamera"' in r
    assert r.splitlines()[0].startswith("/interface bridge vlan") and r.splitlines()[-1].endswith('comment="kamera"')
    assert {c["field"] for c in p["changes"]} == {"pvid", "tagged", "poe", "comment"}


def test_plan_guards():
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV, "ether3", {"tagged": ["20"]})  # řádek "20,30" se neupravuje
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV, "ether5", {"pvid": 10})  # není v bridgi
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV, "ether1", {"poe": "off"})  # bez PoE
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV, "ether3", {"pvid": 5000})
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV, "ether3", {"comment": 'a"; /system reset-configuration'.replace('"', "”")})
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV, "ether9; /x", {"disabled": True})
    rng = dict(INV, bridge_vlans=INV["bridge_vlans"] + [{"bridge": "bridge", "vlan-ids": "100-200", "tagged": "ether2"}])
    with pytest.raises(portcfg.PortError):
        portcfg.plan(rng, "ether3", {"tagged": ["40", "150"]})  # 150 je v rozsahu 100-200
    assert portcfg.plan(INV, "ether3", {"pvid": 1})["changes"] == []
    w = portcfg.plan(INV, "ether1", {"disabled": True}, uplinks={"ether1"})["warnings"]
    assert w and "uplink" in w[0]


def test_safe_scripts_are_valid():
    body = routeros.safe_apply_body("/interface ethernet set [find name=\"ether3\"] disabled=yes",
                                    "/interface ethernet set [find name=\"ether3\"] disabled=no",
                                    "https://c/api/checkin/ack?id=7&status=reverted", "KEY", routeros.FetchOpts(), 5)
    for s in (routeros.wrap_command("https://c/api/checkin/ack?id=7", "KEY", body, routeros.FetchOpts()),
              routeros.wrap_command("https://c/api/checkin/ack?id=8", "KEY", routeros.CONFIRM_BODY, routeros.FetchOpts())):
        assert _balanced(s), s
        s.encode("ascii")
    # revert a tik se uloží dřív, než se změna provede
    assert body.index('name="mtc-revert"') < body.index("disabled=yes")
    assert body.index('/system scheduler add name="mtc-confirm"') < body.index("disabled=yes")
    assert "$mtcConfirmTicks >= 5" in body and "status=reverted" in body
    with pytest.raises(ValueError):
        routeros.safe_apply_body("/x", "/y", "https://c", "K", routeros.FetchOpts(), 60)


def _setup():
    db, name, settings, box, app, tid, sid = _env()
    sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
    inventory.store(db, tid, sw, INV, [])
    return db, name, settings, box, app, tid, sw


def _status(db, pid):
    with operator_scope(db) as tx:
        return tx.fetchone("SELECT status::text AS s FROM config_plan WHERE id = %(i)s", {"i": pid})["s"]


def test_flow_apply_confirm():
    db, name, settings, box, app, tid, sw = _setup()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}"
        st = c.get(base + "/ports/ether3").json()
        assert st["tagged"] == ["40"] and st["known_vlans"] == ["10", "40"] and st["module_mode"] == "observe"
        # v observe se plán uloží, ale schválení odmítne
        r = c.post(base + "/ports/ether3/plan", json_body={"desired": {"pvid": 10}, "apply": True})
        assert r.status_code == 400 and "observe" in r.json()["detail"]
        assert c.post(base + "/modules", json_body={"module": "ports", "mode": "manage"}).status_code == 200
        r = c.post(base + "/ports/ether3/plan", json_body={"desired": {"pvid": 10, "edge": "yes"}, "apply": True})
        assert r.status_code == 201, r.text
        pid, cid = r.json()["plan_id"], r.json()["command_id"]
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert 'name="mtc-revert"' in s and "pvid=10" in s and "edge=yes" in s
        assert re.search(r'source="[^"]*pvid=1', s.replace('\\"', "'"))  # návrat uložený na zařízení
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        assert _status(db, pid) == "confirming"
        # další check-in = cesta ke správě funguje → potvrzení
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert 'scheduler remove [find name="mtc-confirm"]' in s and "pvid=" not in s
        ccid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={ccid}&status=done", "key-SW1")
        assert _status(db, pid) == "applied"
        assert "nic k provedeni" in _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
    finally:
        drop_db(name)


def test_flow_rolled_back_and_poe_cycle():
    from mtcontroller import alerts

    db, name, settings, box, app, tid, sw = _setup()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}"
        c.post(base + "/modules", json_body={"module": "ports", "mode": "manage"})
        r = c.post(base + "/ports/ether1/plan", json_body={"desired": {"disabled": True}, "apply": True}).json()
        assert any("uplink" in w for w in r["warnings"])
        pid, cid = r["plan_id"], r["command_id"]
        _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"})
        # zařízení ztratí spojení, ack nepřijde; po 5 min se samo vrátí a nahlásí to
        assert _device_post(app, f"/api/checkin/ack?id={cid}&status=reverted", "key-SW1").text == "ok\n"
        assert _status(db, pid) == "rolled_back"
        with operator_scope(db) as tx:
            assert any(a.kind == "change_rolled_back" for a in alerts.conditions(tx))
            assert tx.fetchone("SELECT 1 FROM audit_log WHERE action = 'plan.rolled_back'")
        # cizí id příkazu nic nezmění
        assert _device_post(app, "/api/checkin/ack?id=999999&status=reverted", "key-SW1").text == "ignored\n"

        r = c.post(base + "/commands", json_body={"kind": "poe_cycle", "port": "ether3"})
        assert r.status_code == 201, r.text
        assert c.post(base + "/commands", json_body={"kind": "poe_cycle", "port": "ether1"}).status_code == 400
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up"}).text
        assert 'poe power-cycle [find name="ether3"] duration=5s' in s
    finally:
        drop_db(name)


INV2 = dict(INV, poe=[{"name": "ether3", "poe-out": "auto-on", "power-cycle-ping-enabled": "false",
                       "power-cycle-ping-address": "", "power-cycle-ping-timeout": "5s"}],
            switch=[{"name": "switch1", "type": "Marvell-98DX3236"}],
            ethernet=[dict(e, switch="switch1") for e in INV["ethernet"]],
            bridge_ports=[dict(b, learn="auto") for b in INV["bridge_ports"]])


def test_poe_watchdog_plan():
    st = portcfg.port_state(INV2, "ether3")
    assert st["poe_watchdog"] == {"enabled": False, "address": "", "timeout": 5}
    p = portcfg.plan(INV2, "ether3", {"poe_watchdog": {"enabled": True, "address": "192.168.88.20", "timeout": 120}})
    assert ('/interface ethernet poe set [find name="ether3"] power-cycle-ping-enabled=yes '
            'power-cycle-ping-address="192.168.88.20" power-cycle-ping-timeout=120s') in p["apply"]
    assert p["revert"].strip().endswith("power-cycle-ping-enabled=no")
    assert "PoE hlídač" in p["summary"]
    # MAC (MAC-ping z portu) je platný cíl
    assert "AA:BB:CC:00:11:22" in portcfg.plan(INV2, "ether3", {"poe_watchdog": {"enabled": True, "address": "aa:bb:cc:00:11:22"}})["apply"]
    for bad in ({"enabled": True, "address": "evil; /system reset"}, {"enabled": True, "address": "10.0.0.1", "timeout": 5}):
        with pytest.raises(portcfg.PortError):
            portcfg.plan(INV2, "ether3", {"poe_watchdog": bad})
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV2, "ether1", {"poe_watchdog": {"enabled": True, "address": "10.0.0.1"}})  # bez PoE
    assert portcfg.duration_s("1m30s") == 90 and portcfg.duration_s("00:02:00") == 120 and portcfg.duration_s("") is None


def test_port_lock_hw_and_sw():
    p = portcfg.plan(INV2, "ether3", {"lock": "aa:bb:cc:00:11:22"})
    a = p["apply"]
    assert '/interface bridge port set [find interface="ether3" dynamic=no] learn=no' in a
    assert 'bridge host add bridge="bridge" mac-address=AA:BB:CC:00:11:22 interface="ether3" vid=1 comment="mtc-lock ether3"' in a
    assert 'switch rule add switch="switch1" ports="ether3" src-mac-address=AA:BB:CC:00:11:22/FF:FF:FF:FF:FF:FF' in a
    assert 'new-dst-ports="" comment="mtc-lock ether3"' in a
    r = p["revert"].splitlines()
    assert r[0].startswith("/interface ethernet switch rule remove") and r[-1].endswith("learn=auto")
    assert not p["warnings"]
    # switch bez ACL v čipu: bridge filtr + varování
    sw = dict(INV2, switch=[{"name": "switch1", "type": "Atheros-8327"}])
    p = portcfg.plan(sw, "ether3", {"lock": "AA:BB:CC:00:11:22"})
    assert "bridge filter add chain=forward in-interface=\"ether3\" src-mac-address=!AA:BB:CC:00:11:22" in p["apply"]
    assert p["warnings"]
    # zamčený port → odemknutí a změna MAC
    locked = dict(INV2, bridge_hosts=[{"mac-address": "AA:BB:CC:00:11:22", "on-interface": "ether3", "dynamic": "false",
                                       "local": "false"}],
                  switch_rules=[{"comment": "mtc-lock ether3"}],
                  bridge_ports=[dict(b, learn="no" if b["interface"] == "ether3" else "auto") for b in INV["bridge_ports"]])
    assert portcfg.port_state(locked, "ether3")["lock"] == "AA:BB:CC:00:11:22"
    un = portcfg.plan(locked, "ether3", {"lock": ""})
    assert "learn=auto" in un["apply"] and "learn=no" in un["revert"] and "mac-address=AA:BB:CC:00:11:22" in un["revert"]
    mv = portcfg.plan(locked, "ether3", {"lock": "AA:BB:CC:00:11:33"})
    a = mv["apply"].splitlines()
    assert a.index('/interface bridge host remove [find comment="mtc-lock ether3"]') < \
        next(i for i, x in enumerate(a) if "AA:BB:CC:00:11:33" in x)
    rv = mv["revert"].splitlines()
    # návrat: nejdřív pryč nový zámek, pak zpět starý
    assert next(i for i, x in enumerate(rv) if "remove" in x) < next(i for i, x in enumerate(rv) if "AA:BB:CC:00:11:22" in x)
    with pytest.raises(portcfg.PortError):
        portcfg.plan(INV2, "ether3", {"lock": "nesmysl"})
    for s in (p["apply"], p["revert"], mv["apply"], mv["revert"]):
        assert _balanced(s) and s.isascii()


def test_plan_many_aggregates_vlan_membership():
    items = {"ether2": {"tagged": ["40", "50"], "pvid": 20}, "ether3": {"tagged": ["40", "50"]}, "ether4": {"tagged": ["50"]}}
    p = portcfg.plan_many(INV, items)
    a = p["apply"]
    # 50 neexistuje: první port řádek založí, další se přidají – nepřepíšou se navzájem
    assert a.count('vlan add bridge="bridge" vlan-ids=50') == 1
    assert 'vlan-ids=50 dynamic=no] tagged="ether2,ether3"' in a and 'vlan-ids=50 dynamic=no] tagged="ether2,ether3,ether4"' in a
    assert 'vlan-ids=40 dynamic=no] tagged="ether1,ether3,ether2"' in a
    assert p["summary"].startswith("3 portů") and {c["port"] for c in p["changes"]} == {"ether2", "ether3", "ether4"}
    r = p["revert"].splitlines()
    # návrat od posledního portu: řádek 50 se smaže až nakonec
    assert r[-1].startswith("/interface bridge port set") or "vlan-ids=50" in "".join(r[-3:])
    assert r.index('/interface bridge vlan remove [find bridge="bridge" vlan-ids=50 comment="mtc"]') > \
        r.index('/interface bridge vlan set [find bridge="bridge" vlan-ids=50 dynamic=no] tagged="ether2"')
    assert portcfg.plan_many(INV, {"ether3": {"pvid": 1}})["changes"] == []
