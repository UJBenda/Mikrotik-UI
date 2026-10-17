"""Release notes: parsování changelogu, mezilehlé verze, stažení, relevantní změny."""

from helpers import drop_db
from test_flow import _login
from test_modules import _adopted, _env

from mtcontroller import inventory, releases
from mtcontroller.db import operator_scope

CL_2021 = """What's new in 7.20.1 (2025-Oct-01 10:00):

!) system - changed default behaviour of something important;
*) bridge - fixed VLAN filtering with hw offload on CRS3xx;
*) wifi - improved roaming;
*) lte - fixed modem detection;
*) sstp - fixed reconnect after certificate change;
"""
CL_2022 = """What's new in 7.20.2 (2025-Oct-20 12:00):

*) ovpn - fixed something;
*) dhcpv4-server - fixed lease expiry;
"""


def test_parse():
    p = releases.parse(CL_2021)
    assert p["version"] == "7.20.1" and p["released"] == "2025-Oct-01 10:00"
    assert p["entries"][0] == {"important": True, "component": "system",
                               "text": "changed default behaviour of something important"}
    assert [e["component"] for e in p["entries"]] == ["system", "bridge", "wifi", "lte", "sstp"]


def test_between():
    assert releases.between("7.20.1", "7.20.3") == ["7.20.2", "7.20.3"]
    v = releases.between("7.19.4", "7.20.2")
    assert v[0] == "7.19.5" and "7.19.12" in v and v[-3:] == ["7.20", "7.20.1", "7.20.2"]
    assert releases.between("7.20.2", "7.20.2") == ["7.20.2"]
    assert releases.between("7.20.1", "evil/../x") == []


def test_device_components():
    comps = releases.device_components({"bridges": [{"name": "bridge"}], "wifi": [], "dhcp_servers": [{"name": "d"}]})
    assert {"bridge", "dhcpv4-server", "sstp", "system"} <= comps
    assert "wifi" not in comps and "lte" not in comps


def test_sync_and_notes():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")  # 7.20.1 (stable)
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET ros_version = '7.19.12 (stable)'")
        inv = {"bridges": [{"name": "bridge"}], "dhcp_servers": [{"name": "d"}],
               "update": {"channel": "stable", "latest-version": "7.20.2"}}
        inventory.store(db, tid, gw, inv, [])
        served = {"7.20": "What's new in 7.20 (2025-Sep-01 10:00):\n\n*) wifi - new;\n",
                  "7.20.1": CL_2021, "7.20.2": CL_2022}
        asked = []

        def get(url):
            v = url.split("/")[-2]
            asked.append(v)
            return served[v].encode() if v in served else None

        releases._missing.clear()
        assert releases.sync(db, get=get) == 3
        assert not any(v.startswith("7.19.") for v in asked)  # 7.19.12 = MAX_PATCH, další opravy se nezkouší
        assert releases.sync(db, get=get) == 0  # vše staženo, 404 se pamatují
        n = releases.notes_for(db, "7.19.12", "7.20.2", inv)
        assert n["complete"] and [v["version"] for v in n["versions"]] == ["7.20", "7.20.1", "7.20.2"]
        comps = [e["component"] for e in n["relevant"]]
        assert comps[0] == "system"  # důležité první
        assert "bridge" in comps and "sstp" in comps and "dhcpv4-server" in comps
        assert "lte" not in comps and "wifi" not in comps and "ovpn" not in comps
        assert n["other_count"] == 4  # wifi ×2, lte, ovpn

        c = _login(app)
        r = c.get(f"/api/admin/tenants/{tid}/devices/{gw}/release-notes").json()
        assert r["target"] == "7.20.2" and r["other_count"] == 4
    finally:
        drop_db(name)


def test_sync_offline_and_dead_minor():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")  # 7.20.1
        inventory.store(db, tid, gw, {"update": {"latest-version": "7.21.1"}}, [])

        def offline(url):
            raise OSError("Name or service not known")

        releases._missing.clear()
        assert releases.sync(db, get=offline) == 0  # bez internetu tiše nic
        asked = []

        def get(url):
            v = url.split("/")[-2]
            asked.append(v)
            return b"What's new in x (y):\n\n*) a - b;\n" if v in ("7.20.2", "7.21", "7.21.1") else None

        assert releases.sync(db, get=get) == 3
        # 7.20.3 neexistuje → 7.20.4…7.20.12 se už nezkouší
        assert "7.20.3" in asked and "7.20.4" not in asked
    finally:
        drop_db(name)
