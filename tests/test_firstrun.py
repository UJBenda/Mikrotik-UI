"""Průvodce prvního spuštění (balíček .deb): plán, konfigurace, vlastní CA, nginx, aplikace a web."""

import json
import re
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from helpers import AsgiClient, drop_db, fresh_db, make_settings
from test_routeros import _balanced

from mtcontroller import firstrun, routeros, sysbackup, tlscfg
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
    assert not s.tunnel.local and s.security.device_ca_file is not None and s.tunnel.controller_ip == "10.10.0.10"


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
    out = firstrun.render_nginx(plan, Path("/s"), share)
    assert "ssl_certificate     /s/admin.crt;" in out and "/s/admin.key;" in out
    assert "include /s/hsts-admin*.conf;" in out and "Strict-Transport-Security" not in out
    assert "__" not in out and "example" not in out
    assert "allow 192.168.88.0/24;\n    allow 10.8.0.0/24;" in out
    assert "proxy_set_header Host 192.168.88.10;" in out
    assert "alias /etc/ssl/mtc-public/ca.crt;" in out
    rem = firstrun.render_nginx(firstrun.SetupPlan.model_validate(
        {**LOCAL, "mode": "remote", "ui_host": "a.firma.cz", "checkin_host": "c.firma.cz"}), Path("/s"), share)
    assert "/s/checkin.crt;" in rem and "/s/admin.crt;" in rem
    assert "server_name a.firma.cz;" in rem and "server_name c.firma.cz;" in rem and "__" not in rem
    assert "allow 192.168.88.0/24;  # mtc-admin-net" in rem


class _R:
    returncode, stdout, stderr = 0, "", ""


def _env(tmp: Path, **kw) -> firstrun.Env:
    etc, data = tmp / "etc", tmp / "data"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "setup-code").write_text("CODE")
    firstrun.setup_dir(data).mkdir(parents=True, exist_ok=True)
    ngx = tmp / "nginx"
    (ngx / "sites-enabled").mkdir(parents=True, exist_ok=True)
    (ngx / "sites-available").mkdir(parents=True, exist_ok=True)
    (ngx / "sites-available" / "mtcontroller-setup.conf").write_text("x")
    (ngx / "sites-enabled" / "mtcontroller-setup.conf").symlink_to(ngx / "sites-available" / "mtcontroller-setup.conf")
    calls = []

    def run(*cmd, check=True):
        calls.append(cmd)
        return _R()

    env = firstrun.Env(etc=etc, data=data, share=_share(tmp), ssl=tmp / "ssl", nginx=ngx, run=run, le_live=tmp / "le",
                       current_ip=lambda: "192.168.88.10", **kw)
    env.calls = calls
    return env


def _req(env, body):
    (firstrun.setup_dir(env.data) / "request.json").write_text(json.dumps(body))
    return firstrun.apply_request(env)


def _st(env):
    return json.loads((firstrun.setup_dir(env.data) / "status.json").read_text())


def test_install_then_finish(tmp_path):
    backups = []
    env = _env(tmp_path, backup=lambda: backups.append(1))
    plan = LOCAL | {"timezone": "Europe/Bratislava", "country": "Slovakia", "ntp": ["sk.pool.ntp.org"]}
    assert _req(env, {"action": "install", "plan": plan, "admin": "a@b.cz"}) == 0
    st = _st(env)
    assert st["state"] == "ready" and backups == [1]
    # zatím běží průvodce: nginx controlleru připravený, ale nezapnutý
    assert (env.nginx / "sites-enabled" / "mtcontroller-setup.conf").exists()
    assert not (env.nginx / "sites-enabled" / "mtcontroller.conf").exists()
    s = load_settings(env.etc / "config.toml", environ={})
    assert s.tunnel.local and s.site_defaults.country == "Slovakia" and s.site_defaults.timezone == "Europe/Bratislava"
    assert s.site_defaults.ntp == ["sk.pool.ntp.org"] and s.backup.keep == 14
    pw = (env.etc / "backup.key").read_text().strip()
    assert (env.etc / "backup.key").stat().st_mode & 0o077 == 0
    kit = json.loads((firstrun.setup_dir(env.data) / "kit.json").read_text())
    assert kit["backup_passphrase"] == pw and kit["admin"] == "a@b.cz" and kit["url"] == "https://192.168.88.10/"
    text = firstrun.kit_text(kit)
    assert pw in text and "bez hesel" in text and "mtc-admin restore" in text
    assert ("timedatectl", "set-timezone", "Europe/Bratislava") in env.calls
    assert any(c[:3] == ("systemctl", "enable", "--now") and "mtc-backup.timer" in c for c in env.calls)
    assert firstrun.SetupPlan.model_validate_json((env.etc / "setup-plan.json").read_text()).country == "Slovakia"

    assert _req(env, {"action": "finish"}) == 0
    st = _st(env)
    assert st["state"] == "done" and st["url"] == "https://192.168.88.10/"
    assert (env.nginx / "sites-enabled" / "mtcontroller.conf").is_symlink()
    assert not (env.nginx / "sites-enabled" / "mtcontroller-setup.conf").exists()
    assert not (env.etc / "setup-code").exists() and not (firstrun.setup_dir(env.data) / "kit.json").exists()
    assert any(c[:2] == ("systemctl", "disable") for c in env.calls)

    # podruhé (už nastaveno) nic nepřepíše
    before = (env.etc / "config.toml").read_text()
    assert _req(env, {"action": "install", "plan": LOCAL}) == 1
    assert _st(env)["state"] == "error" and (env.etc / "config.toml").read_text() == before


