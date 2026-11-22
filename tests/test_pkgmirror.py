"""Izolace řízených zařízení (lokální režim) a balíčky RouterOS přes controller."""

import copy
import tempfile
from pathlib import Path

from test_routeros import _balanced
from test_switchcfg import INTENT, _managed_inv

from mtcontroller import pkgmirror, switchcfg

INV = {"resource": {"version": "7.20.1 (stable)", "architecture-name": "arm"},
       "packages": [{"name": "routeros", "version": "7.20.1"}, {"name": "wifi-qcom", "version": "7.20.1"},
                    {"name": "container", "version": "7.20.1", "disabled": "true"}]}


def test_files_urls_and_scripts():
    assert pkgmirror.upgrade_files(INV, "7.20.5") == ["routeros-7.20.5-arm.npk", "wifi-qcom-7.20.5-arm.npk"]
    assert pkgmirror.url_for("wifi-qcom-7.20.5-arm.npk") == "https://download.mikrotik.com/routeros/7.20.5/wifi-qcom-7.20.5-arm.npk"
    for bad in ("../x.npk", "routeros-7.20.5-arm.npk;rm", "routeros.npk"):
        assert not pkgmirror.FILE_RE.match(bad)
    try:
        pkgmirror.upgrade_files({**INV, "resource": {"architecture-name": "x86", "version": "7.20"}}, "7.20.5")
        raise AssertionError("x86 prošlo")
    except ValueError:
        pass
    b = pkgmirror.upgrade_body("7.20.5", pkgmirror.upgrade_files(INV, "7.20.5"), "https://ctl.lan", "K", "yes-without-crl")
    assert '/api/checkin/pkg/routeros-7.20.5-arm.npk' in b and '"Authorization: Bearer K"' in b and 'flash/' in b
    assert b.isascii() and _balanced("{\n" + b + "}\n")
    assert "/system reboot" in pkgmirror.upgrade_tail("7.20.5")
    # místo na disku: se známou velikostí balíčků kontrola předem (velikost + rezerva) a po stažení
    assert "free-hdd-space] < 65536" in b and "potreba" not in b
    files = pkgmirror.upgrade_files(INV, "7.20.5")
    dd = Path(tempfile.mkdtemp())
    assert pkgmirror.files_size(dd, files) is None                      # balíčky u controlleru ještě nejsou
    pkgmirror.pkg_dir(dd).mkdir(parents=True)
    for f in files:
        (pkgmirror.pkg_dir(dd) / f).write_bytes(b"x" * 1048576)
    need = pkgmirror.files_size(dd, files)
    assert need == 1048576 * len(files)
    b2 = pkgmirror.upgrade_body("7.20.5", files, "https://ctl.lan", "K", "yes-without-crl", need=need)
    assert f"free-hdd-space] < {need + 262144}" in b2 and "malo mista na disku pro balicky" in b2
    assert b2.index("malo mista") < b2.index("/tool fetch") < b2.index("disk je plny")
    assert b2.isascii() and _balanced("{\n" + b2 + "}\n")
    # stažení: hotové soubory se nestahují znovu, prázdný soubor se zahodí
    d = Path(tempfile.mkdtemp())
    got = []

    def get(url, dst):
        got.append(url)
        dst.write_bytes(b"x" * (10 if "wifi" in url else 4096))

    assert pkgmirror.fetch(d, ["routeros-7.20.5-arm.npk", "wifi-qcom-7.20.5-arm.npk"], get) == 1
    assert pkgmirror.ready(d, ["routeros-7.20.5-arm.npk"]) and not pkgmirror.ready(d, ["wifi-qcom-7.20.5-arm.npk"])
    assert pkgmirror.fetch(d, ["routeros-7.20.5-arm.npk"], get) == 0 and len(got) == 2
    assert pkgmirror.latest(lambda url, dst: b"7.20.5 1761000000" if "stable" in url else b"") == {"stable": "7.20.5"}


def test_isolation_rules_in_model_and_reconcile():
    m = switchcfg.build(_managed_inv(), INTENT, {}, model_name="CRS326", ntp={"upstream": ["192.168.88.10"]},
                        dns=["8.8.8.8"], ntp_ctl=True, isolate=["192.168.88.10", "8.8.8.8"])
    body = switchcfg.config_body(m)
    assert 'chain=output dst-address=192.168.0.0/16 action=accept comment="mtc isolate"' in body
    assert 'chain=output dst-address=8.8.8.8 action=accept' in body and "dst-address=192.168.88.10 " not in body
    assert body.index('chain=output dst-address-type=local') < body.index('chain=output action=drop comment="mtc isolate"')
    assert "/ipv6 firewall filter add chain=output action=drop" in body and body.isascii()
    # řízené zařízení bez pravidel → doplnit; s výjimkou (isolate None) → odebrat
    inv = _managed_inv()
    r = switchcfg.reconcile(inv, m)
    assert any(c.startswith("izolace: zařízení jen") for c in r["changes"]) and 'action=drop comment="mtc isolate"' in r["apply"]
    assert r["revert"].startswith('/ip firewall filter remove [find comment="mtc isolate"]') or "remove [find comment=" in r["revert"]
    inv["fw_filter"] = [{"chain": "output", "dst-address-type": t, "action": "accept", "comment": "mtc isolate"}
                        for t in ("local", "broadcast", "multicast")] + \
        [{"chain": "output", "dst-address": n, "action": "accept", "comment": "mtc isolate"} for n in switchcfg.ISO_PRIVATE] + \
        [{"chain": "output", "dst-address": "8.8.8.8", "action": "accept", "comment": "mtc isolate"},
         {"chain": "output", "action": "drop", "comment": "mtc isolate"}]
    assert not any("izolace" in c for c in switchcfg.reconcile(inv, m)["changes"])
    off = switchcfg.build(inv, INTENT, {}, model_name="CRS326", ntp={"upstream": ["192.168.88.10"]}, ntp_ctl=True)
    r = switchcfg.reconcile(inv, off)
    assert any("izolace vypnuta" in c for c in r["changes"]) and 'action=drop comment="mtc isolate"' in r["revert"]
    # vlastní pravidla controlleru nedělají z konfigurace „custom“
    assert switchcfg.classify(inv)["class"] != "custom"


