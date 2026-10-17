"""Historie portů (Port Time Machine): změny mezi inventurami."""

import copy

from helpers import drop_db
from test_flow import _login
from test_portcfg import INV, _setup

from mtcontroller import inventory, porthistory


def _inv():
    inv = copy.deepcopy(INV)
    for i in inv["interfaces"]:
        i.update({"running": "true", "link-downs": "0"})
    inv["ethernet_monitor"] = [{"name": "ether1", "status": "link-ok", "rate": "1Gbps", "full-duplex": "true"}]
    inv["neighbors"] = [{"interface": "ether1,bridge", "identity": "core-gw"}]
    return inv


def test_diff():
    a, b = _inv(), _inv()
    b["interfaces"][1]["running"] = "false"                       # ether2 dole
    b["interfaces"][3]["link-downs"] = "3"                        # ether4 třikrát spadl, ale je nahoře
    b["ethernet_monitor"][0]["rate"] = "100Mbps"
    b["ethernet"][2]["poe-out"] = "off"
    b["bridge_ports"][2]["pvid"] = "10"
    b["bridge_vlans"][0]["tagged"] = "ether1,bridge,ether3"
    b["neighbors"] = []
    ev = {(e["port"], e["field"]): e for e in porthistory.diff(a, b)}
    assert set(ev) == {("ether2", "link"), ("ether4", "flaps"), ("ether1", "rate"), ("ether3", "poe"),
                       ("ether3", "pvid"), ("ether3", "tagged"), ("ether1", "neighbor")}
    assert ev[("ether4", "flaps")]["new"] == "3" and ev[("ether3", "tagged")]["new"] == "10,40"
    assert porthistory.describe(ev[("ether1", "neighbor")]) == "soused odpojen (core-gw)"
    assert porthistory.describe(ev[("ether1", "rate")]) == "rychlost: 1Gbps → 100Mbps"
    # sekce chybí v jedné z inventur (výpadek REST) → žádné falešné změny
    c = _inv()
    del c["neighbors"], c["ethernet_monitor"]
    assert porthistory.diff(a, c) == [] and porthistory.diff({}, a) == []


def test_store_and_api():
    db, name, settings, box, app, tid, sw = _setup()  # první inventura (INV) bez running → linky dole
    try:
        inventory.store(db, tid, sw, _inv(), [])      # druhá: linky nahoře
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{sw}"
        h = c.get(base + "/port-history?days=7").json()
        assert {(e["port"], e["text"]) for e in h["events"]} >= {("ether1", "linka nahoře"), ("ether5", "linka nahoře")}
        one = c.get(base + "/port-history?port=ether3").json()["events"]
        assert one and all(e["port"] == "ether3" for e in one)
        assert c.get(base + "/ports/ether3").json()["history"][0]["text"] == "linka nahoře"
        inventory.store(db, tid, sw, _inv(), [])      # beze změny → nic nového
        assert len(c.get(base + "/port-history").json()["events"]) == len(h["events"])
        inventory.retention(db, event_days=0)
        assert c.get(base + "/port-history").json()["events"] == []
    finally:
        drop_db(name)
