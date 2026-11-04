"""Čas a DNS pro řízená zařízení: NTP vždy z controlleru (chrony), DNS z administrace."""

import copy
from types import SimpleNamespace

import pytest
from test_switchcfg import INTENT, _managed_inv

from mtcontroller import netsvc, switchcfg
from mtcontroller.services import ServiceError


def test_validate_and_render():
    v = netsvc.validate({"ntp_upstream": "tik.cesnet.cz, tak.cesnet.cz", "dns": ["192.168.77.1"]})
    assert v == {"ntp_upstream": ["tik.cesnet.cz", "tak.cesnet.cz"], "dns": ["192.168.77.1"]}
    assert netsvc.validate({})["ntp_upstream"]  # regionální výchozí
    for bad in ({"ntp_upstream": []}, {"ntp_upstream": ["a;rm -rf /"]}, {"dns": ["dns.google"]}, {"dns": ["1.1.1.1"] * 4}):
        with pytest.raises(ServiceError):
            netsvc.validate(bad)
    src = netsvc.render_sources(v)
    assert "server tik.cesnet.cz iburst\nserver tak.cesnet.cz iburst\n" in src
    local = SimpleNamespace(tunnel=SimpleNamespace(local=True, pool="10.200.0.0/16"))
    remote = SimpleNamespace(tunnel=SimpleNamespace(local=False, pool="10.200.0.0/16"))
    assert "allow 192.168.0.0/16" in netsvc.render_conf(local) and "allow 10.200.0.0/16" in netsvc.render_conf(remote)
    assert "192.168" not in netsvc.render_conf(remote)
    main = "# x\npool 2.debian.pool.ntp.org iburst\nsourcedir /etc/chrony/sources.d\n"
    out = netsvc.disable_default_pool(main)
    assert out.count(netsvc.MARK) == 1 and "sourcedir /etc/chrony/sources.d\n" in out
    assert netsvc.disable_default_pool(out) == out  # podruhé beze změny


def test_managed_switch_uses_controller_ntp_and_admin_dns():
    m = switchcfg.build(INV_DHCP, INTENT, {}, model_name="CRS326", ntp={"upstream": ["10.10.0.10"]},
                        dns=["192.168.77.53"], ntp_ctl=True)
    body = switchcfg.config_body(m)
    assert '/system ntp client set enabled=yes servers="10.10.0.10"' in body
    assert "use-peer-dns=no" in body and '/ip dns set servers="192.168.77.53"' in body
    # sladění: zařízení s NTP z internetu a DNS z DHCP → controller a DNS z administrace (s návratem)
    inv = _managed_inv()
    inv["ntp_client"] = {"enabled": "true", "servers": "tik.cesnet.cz"}
    inv["dns"] = {"servers": "", "allow-remote-requests": "false"}
    inv["dhcp_client"] = [{"interface": "bridge", "comment": "mtc", "use-peer-dns": "true", "status": "bound",
                           "gateway": "192.168.77.1", "address": "192.168.77.50/24"}]
    m = switchcfg.build(inv, INTENT, {}, model_name="CRS326", ntp={"upstream": ["10.10.0.10"]},
                        dns=["192.168.77.53"], ntp_ctl=True)
    r = switchcfg.reconcile(inv, m)
    assert '/system ntp client set enabled=yes servers="10.10.0.10"' in r["apply"]
    assert '/system ntp client set enabled=yes servers="tik.cesnet.cz"' in r["revert"]
    assert '/ip dns set servers="192.168.77.53"' in r["apply"] and 'use-peer-dns=no' in r["apply"]
    assert any(c.startswith("NTP → controller") for c in r["changes"]) and r["apply"].isascii()
    # bez nastavení controlleru (starý náhled) se NTP ani DNS nesladí
    r = switchcfg.reconcile(inv, switchcfg.build(inv, INTENT, {}, model_name="CRS326"))
    assert "ntp client" not in r["apply"] and "/ip dns set" not in r["apply"]
    b = switchcfg.basis(inv)
    inv["ntp_client"]["servers"] = "10.10.0.10"
    assert switchcfg.basis(inv) != b


INV_DHCP = copy.deepcopy(__import__("test_switchcfg").INV)


def test_api_and_root_apply(tmp_path):
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _env

    db, name, settings, box, app, tid, sid = _env()
    settings.paths.data_dir = tmp_path
    try:
        c = _login(app)
        r = c.get("/api/admin/system/netsvc").json()
        assert r["netsvc"]["ntp_upstream"] and r["controller_ip"] == settings.tunnel.controller_ip
        assert c.put("/api/admin/system/netsvc", json_body={"dns": ["dns.google"]}).status_code == 400
        r = c.put("/api/admin/system/netsvc", json_body={"ntp_upstream": ["ntp.firma.cz"], "dns": ["10.0.0.53"]})
        assert r.status_code == 200, r.text
        assert netsvc.request_path(settings.paths.data_dir).exists()
        assert netsvc.device_view(db, settings) == {"ntp": [settings.tunnel.controller_ip], "dns": ["10.0.0.53"]}
        # root: chrony podle DB, výchozí pool vypnutý, restart; stav pro web
        main = tmp_path / "chrony.conf"
        main.write_text("pool 2.debian.pool.ntp.org iburst\nconfdir /etc/chrony/conf.d\n")
        calls = []

        def run(*cmd):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        assert netsvc.apply_request(db, settings, sources=tmp_path / "s" / "m.sources", conf=tmp_path / "c" / "m.conf",
                                    main=main, run=run) == 0
        assert (tmp_path / "s" / "m.sources").read_text().endswith("server ntp.firma.cz iburst\n")
        assert netsvc.MARK in main.read_text() and (tmp_path / "chrony.conf.mtc-orig").exists()
        assert ("systemctl", "restart", "chrony") in calls
        assert not netsvc.request_path(settings.paths.data_dir).exists()
        assert c.get("/api/admin/system/netsvc").json()["status"]["ok"] is True
        from mtcontroller.db import operator_scope

        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT 1 FROM audit_log WHERE action = 'system.netsvc'")
    finally:
        drop_db(name)
