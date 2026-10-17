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
