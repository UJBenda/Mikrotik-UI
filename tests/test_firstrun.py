"""Průvodce prvního spuštění (balíček .deb): plán, konfigurace, vlastní CA, nginx, aplikace a web."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from helpers import AsgiClient, drop_db, fresh_db, make_settings
from test_routeros import _balanced

from mtcontroller import firstrun, routeros
from mtcontroller.config import load_settings
from mtcontroller.db import operator_scope

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"mode": "local", "controller_ip": "192.168.88.10", "ui_host": "192.168.88.10",
         "checkin_host": "192.168.88.10", "admin_nets": ["192.168.88.0/24"]}


def _share(tmp: Path) -> Path:
    share = tmp / "share"
    (share / "nginx").mkdir(parents=True)
    for n in ("mtcontroller.conf", "mtcontroller-local.conf", "mtcontroller-setup.conf"):
        (share / "nginx" / n).write_text((ROOT / "deploy" / "nginx" / n).read_text())
    return share


def test_plan_validation():
    p = firstrun.SetupPlan.model_validate(LOCAL | {"admin_nets": ["192.168.88.7/24", " ", "10.0.0.0/8"]})
    assert p.admin_nets == ["192.168.88.0/24", "10.0.0.0/8"]
    bad = [
        LOCAL | {"checkin_host": "jiny.lan"},  # lokálně jedna adresa
        LOCAL | {"mode": "remote"},  # vzdáleně dvě jména
        LOCAL | {"certs": "letsencrypt", "acme_email": "a@b.cz"},  # LE neumí IP
        LOCAL | {"controller_ip": "127.0.0.1"},
        LOCAL | {"admin_nets": []},
        LOCAL | {"ui_host": "bad host", "checkin_host": "bad host"},
        {**LOCAL, "mode": "remote", "ui_host": "a.cz", "checkin_host": "b.cz", "certs": "letsencrypt"},  # bez e-mailu
    ]
    for b in bad:
        with pytest.raises(ValidationError):
            firstrun.SetupPlan.model_validate(b)


def test_config_is_valid_for_both_modes(tmp_path):
    loc = firstrun.render_config(firstrun.SetupPlan.model_validate(LOCAL))
    (tmp_path / "l.toml").write_text(loc)
    s = load_settings(tmp_path / "l.toml", environ={})
    assert s.tunnel.local and s.web.ui_host == s.web.checkin_host == "192.168.88.10"
    assert str(s.security.device_ca_file) == "/etc/mtcontroller/device-ca.crt"
    rem = firstrun.render_config(firstrun.SetupPlan.model_validate(
        {**LOCAL, "mode": "remote", "ui_host": "mtc-admin.firma.cz", "checkin_host": "mtc-checkin.firma.cz",
         "controller_ip": "10.10.0.10", "certs": "letsencrypt", "acme_email": "it@firma.cz"}))
    (tmp_path / "r.toml").write_text(rem)
    s = load_settings(tmp_path / "r.toml", environ={})
    assert not s.tunnel.local and s.security.device_ca_file is None and s.tunnel.controller_ip == "10.10.0.10"


def test_own_ca_chain_and_fingerprint(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    ssl, dca, pub = tmp_path / "ssl", tmp_path / "device-ca.crt", tmp_path / "pub" / "ca.crt"
    crt, key = firstrun.ensure_own_ca(["192.168.88.10", "mtc.lan"], ssl, dca, pub)
    certs = x509.load_pem_x509_certificates(crt.read_bytes())
    server, ca = certs[0], certs[1]
    server.verify_directly_issued_by(ca)
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(i) for i in san.get_values_for_type(x509.IPAddress)] == ["192.168.88.10"]
    assert san.get_values_for_type(x509.DNSName) == ["mtc.lan"]
    assert (ssl / "ca.key").stat().st_mode & 0o077 == 0 and key.stat().st_mode & 0o077 == 0
    # otisk ve formátu RouterOS (sha256 DER, hex)
    assert routeros.ca_fingerprint(dca) == ca.fingerprint(hashes.SHA256()).hex()
    assert pub.read_bytes() == dca.read_bytes()
    # další běh použije stejnou CA – zařízení jí dál věří
    fp = routeros.ca_fingerprint(dca)
    firstrun.ensure_own_ca(["192.168.88.11"], ssl, dca, pub)
    routeros._CA_CACHE.clear()
    assert routeros.ca_fingerprint(dca) == fp


def test_device_scripts_trust_own_ca(tmp_path):
    from mtcontroller import defaults

    dca = tmp_path / "device-ca.crt"
    firstrun.ensure_own_ca(["192.168.88.10"], tmp_path / "ssl", dca, tmp_path / "pub.crt")
    s = make_settings(tmp_path, local=True).model_copy(deep=True)
    s.security.device_ca_file = dca
    opts = routeros.fetch_opts(s)
    fp = routeros.ca_fingerprint(dca)
    assert opts.ca_url == "http://checkin.test/mtc-ca.crt" and opts.ca_fp == fp
    line = routeros.adopt_one_liner("https://checkin.test", "TOKEN", opts)
    assert "\n" not in line and line.isascii() and _balanced(line)
    assert f'fingerprint="{fp}"' in line and line.index("mtc-ca.crt") < line.index("/adopt/TOKEN")
    # cizí certifikát z HTTP se smaže
    assert "/certificate remove $c" in line
    body = routeros.trust_ca_body(opts)
    assert body.isascii() and _balanced("{\n" + body + "}\n")
    enroll = defaults.enroll_source("https://checkin.test/enroll/K?role=ap", opts)
    assert f'fingerprint="{fp}"' in enroll and _balanced(enroll)
    one = defaults.one_liner("https://checkin.test/enroll/K/default/ap", opts)
    assert "\n" not in one and _balanced(one) and one.index("mtc-ca.crt") < one.index("mtc-default.rsc")
    # bez vlastní CA beze změny
    plain = routeros.FetchOpts("yes-without-crl")
    assert routeros.trust_ca_body(plain) == "" and routeros.adopt_one_liner("https://c", "T", plain).startswith("/tool fetch")
    # bez ověřování (lab) se CA nestahuje
    s.security.device_check_certificate = "no"
    assert routeros.fetch_opts(s).ca_url == ""


def test_nginx_local(tmp_path):
    share = _share(tmp_path)
    plan = firstrun.SetupPlan.model_validate(LOCAL | {"admin_nets": ["192.168.88.0/24", "10.8.0.0/24"]})
    out = firstrun.render_nginx(plan, "/c.crt", "/c.key", share)
    assert "__" not in out and "example" not in out
    assert "allow 192.168.88.0/24;\n    allow 10.8.0.0/24;" in out
    assert "proxy_set_header Host 192.168.88.10;" in out
    assert "alias /etc/ssl/mtc-public/ca.crt;" in out
    rem = firstrun.render_nginx(firstrun.SetupPlan.model_validate(
        {**LOCAL, "mode": "remote", "ui_host": "a.firma.cz", "checkin_host": "c.firma.cz"}), "/c.crt", "/c.key", share)
    assert "server_name a.firma.cz;" in rem and "server_name c.firma.cz;" in rem and "__" not in rem
    assert "allow 192.168.88.0/24;  # mtc-admin-net" in rem


def test_apply_request(tmp_path):
    share = _share(tmp_path)
    etc, data, ssl, ngx = tmp_path / "etc", tmp_path / "data", tmp_path / "ssl", tmp_path / "nginx"
    etc.mkdir()
    (etc / "setup-code").write_text("CODE")
    sd = firstrun.setup_dir(data)
    sd.mkdir(parents=True)
    (ngx / "sites-enabled").mkdir(parents=True)
    (ngx / "sites-enabled" / "mtcontroller-setup.conf").write_text("x")
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    def run(*cmd, check=True):
        calls.append(cmd)
        return R()

    (sd / "request.json").write_text(json.dumps(LOCAL))
    assert firstrun.apply_request(etc, data, share, ssl, ngx, run=run, switch_delay=0) == 0
    st = json.loads((sd / "status.json").read_text())
    assert st["state"] == "done" and st["url"] == "https://192.168.88.10/"
    assert not (sd / "request.json").exists() and not (etc / "setup-code").exists()
    s = load_settings(etc / "config.toml", environ={})
    assert s.tunnel.local and s.security.device_ca_file == etc / "device-ca.crt"
    assert (etc / "config.toml").stat().st_mode & 0o007 == 0
    assert (ngx / "sites-enabled" / "mtcontroller.conf").is_symlink()
    assert not (ngx / "sites-enabled" / "mtcontroller-setup.conf").exists()
    assert ("nginx", "-t") in calls and ("systemctl", "enable", "--now", "mtc-web", "mtc-worker", "mtc-syslog") in calls
    assert any(c[:2] == ("systemctl", "disable") for c in calls)

    # podruhé (už nastaveno) nic nepřepíše
    before = (etc / "config.toml").read_text()
    (sd / "request.json").write_text(json.dumps(LOCAL | {"controller_ip": "192.168.88.99", "ui_host": "x.lan",
                                                         "checkin_host": "x.lan"}))
    assert firstrun.apply_request(etc, data, share, ssl, ngx, run=run, switch_delay=0) == 1
    assert json.loads((sd / "status.json").read_text())["state"] == "error"
    assert (etc / "config.toml").read_text() == before and not (sd / "request.json").exists()


def test_apply_rejects_tampered_request(tmp_path):
    """Požadavek píše méně privilegovaný průvodce – root ho ověřuje znovu."""
    data = tmp_path / "data"
    sd = firstrun.setup_dir(data)
    sd.mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    evil = LOCAL | {"ui_host": 'x"; include /etc/shadow; #', "checkin_host": 'x"; include /etc/shadow; #'}
    (sd / "request.json").write_text(json.dumps(evil))
    assert firstrun.apply_request(tmp_path / "etc", data, _share(tmp_path), tmp_path / "ssl", tmp_path / "ngx",
                                  run=lambda *a, **k: None, switch_delay=0) == 1
    assert json.loads((sd / "status.json").read_text())["state"] == "error"
    assert not (tmp_path / "etc" / "config.toml").exists()


def test_setup_web(tmp_path):
    db, name = fresh_db()
    try:
        etc, data = tmp_path / "etc", tmp_path / "data"
        etc.mkdir()
        (etc / "setup-code").write_text("ABCD-EFGH-JKLM\n")
        app = firstrun.create_setup_app(db, make_settings(tmp_path), etc=etc, data=data)
        c = AsgiClient(app, "192.168.88.10")
        assert "setup.js" in c.get("/").text
        assert c.get("/setup/static/setup.js").status_code == 200
        assert c.get("/setup/static/app.js").status_code == 404
        assert c.get("/setup/api/info").status_code == 403
        assert c.get("/setup/api/info", headers={"x-setup-code": "SPATNY"}).status_code == 403
        h = {"x-setup-code": "abcd-efgh-jklm"}
        assert c.get("/setup/api/info", headers=h).json()["configured"] is False
        body = {"plan": LOCAL, "admin": {"email": "Admin@Firma.cz", "password": "kratke"}, "tenant": "Doma", "site": "Dům"}
        r = c.post("/setup/api/apply", json_body=body, headers=h)
        assert r.status_code == 400 and "12" in r.json()["detail"]
        body["admin"]["password"] = "dlouhe-heslo-123"
        body["plan"] = LOCAL | {"checkin_host": "jine.lan"}
        r = c.post("/setup/api/apply", json_body=body, headers=h)
        assert r.status_code == 400 and "jedné adrese" in r.json()["detail"]
        body["plan"] = LOCAL
        r = c.post("/setup/api/apply", json_body=body, headers=h)
        assert r.status_code == 200 and r.json()["admin"]["created"], r.text
        req = json.loads((firstrun.setup_dir(data) / "request.json").read_text())
        assert req["mode"] == "local" and "password" not in json.dumps(req)
        assert c.get("/setup/api/status", headers=h).json()["state"] == "queued"
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM role_binding WHERE role = 'superadmin'")["n"] == 1
            assert tx.fetchone("SELECT name FROM tenant")["name"] == "Doma"
            assert tx.fetchone("SELECT name FROM site")["name"] == "Dům"
            assert "user.create_superadmin" in [r["action"] for r in tx.fetchall("SELECT action FROM audit_log")]
        # opakované odeslání (např. po chybě aplikace) druhého admina nezaloží
        assert c.post("/setup/api/apply", json_body=body, headers=h).json()["admin"]["created"] is False
        # hádání kódu se zablokuje
        for _ in range(12):
            c.get("/setup/api/info", headers={"x-setup-code": "X"})
        assert c.get("/setup/api/info", headers=h).status_code == 403
    finally:
        drop_db(name)


def test_new_code_format():
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", firstrun.new_code())