def _local_env():
    from helpers import FakeProvider, fresh_db, make_settings, new_box
    from test_flow import PASSWORD

    from mtcontroller import auth, services
    from mtcontroller.db import operator_scope
    from mtcontroller.web import admin as _admin
    from mtcontroller.web.app import AppState, create_app

    db, name = fresh_db()
    tmp = Path(tempfile.mkdtemp())
    settings = make_settings(tmp, local=True)
    settings.paths.data_dir = tmp
    box = new_box()
    app = create_app(AppState(settings, db, box, [FakeProvider()]))
    _admin._attempts.clear()
    with operator_scope(db) as tx:
        uid = auth.create_user(tx, "admin@example.cz", PASSWORD)
        tx.execute("INSERT INTO role_binding (user_id, role) VALUES (%(u)s, 'superadmin')", {"u": uid})
    t = services.create_tenant(db, settings, "Firma")
    s = services.create_site(db, t["id"], "Praha")
    return db, name, settings, box, app, str(t["id"]), str(s["id"])


def test_isolated_device_upgrades_via_controller():
    from helpers import drop_db
    from test_flow import _device_post, _login
    from test_modules import _adopted

    from mtcontroller import inventory, services
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _local_env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "192.168.88.2", "SW-sklad")
        inv = copy.deepcopy(_managed_inv()) | copy.deepcopy(INV)
        inv["update"] = {"channel": "stable", "installed-version": "7.20.1", "latest-version": ""}
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET managed_at = now(), role = 'switch' WHERE id = %(d)s", {"d": sw})
            tx.execute("INSERT INTO system_setting (key, data) VALUES ('ros_latest', %(d)s)", {"d": {"stable": "7.20.5"}})
        # dostupnou verzi doplní controller (izolované zařízení se nezeptá)
        pkgmirror.fill_update(db, settings, sw, inv)
        assert inv["update"]["latest-version"] == "7.20.5"
        inventory.store(db, tid, sw, inv, [])
        cid = services.request_command(db, tid, sw, "upgrade", None, None, {"version": "7.20.5"})
        # balíčky u controlleru ještě nejsou → příkaz čeká, check-in nic nedoručí
        assert "nic k provedeni" in _device_post(app, "/api/checkin", "key-SW1", {"sstp": "none"}).text
        assert pkgmirror.wanted(db, settings) == ["routeros-7.20.5-arm.npk", "wifi-qcom-7.20.5-arm.npk"]
        n = pkgmirror.run(db, settings, get=lambda url, dst: dst.write_bytes(b"npk" * 2000) if dst else b"7.20.5 1")
        assert n == 2
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "none"}).text
        assert "/api/checkin/pkg/routeros-7.20.5-arm.npk" in s and "check-for-updates" not in s and f"id={cid}" in s
        # stažení balíčku jen s klíčem zařízení a jen platné jméno
        from helpers import AsgiClient

        d = AsgiClient(app, "checkin.test", client_ip="192.168.88.2")
        assert d.get("/api/checkin/pkg/routeros-7.20.5-arm.npk").status_code == 401
        r = d.get("/api/checkin/pkg/routeros-7.20.5-arm.npk", headers={"authorization": "Bearer key-SW1"})
        assert r.status_code == 200 and len(r.body if hasattr(r, "body") else r.content) == 6000
        assert d.get("/api/checkin/pkg/..%2Fmaster.key", headers={"authorization": "Bearer key-SW1"}).status_code == 404
        # výjimka „přístup do internetu“: zařízení zase stahuje samo (audit)
        c = _login(app)
        r = c.request("PUT", f"/api/admin/tenants/{tid}/devices/{sw}/internet", json_body={"allow": True})
        assert r.status_code == 200, r.text
        assert c.get(f"/api/admin/tenants/{tid}/devices/{sw}").json()["isolation"] == {"isolated": False, "internet": True}
        with tenant_scope(db, tid) as tx:
            assert not pkgmirror.isolated(tx, settings, sw)
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT 1 FROM audit_log WHERE action = 'device.internet'")
    finally:
        drop_db(name)