def test_finish_reverts_when_nginx_fails(tmp_path):
    env = _env(tmp_path)
    assert _req(env, {"action": "install", "plan": LOCAL}) == 0

    class Bad:
        returncode, stdout, stderr = 1, "", "emerg: chyba"

    env.run = lambda *c, check=True: Bad() if c[:2] == ("nginx", "-t") else _R()
    assert _req(env, {"action": "finish"}) == 1
    assert "nginx -t" in _st(env)["detail"]
    assert (env.nginx / "sites-enabled" / "mtcontroller-setup.conf").exists()  # průvodce zůstal dostupný
    assert not (env.nginx / "sites-enabled" / "mtcontroller.conf").exists()


def test_restore_flow(tmp_path):
    """Obnova: root obnoví soubory ze zálohy, nginx a certifikát podle uloženého plánu."""
    got = {}
    plan = LOCAL | {"controller_ip": "192.168.88.20", "ui_host": "192.168.88.20", "checkin_host": "192.168.88.20"}

    def restore(path, pw):
        got["file"], got["pw"] = path.read_bytes(), pw
        (tmp_path / "etc" / "config.toml").write_text(firstrun.render_config(firstrun.SetupPlan.model_validate(plan)))
        (tmp_path / "etc" / "setup-plan.json").write_text(json.dumps(plan))
        return {"manifest": {"created_at": "2026-10-01T02:30:00+00:00", "app_version": "0.0.9", "hostname": "stary"},
                "migrated": [18, 19]}

    env = _env(tmp_path, restore=restore)
    (firstrun.setup_dir(env.data) / "upload.mtcbak").write_bytes(b"MTCBAK1\nxx")
    pw = "abcde-fghij-kmnpq-rstuv-wxyz2-34567"
    assert _req(env, {"action": "restore", "restore": {"passphrase": pw}, "form": {"admin_nets": ["10.0.0.0/8"]}}) == 0
    assert got == {"file": b"MTCBAK1\nxx", "pw": pw}
    assert not (firstrun.setup_dir(env.data) / "upload.mtcbak").exists()
    st = _st(env)
    assert st["state"] == "ready" and "192.168.88.20" in st["warning"]  # server má jinou IP než záloha
    kit = json.loads((firstrun.setup_dir(env.data) / "kit.json").read_text())
    assert kit["backup_passphrase"] == pw and kit["restored"]["migrated"] == [18, 19]
    assert "POZOR" in firstrun.kit_text(kit) and "0.0.9" in firstrun.kit_text(kit)
    assert (env.etc / "backup.key").read_text().strip() == pw
    conf = (env.nginx / "sites-available" / "mtcontroller.conf").read_text()
    assert "proxy_set_header Host 192.168.88.20;" in conf and "allow 192.168.88.0/24;" in conf
    # chyba obnovy (např. špatné heslo) se ukáže průvodci a nahraný soubor zmizí
    shutil.rmtree(env.etc)
    env2 = _env(tmp_path / "b", restore=lambda p, w: (_ for _ in ()).throw(sysbackup.BackupError("špatné heslo zálohy")))
    (firstrun.setup_dir(env2.data) / "upload.mtcbak").write_bytes(b"x")
    assert _req(env2, {"action": "restore", "restore": {"passphrase": "x" * 12}}) == 1
    assert "špatné heslo" in _st(env2)["detail"]
    assert not (firstrun.setup_dir(env2.data) / "upload.mtcbak").exists()


