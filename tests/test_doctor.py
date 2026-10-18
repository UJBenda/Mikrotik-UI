"""mtc-admin doctor: kontroly instalace hlásí srozumitelné chyby."""

from helpers import drop_db, fresh_db, make_settings, owner_db

from mtcontroller import doctor
from mtcontroller.security import SecretBox


class _Api:
    def __init__(self, fail=False):
        self.fail, self.closed = fail, False

    def get(self, path):
        if self.fail:
            raise ConnectionError("timeout")
        return {"name": "vpn1"}

    def close(self):
        self.closed = True


class _Prov:
    def __init__(self, name, fail=False):
        self.name, self.api = name, _Api(fail)

    def active_sessions(self):
        return {"mtc-A": "10.200.1.2"}


def _by(checks, name):
    return [c for c in checks if c.name == name]


def test_network_route_and_controller_ip(tmp_path):
    s = make_settings(tmp_path)
    good = doctor.check_network(s, route_get=lambda ip: {"gateway": "10.10.0.1", "prefsrc": "10.10.0.10"},
                                is_local=lambda ip: True)
    assert _by(good, "controller_ip")[0].status == doctor.OK
    assert _by(good, "routa na pool")[0].status == doctor.OK
    bad = doctor.check_network(s, route_get=lambda ip: {"gateway": "192.168.1.1", "dev": "eth0"},
                               is_local=lambda ip: False)
    assert _by(bad, "controller_ip")[0].status == doctor.FAIL
    r = _by(bad, "routa na pool")[0]
    assert r.status == doctor.FAIL and "ip route add 10.200.0.0/16 via 10.10.0.1" in r.detail
    src = doctor.check_network(s, route_get=lambda ip: {"gateway": "10.10.0.1", "prefsrc": "10.10.0.99"},
                               is_local=lambda ip: True)
    assert _by(src, "routa na pool")[0].status == doctor.WARN


def test_concentrators_fingerprint_and_rest(tmp_path):
    s = make_settings(tmp_path, concentrators=2)
    c2 = s.tunnel.concentrators[1].model_copy(update={"api_fingerprint": "0" * 64})
    s.tunnel.concentrators[1] = c2
    provs = [_Prov("vpn1"), _Prov("vpn2", fail=True)]
    checks = doctor.check_concentrators(s, providers=provs)
    assert _by(checks, "vpn1: REST")[0].status == doctor.OK
    assert _by(checks, "vpn2: REST")[0].status == doctor.FAIL
    assert _by(checks, "vpn2: otisk")[0].status == doctor.FAIL
    assert all(p.api.closed for p in provs)


def test_master_key_and_config(tmp_path):
    s = make_settings(tmp_path)
    key = tmp_path / "master.key"
    key.write_text(SecretBox.generate_master_key())
    key.chmod(0o644)
    s.security.master_key_file = key
    assert doctor.check_master_key(s)[0].status == doctor.FAIL  # čitelný pro všechny
    key.chmod(0o640)
    assert doctor.check_master_key(s)[0].status == doctor.OK
    s.web.ui_host = "mtc-admin.example.cz"
    assert _by(doctor.check_config(s), "hostname")[0].status == doctor.FAIL


def test_database_checks(tmp_path):
    db, name = fresh_db()
    try:
        checks = doctor.check_database(make_settings(tmp_path), db, owner_db(name))
        assert _by(checks, "databáze (aplikace)")[0].status == doctor.OK
        assert _by(checks, "superadmin")[0].status == doctor.WARN
        assert _by(checks, "migrace")[0].status == doctor.OK
    finally:
        drop_db(name)


def test_run_never_raises(tmp_path):
    s = make_settings(tmp_path)
    checks = doctor.run(s, app_db=None, owner_db=None, providers=[])
    assert any(c.status == doctor.FAIL for c in checks)
    assert doctor.render(checks)


def test_routes_check(tmp_path):
    s = make_settings(tmp_path, concentrators=2)
    assert doctor.check_routes(s)[0].status == doctor.WARN  # dva koncentrátory bez manage_routes
    s.tunnel.manage_routes = True
    assert doctor.check_routes(s, caps=lambda: "")[0].status == doctor.FAIL
    assert doctor.check_routes(s, caps=lambda: "cap_net_admin")[0].status == doctor.OK
    one = tmp_path / "one"
    one.mkdir()
    assert doctor.check_routes(make_settings(one)) == []  # jeden koncentrátor: nic k hlášení


def test_oui_check(tmp_path):
    from pathlib import Path

    from mtcontroller import oui

    oui.configure(tmp_path)
    assert doctor.check_oui()[0].status == doctor.WARN
    oui.configure(Path(__file__).parent / "data")
    assert doctor.check_oui()[0].status == doctor.OK
