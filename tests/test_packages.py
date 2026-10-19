"""Hlídání Wi-Fi ovladače (wifi-qcom / wifi-qcom-ac / wireless) a verzí balíčků."""

import pytest

from mtcontroller import packages
from test_routeros import _balanced


def _inv(board, arch, pkgs, radios=True, ver="7.20.1"):
    inv = {"resource": {"board-name": board, "architecture-name": arch, "version": f"{ver} (stable)"},
           "packages": [{"name": "routeros", "version": ver}] + [{"name": p, "version": ver} for p in pkgs]}
    if radios:
        inv["wifi" if any(p.startswith("wifi") for p in pkgs) else "wireless"] = [{"name": "wlan1"}]
    return inv


def test_wifi_status():
    ok = packages.wifi_status(_inv("cAP ax", "arm64", ["wifi-qcom"]))
    assert ok["ok"] and ok["chip"] == "ax" and ok["required"] == "wifi-qcom"
    ac_old = packages.wifi_status(_inv("RBcAPGi-5acD2nD", "arm", ["wireless"]))
    assert not ac_old["ok"] and ac_old["chip"] == "ac" and ac_old["fix"] == {"install": "wifi-qcom-ac", "remove": ["wireless"]}
    assert packages.wifi_status(_inv("hAP ac^2", "arm", ["wifi-qcom-ac"]))["ok"]
    lite = packages.wifi_status(_inv("RB952Ui-5ac2nD", "mipsbe", ["wireless"]))
    assert not lite["ok"] and lite["chip"] == "legacy" and lite["fix"] is None and "jen jako switch" in lite["problem"]
    both = packages.wifi_status(_inv("hAP ax^3", "arm64", ["wifi-qcom", "wifi-qcom-ac"]))
    assert not both["ok"] and "oba" in both["problem"]
    sw = packages.wifi_status(_inv("CRS326-24G-2S+", "arm", [], radios=False))
    assert sw["ok"] and sw["chip"] is None


def test_version_mismatch():
    inv = _inv("cAP ax", "arm64", ["wifi-qcom"])
    assert packages.version_mismatch(inv) == []
    inv["packages"][1]["version"] = "7.19.4"
    assert packages.version_mismatch(inv) == ["wifi-qcom 7.19.4"]


def test_switch_body():
    s = packages.switch_body(_inv("RBcAPGi-5acD2nD", "arm", ["wireless"], ver="7.20.1"))
    assert _balanced(s)
    s.encode("ascii")
    assert 'routeros/7.20.1/wifi-qcom-ac-7.20.1-arm.npk' in s and "/system package uninstall wireless" in s
    with pytest.raises(ValueError):
        packages.switch_body(_inv("cAP ax", "arm64", ["wifi-qcom"]))
    bad = _inv("RBcAPGi-5acD2nD", "arm", ["wireless"])
    bad["resource"]["architecture-name"] = "arm; /system reset"
    with pytest.raises(ValueError):
        packages.switch_body(bad)


def test_command_and_alert():
    from helpers import drop_db
    from test_flow import _device_post
    from test_modules import _adopted, _env

    from mtcontroller import alerts, inventory, services
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap")
        inventory.store(db, tid, ap, _inv("RBcAPGi-5acD2nD", "arm", ["wireless"]), [])
        with operator_scope(db) as tx:
            found = [a for a in alerts.conditions(tx) if a.kind == "wifi_package"]
        assert len(found) == 1 and "wifi-qcom-ac" in found[0].message
        cid = services.request_command(db, tid, ap, "wifi_package")
        s = _device_post(app, "/api/checkin", "key-AP1", {"sstp": "up"}).text
        assert "wifi-qcom-ac-7.20.1-arm.npk" in s and "/system reboot" in s and s.index("&status=") < s.index("/system reboot")
        assert cid
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        inventory.store(db, tid, sw, _inv("CRS326", "arm", [], radios=False), [])
        with pytest.raises(services.ServiceError):
            services.request_command(db, tid, sw, "wifi_package")
    finally:
        drop_db(name)