def test_restore_older_install_without_plan(tmp_path):
    """Záloha z instalace skriptem (bez setup-plan.json): plán z konfigurace + sítě z průvodce."""
    def restore(path, pw):
        (tmp_path / "etc" / "config.toml").write_text(
            '[web]\nui_host = "mtc-admin.firma.cz"\ncheckin_host = "mtc-checkin.firma.cz"\n'
            '[tunnel]\ncontroller_ip = "10.10.0.10"\n')
        return {"manifest": {}, "migrated": []}

    env = _env(tmp_path, restore=restore)
    (firstrun.setup_dir(env.data) / "upload.mtcbak").write_bytes(b"x")
    assert _req(env, {"action": "restore", "restore": {"passphrase": "x" * 12},
                      "form": {"admin_nets": ["10.10.0.0/24"], "acme_email": "it@firma.cz"}}) == 0
    p = firstrun.SetupPlan.model_validate_json((env.etc / "setup-plan.json").read_text())
    assert p.mode == "remote" and p.certs == "letsencrypt" and p.admin_nets == ["10.10.0.0/24"]
    assert any(c[0] == "certbot" for c in env.calls)


def test_apply_rejects_tampered_request(tmp_path):
    """Požadavek píše méně privilegovaný průvodce – root ho ověřuje znovu."""
    env = _env(tmp_path)
    evil = LOCAL | {"ui_host": 'x"; include /etc/shadow; #', "checkin_host": 'x"; include /etc/shadow; #'}
    assert _req(env, {"action": "install", "plan": evil}) == 1
    assert _st(env)["state"] == "error" and not (env.etc / "config.toml").exists()
    assert _req(env, {"action": "rm -rf"}) == 1
    assert _req(env, LOCAL) == 1  # starý formát bez akce


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
        assert c.get("/setup/static/setup.css").status_code == 200
        assert c.get("/setup/static/app.js").status_code == 404
        assert c.get("/setup/api/info").status_code == 403
        assert c.get("/setup/api/info", headers={"x-setup-code": "SPATNY"}).status_code == 403
        h = {"x-setup-code": "abcd-efgh-jklm"}
        info = c.get("/setup/api/info", headers=h).json()
        assert info["configured"] is False and "Europe/Prague" in info["timezones"]
        assert {"iso": "CZ", "label": "Česko", "ros": "Czech Republic", "ntp": ["tik.cesnet.cz", "tak.cesnet.cz"]} in info["countries"]
        body = {"action": "install", "plan": LOCAL, "admin": {"email": "Admin@Firma.cz", "password": "kratke"},
                "tenant": "Doma", "site": "Dům"}
        r = c.post("/setup/api/apply", json_body=body, headers=h)
        assert r.status_code == 400 and "12" in r.json()["detail"]
        body["admin"] = {"email": "Admin@Firma.cz", "password": "dlouhe-heslo-123", "password2": "jine-heslo-1234"}
        assert "neshodují" in c.post("/setup/api/apply", json_body=body, headers=h).json()["detail"]
        body["admin"]["password2"] = "dlouhe-heslo-123"
        body["plan"] = LOCAL | {"checkin_host": "jine.lan"}
        r = c.post("/setup/api/apply", json_body=body, headers=h)
        assert r.status_code == 400 and "jedné adrese" in r.json()["detail"]
        body["plan"] = LOCAL | {"country": "Czech; /system reset"}
        assert c.post("/setup/api/apply", json_body=body, headers=h).status_code == 400
        body["plan"] = LOCAL
        r = c.post("/setup/api/apply", json_body=body, headers=h)
        assert r.status_code == 200 and r.json()["admin"]["created"], r.text
        req = json.loads((firstrun.setup_dir(data) / "request.json").read_text())
        assert req["action"] == "install" and req["plan"]["mode"] == "local" and req["admin"] == "admin@firma.cz"
        assert "dlouhe-heslo" not in json.dumps(req)
        assert c.get("/setup/api/status", headers=h).json()["state"] == "queued"
        # během nastavování další požadavek neprojde
        assert c.post("/setup/api/apply", json_body=body, headers=h).status_code == 409
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM role_binding WHERE role = 'superadmin'")["n"] == 1
            assert tx.fetchone("SELECT name FROM tenant")["name"] == "Doma"
            assert tx.fetchone("SELECT name FROM site")["name"] == "Dům"
            assert "user.create_superadmin" in [r["action"] for r in tx.fetchall("SELECT action FROM audit_log")]
        # stav „ready“ vrací záchranné údaje
        firstrun._status(firstrun.setup_dir(data), "ready", "ok", step=5)
        (firstrun.setup_dir(data) / "kit.json").write_text(json.dumps(firstrun.kit(firstrun.SetupPlan.model_validate(LOCAL), "heslo-zaloh-123")))
        st = c.get("/setup/api/status", headers=h).json()
        assert st["kit"]["backup_passphrase"] == "heslo-zaloh-123" and "heslo-zaloh-123" in st["kit_text"]
        # opakované odeslání (např. po chybě aplikace) druhého admina nezaloží
        assert c.post("/setup/api/apply", json_body=body, headers=h).json()["admin"]["created"] is False
        # hádání kódu se zablokuje
        for _ in range(12):
            c.get("/setup/api/info", headers={"x-setup-code": "X"})
        assert c.get("/setup/api/info", headers=h).status_code == 403
    finally:
        drop_db(name)


