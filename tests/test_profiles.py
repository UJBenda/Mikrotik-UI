"""Profily portů: validace, shoda s portem, API a audit."""

import pytest
from helpers import drop_db
from test_flow import _login
from test_portcfg import _setup

from mtcontroller import profiles
from mtcontroller.db import operator_scope
from mtcontroller.services import ServiceError


def test_clean_and_matching():
    p = profiles.clean({"name": " AP trunk ", "pvid": "1", "tagged": "40, 10;10", "edge": "auto"})
    assert p == {"name": "AP trunk", "pvid": 1, "tagged": "10,40", "poe": None, "edge": "auto",
                 "poe_watchdog": None, "poe_watchdog_timeout": None}
    assert profiles.clean({"name": "Bez tagů", "tagged": []})["tagged"] == ""
    for bad in ({"name": ""}, {"name": "x"}, {"name": "x", "pvid": 5000}, {"name": "x", "tagged": "1,abc"},
                {"name": "x", "poe": "on"}, {"name": "x", "edge": "no-discover"}, {"name": "a\nb", "pvid": 1}):
        with pytest.raises(ServiceError):
            profiles.clean(bad)
    profs = [{"id": 1, "name": "Kamery", "pvid": 10, "tagged": [], "poe": "forced-on", "edge": None},
             {"id": 2, "name": "Trunk", "pvid": 1, "tagged": ["10", "40"], "poe": None, "edge": None}]
    assert profiles.matching(profs, {"pvid": 1, "tagged": ["40", "10"], "poe": "auto-on"})["id"] == 2
    assert profiles.matching(profs, {"pvid": 10, "tagged": ["40"], "poe": "forced-on"}) is None
    assert profiles.desired(profs[0]) == ({"pvid": 10, "tagged": [], "poe": "forced-on"}, None)
    # PoE hlídač: s cílem se zapne, bez cíle varování
    wd = dict(profs[0], poe_watchdog=True, poe_watchdog_timeout=90)
    assert profiles.desired(wd, "192.168.1.5")[0]["poe_watchdog"] == {"enabled": True, "address": "192.168.1.5", "timeout": 90}
    d, w = profiles.desired(wd)
    assert "poe_watchdog" not in d and "hlídač" in w
    assert profiles.clean({"name": "AP", "poe": "auto-on", "poe_watchdog": True})["poe_watchdog_timeout"] == 60
    for bad in ({"name": "x", "poe_watchdog": True, "poe_watchdog_timeout": 3}, {"name": "x", "poe": "off", "poe_watchdog": True}):
        with pytest.raises(ServiceError):
            profiles.clean(bad)


def test_api_flow():
    db, name, settings, box, app, tid, sw = _setup()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        r = c.post(base + "/port-profiles", json_body={"name": "Trunk 40", "pvid": 1, "tagged": ["40"]})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert c.post(base + "/port-profiles", json_body={"name": "Trunk 40", "pvid": 2}).status_code == 400
        lst = c.get(base + "/port-profiles").json()
        assert [x["name"] for x in lst] == [profiles.TRUNK_NAME, "Trunk 40"]
        assert lst[0]["builtin"] and lst[0]["all_vlans"] and lst[0]["pvid"] == 1
        # vestavěný trunk nejde smazat; „*“ jde uložit i do vlastního profilu
        assert c.request("DELETE", f"{base}/port-profiles/{lst[0]['id']}").status_code == 400
        assert c.post(base + "/port-profiles", json_body={"name": "Vše", "tagged": "*"}).json()["all_vlans"]
        st = c.get(f"{base}/devices/{sw}/ports/ether3").json()  # ether3: pvid 1, tagged 40
        assert st["profile"] == pid and next(x for x in st["profiles"] if x["id"] == pid)["tagged"] == ["40"]
        assert c.get(f"{base}/devices/{sw}/ports/ether2").json()["profile"] is None
        assert c.request("DELETE", f"{base}/port-profiles/{pid}").status_code == 200
        assert c.request("DELETE", f"{base}/port-profiles/{pid}").status_code == 404
        with operator_scope(db) as tx:
            acts = [r["action"] for r in tx.fetchall(
                "SELECT action FROM audit_log WHERE action LIKE %(a)s ORDER BY id", {"a": "port_profile.%"})]
        assert acts == ["port_profile.create", "port_profile.create", "port_profile.delete"]
    finally:
        drop_db(name)