def test_setup_upload_and_restore_request(tmp_path):
    etc, data = tmp_path / "etc", tmp_path / "data"
    etc.mkdir()
    (etc / "setup-code").write_text("ABCD-EFGH-JKLM")
    app = firstrun.create_setup_app(None, make_settings(tmp_path), etc=etc, data=data)
    c = AsgiClient(app, "x")
    h = {"x-setup-code": "ABCD-EFGH-JKLM"}
    body = {"action": "restore", "restore": {"passphrase": "abcde-fghij-kmnpq"}, "form": {"admin_nets": ["10.0.0.0/8"]}}
    assert "nahraj" in c.post("/setup/api/apply", json_body=body, headers=h).json()["detail"]
    r = c.request("PUT", "/setup/api/upload", data=b"PK\x03\x04 to je zip", headers=h)
    assert r.status_code == 400 and "není záloha" in r.json()["detail"]
    assert not list(firstrun.setup_dir(data).glob("upload*"))
    r = c.request("PUT", "/setup/api/upload", data=sysbackup.MAGIC + b"data" * 100, headers=h)
    assert r.status_code == 200 and r.json()["size"] == len(sysbackup.MAGIC) + 400
    assert c.request("PUT", "/setup/api/upload", data=sysbackup.MAGIC, headers={"x-setup-code": "X"}).status_code == 403
    assert c.post("/setup/api/apply", json_body=body | {"restore": {"passphrase": "kratke"}}, headers=h).status_code == 400
    r = c.post("/setup/api/apply", json_body=body, headers=h)
    assert r.status_code == 200
    req = json.loads((firstrun.setup_dir(data) / "request.json").read_text())
    assert req == {"action": "restore", "restore": {"passphrase": "abcde-fghij-kmnpq"},
                   "form": {"admin_nets": ["10.0.0.0/8"], "acme_email": ""}}


def test_new_code_format():
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", firstrun.new_code())


def _le_files(live: Path, slot: str, host: str) -> bytes:
    """Podvržený výsledek certbotu (jiná CA než controlleru)."""
    from cryptography.hazmat.primitives import serialization

    ca_key, ca = firstrun.make_ca("Fake LE")
    key, cert = firstrun.make_server_cert(ca_key, ca, [host])
    d = live / f"mtc-{slot}"
    d.mkdir(parents=True, exist_ok=True)
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (d / "fullchain.pem").write_bytes(pem)
    (d / "privkey.pem").write_bytes(firstrun._pem_key(key))
    return pem


REMOTE_LE = {**LOCAL, "mode": "remote", "ui_host": "a.firma.cz", "checkin_host": "c.firma.cz",
             "certs": "letsencrypt", "acme_email": "it@firma.cz"}


def test_install_letsencrypt_fails_falls_back_to_own_ca(tmp_path):
    """Certbot selže (DNS, port 80) – instalace doběhne s certifikátem od CA controlleru a varováním."""
    env = _env(tmp_path)

    class Bad:
        returncode, stdout, stderr = 1, "", "Challenge failed"

    calls = env.calls
    env.run = lambda *c, check=True: (calls.append(c), Bad() if c[0] == "certbot" else _R())[1]
    assert _req(env, {"action": "install", "plan": REMOTE_LE}) == 0
    st = _st(env)
    assert st["state"] == "ready" and "a.firma.cz" in st["warning"] and "Certifikát HTTPS" in st["warning"]
    assert sum(c[0] == "certbot" for c in calls) == 2
    for slot in ("admin", "checkin"):
        info = tlscfg.inspect((env.ssl / f"{slot}.crt").read_bytes())
        assert info["issuer"].startswith("mtc controller CA") and (env.ssl / f"{slot}.key").stat().st_mode & 0o077 == 0
        assert tlscfg.marker(env.ssl.parent / "mtc-public")[slot]["kind"] == "own_ca"
    assert tlscfg.covers(tlscfg.inspect((env.ssl / "checkin.crt").read_bytes()), "c.firma.cz")
    # CA pro zařízení je v konfiguraci vždy
    s = load_settings(env.etc / "config.toml", environ={})
    assert s.security.device_ca_file == env.etc / "device-ca.crt" and (env.etc / "device-ca.crt").exists()
    kit = json.loads((firstrun.setup_dir(env.data) / "kit.json").read_text())
    assert "POZOR" in firstrun.kit_text(kit)
    conf = (env.nginx / "sites-available" / "mtcontroller.conf").read_text()
    assert f"{env.ssl}/admin.crt;" in conf and f"{env.ssl}/checkin.key;" in conf


def test_install_letsencrypt_ok(tmp_path):
    env = _env(tmp_path)
    pem = {s: _le_files(env.le_live, s, h) for s, h in (("admin", "a.firma.cz"), ("checkin", "c.firma.cz"))}
    assert _req(env, {"action": "install", "plan": REMOTE_LE}) == 0
    assert not _st(env).get("warning")
    certbot = [c for c in env.calls if c[0] == "certbot"]
    assert len(certbot) == 2 and "--webroot" in certbot[0] and "mtc-admin" in certbot[0]
    for slot in ("admin", "checkin"):
        assert (env.ssl / f"{slot}.crt").read_bytes() == pem[slot]
        assert tlscfg.marker(env.ssl.parent / "mtc-public")[slot]["kind"] == "letsencrypt"


def test_upgrade_nginx_legacy_letsencrypt_paths(tmp_path):
    """Instalace skriptem s Let's Encrypt, který neexistuje: nginx nenaběhne. Oprava přesměruje
    certifikáty na sloty (admin blok → admin, check-in blok → checkin)."""
    env = _env(tmp_path)
    le_c, le_k = "/etc/letsencrypt/live/mtcontroller/fullchain.pem", "/etc/letsencrypt/live/mtcontroller/privkey.pem"
    tpl = (env.share / "nginx" / "mtcontroller.conf").read_text()
    legacy = (tpl.replace("mtc-admin.example.cz", "a.firma.cz").replace("mtc-checkin.example.cz", "c.firma.cz")
              .replace("__ADMIN_CERT__", le_c).replace("__ADMIN_KEY__", le_k)
              .replace("__CHECKIN_CERT__", le_c).replace("__CHECKIN_KEY__", le_k)
              # starší šablona: pevné HSTS (s CA controlleru prohlížeč nedovolí výjimku)
              .replace("include __ADMIN_HSTS__;", 'add_header Strict-Transport-Security "max-age=31536000" always;')
              .replace("    include __CHECKIN_HSTS__;\n", ""))
    (env.nginx / "sites-available" / "mtcontroller.conf").write_text(legacy)
    (env.etc / "config.toml").write_text('[web]\nui_host = "a.firma.cz"\ncheckin_host = "c.firma.cz"\n')
    assert firstrun.upgrade_nginx(env)
    out = (env.nginx / "sites-available" / "mtcontroller.conf").read_text()
    assert "letsencrypt" not in out
    assert "Strict-Transport-Security" not in out and out.count(f"include {env.ssl}/hsts-admin*.conf;") == 1
    admin = out[out.index("server_name a.firma.cz;"):out.index("server_name c.firma.cz;")]
    checkin = out[out.index("server_name c.firma.cz;"):]
    assert f"ssl_certificate     {env.ssl}/admin.crt;" in admin and f"{env.ssl}/admin.key;" in admin
    assert f"ssl_certificate     {env.ssl}/checkin.crt;" in checkin and f"{env.ssl}/checkin.key;" in checkin
    assert ("systemctl", "reload-or-restart", "nginx") in env.calls
    assert not firstrun.upgrade_nginx(env)  # podruhé nic
    # nginx -t neprojde → zpět původní
    (env.nginx / "sites-available" / "mtcontroller.conf").write_text(legacy)

    class Bad:
        returncode, stdout, stderr = 1, "", "emerg"

    env.run = lambda *c, check=True: Bad() if c[:2] == ("nginx", "-t") else _R()
    assert not firstrun.upgrade_nginx(env)
    assert (env.nginx / "sites-available" / "mtcontroller.conf").read_text() == legacy
