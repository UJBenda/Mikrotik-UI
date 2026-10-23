"""Certifikáty HTTPS: vždy něco pro start nginx, Let's Encrypt / vlastní potom, návrat při chybě."""

import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from mtcontroller import doctor, firstrun, tlscfg

HOSTS = {"admin": "a.firma.cz", "checkin": "c.firma.cz"}


class _R:
    def __init__(self, rc=0, err=""):
        self.returncode, self.stdout, self.stderr = rc, "", err


def _runner(fail: set[str] = frozenset()):
    calls = []

    def run(*cmd, check=False):
        calls.append(cmd)
        key = "certbot" if cmd[0] == "certbot" else " ".join(cmd[:2])
        return _R(1, "selhalo: " + key) if key in fail else _R()

    run.calls = calls
    return run


def _pair(hosts, days=90, ca=None):
    """(cert PEM s řetězcem, klíč PEM) od cizí CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    ca_key, ca_cert = ca or firstrun.make_ca("Firemni CA")
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return cert.public_bytes(Encoding.PEM) + ca_cert.public_bytes(Encoding.PEM), firstrun._pem_key(key)


def _dirs(tmp: Path):
    return {"ssl_dir": tmp / "ssl", "public": tmp / "pub"}


def _boot(tmp: Path, local=False):
    d = _dirs(tmp)
    s = _settings(tmp, local)
    out = tlscfg.bootstrap(s, live=tmp / "le", device_ca=tmp / "device-ca.crt", **d)
    return d, s, out


def _settings(tmp: Path, local=False):
    return SimpleNamespace(
        web=SimpleNamespace(ui_host="192.168.88.10" if local else HOSTS["admin"],
                            checkin_host="192.168.88.10" if local else HOSTS["checkin"]),
        tunnel=SimpleNamespace(local=local, controller_ip="192.168.88.10" if local else "10.10.0.10"),
        security=SimpleNamespace(device_ca_file=tmp / "device-ca.crt"),
        paths=SimpleNamespace(data_dir=tmp / "data"))


def test_bootstrap_always_gives_nginx_a_certificate(tmp_path):
    d, s, out = _boot(tmp_path)
    assert len(out) == 2 and all("CA controlleru" in x for x in out)
    st = tlscfg.status(d["public"])
    assert st["admin"]["kind"] == "own_ca" and tlscfg.covers(st["admin"], "a.firma.cz")
    assert tlscfg.covers(st["checkin"], "c.firma.cz") and "10.10.0.10" in st["checkin"]["names"]
    assert (d["ssl_dir"] / "admin.key").stat().st_mode & 0o077 == 0
    import ssl
    for slot in ("admin", "checkin"):  # stejně jako nginx: řetěz + klíč k sobě patří
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(d["ssl_dir"] / f"{slot}.crt", d["ssl_dir"] / f"{slot}.key")
    assert (d["public"] / "ca.crt").exists() and (tmp_path / "device-ca.crt").exists()
    # podruhé (aktualizace) nic nepřepisuje
    before = (d["ssl_dir"] / "admin.crt").read_bytes()
    assert tlscfg.bootstrap(s, live=tmp_path / "le", **d) == []
    assert (d["ssl_dir"] / "admin.crt").read_bytes() == before
    # lokální režim: jen jeden slot
    d2, _, out2 = _boot(tmp_path / "loc", local=True)
    assert len(out2) == 1 and "192.168.88.10" in tlscfg.status(d2["public"])["admin"]["names"]


def test_bootstrap_takes_over_legacy_letsencrypt(tmp_path):
    """Starší instalace skriptem: jeden LE `mtcontroller` pro obě jména – převezme se, obnovy přes sync."""
    cert, key = _pair(["a.firma.cz", "c.firma.cz"])
    le = tmp_path / "le" / "mtcontroller"
    le.mkdir(parents=True)
    (le / "fullchain.pem").write_bytes(cert)
    (le / "privkey.pem").write_bytes(key)
    d, s, out = _boot(tmp_path)
    assert all("převzat" in x for x in out)
    m = tlscfg.marker(d["public"])
    assert m["admin"]["kind"] == "letsencrypt" and m["checkin"]["lineage"] == "mtcontroller"
    # obnova certbotem → deploy hook → do obou slotů a reload nginx
    cert2, key2 = _pair(["a.firma.cz", "c.firma.cz"])
    (le / "fullchain.pem").write_bytes(cert2)
    (le / "privkey.pem").write_bytes(key2)
    run = _runner()
    assert sorted(tlscfg.sync(live=tmp_path / "le", run=run, **d)) == ["admin", "checkin"]
    assert (d["ssl_dir"] / "checkin.crt").read_bytes() == cert2 and ("systemctl", "reload", "nginx") in run.calls
    assert tlscfg.sync(live=tmp_path / "le", run=run, **d) == []  # beze změny nic


def test_check_request_validation():
    def bad(req, local=False):
        try:
            tlscfg.check_request(req, local=local, hosts=HOSTS)
        except tlscfg.TlsError as e:
            return str(e)
        raise AssertionError("prošlo")

    assert "neznámá" in bad({"action": "rm -rf"})
    assert "vyber" in bad({"action": "own_ca", "slots": ["x"]})
    assert "e-mail" in bad({"action": "letsencrypt", "slots": ["admin"], "email": "a@b; touch /x"})
    ip_hosts = {"admin": "192.168.88.10", "checkin": "192.168.88.10"}
    try:
        tlscfg.check_request({"action": "letsencrypt", "email": "a@b.cz"}, local=True, hosts=ip_hosts)
        raise AssertionError("LE na IP prošel")
    except tlscfg.TlsError as e:
        assert "DNS" in str(e)
    cert, key = _pair(["a.firma.cz"])
    other_cert, _ = _pair(["a.firma.cz"])
    assert "nepatří" in bad({"action": "custom", "slots": ["admin"], "cert": other_cert.decode(), "key": key.decode()})
    assert "PEM" in bad({"action": "custom", "slots": ["admin"], "cert": "nic", "key": key.decode()})
    old, okey = _pair(["a.firma.cz"], days=-1)
    assert "vypršel" in bad({"action": "custom", "slots": ["admin"], "cert": old.decode(), "key": okey.decode()})
    # jiné jméno → jen varování; LE / vlastní na check-inu → varování o důvěře zařízení
    r = tlscfg.check_request({"action": "custom", "slots": ["admin", "checkin"], "cert": cert.decode(),
                              "key": key.decode()}, local=False, hosts=HOSTS)
    assert any("c.firma.cz" in w for w in r["warnings"]) and any("builtin-trust-store" in w for w in r["warnings"])
    r = tlscfg.check_request({"action": "letsencrypt", "slots": ["admin"], "email": "it@firma.cz"},
                             local=False, hosts=HOSTS)
    assert r["slots"] == ["admin"] and not r.get("warnings")
    # lokální režim: vždy jen slot admin
    assert tlscfg.check_request({"action": "own_ca", "slots": ["checkin"]}, local=True, hosts=HOSTS)["slots"] == ["admin"]


def test_apply_custom_and_revert_when_nginx_fails(tmp_path):
    d, s, _ = _boot(tmp_path)
    own = (d["ssl_dir"] / "admin.crt").read_bytes()
    cert, key = _pair(["a.firma.cz"])
    req = {"action": "custom", "slots": ["admin"], "cert": cert.decode(), "key": key.decode()}
    # nginx -t neprojde → zpět předchozí certifikát
    run = _runner({"nginx -t"})
    try:
        tlscfg.apply(req, local=False, hosts=HOSTS, ip="10.10.0.10", run=run, **d)
        raise AssertionError("prošlo")
    except tlscfg.TlsError:
        pass
    assert (d["ssl_dir"] / "admin.crt").read_bytes() == own and tlscfg.marker(d["public"])["admin"]["kind"] == "own_ca"
    run = _runner()
    res = tlscfg.apply(req, local=False, hosts=HOSTS, ip="10.10.0.10", run=run, **d)
    assert res["slots"] == ["admin"] and ("systemctl", "reload", "nginx") in run.calls
    assert (d["ssl_dir"] / "admin.crt").read_bytes() == cert and (d["ssl_dir"] / "admin.key").read_bytes() == key
    assert (d["public"] / "admin.crt").read_bytes() == cert and not (d["public"] / "admin.key").exists()
    st = tlscfg.status(d["public"])
    assert st["admin"]["kind"] == "custom" and st["admin"]["issuer"].startswith("Firemni CA")
    assert st["checkin"]["kind"] == "own_ca"  # druhý slot beze změny


def test_apply_letsencrypt_failure_keeps_previous(tmp_path):
    d, s, _ = _boot(tmp_path)
    own = (d["ssl_dir"] / "admin.crt").read_bytes()
    run = _runner({"certbot"})
    try:
        tlscfg.apply({"action": "letsencrypt", "slots": ["admin"], "email": "it@firma.cz"}, local=False,
                     hosts=HOSTS, ip="10.10.0.10", run=run, live=tmp_path / "le", **d)
        raise AssertionError("prošlo")
    except tlscfg.TlsError as e:
        assert "port 80" in str(e)
    assert (d["ssl_dir"] / "admin.crt").read_bytes() == own
    cb = [c for c in run.calls if c[0] == "certbot"][0]
    assert "--webroot" in cb and "a.firma.cz" in cb and "mtc-admin" in cb  # --cert-name mtc-admin
    # úspěch: soubory z live/mtc-admin
    cert, key = _pair(["a.firma.cz"])
    le = tmp_path / "le" / "mtc-admin"
    le.mkdir(parents=True)
    (le / "fullchain.pem").write_bytes(cert)
    (le / "privkey.pem").write_bytes(key)
    tlscfg.apply({"action": "letsencrypt", "slots": ["admin"], "email": "it@firma.cz"}, local=False,
                 hosts=HOSTS, ip="10.10.0.10", run=_runner(), live=tmp_path / "le", **d)
    m = tlscfg.marker(d["public"])["admin"]
    assert m["kind"] == "letsencrypt" and m["lineage"] == "mtc-admin" and m["email"] == "it@firma.cz"
    # zpět na CA controlleru (stejná CA – zařízení jí dál věří)
    ca = (d["public"] / "ca.crt").read_bytes()
    tlscfg.apply({"action": "own_ca", "slots": ["admin"]}, local=False, hosts=HOSTS, ip="10.10.0.10",
                 run=_runner(), device_ca=tmp_path / "device-ca.crt", **d)
    assert tlscfg.marker(d["public"])["admin"]["kind"] == "own_ca" and (d["public"] / "ca.crt").read_bytes() == ca


def test_apply_request_from_web(tmp_path):
    """Web zapíše žádost (0600), root ji provede a zapíše stav; chyba se ukáže jako stav."""
    d, s, _ = _boot(tmp_path)
    s.paths.data_dir.mkdir()
    cert, key = _pair(["a.firma.cz"])
    tlscfg.write_request(s.paths.data_dir, {"action": "custom", "slots": ["admin"], "cert": cert.decode(),
                                            "key": key.decode(), "user": "admin@firma.cz"})
    assert tlscfg.request_path(s.paths.data_dir).stat().st_mode & 0o077 == 0
    assert tlscfg.apply_request(s, run=_runner(), live=tmp_path / "le", **d) == 0
    assert not tlscfg.request_path(s.paths.data_dir).exists()
    assert tlscfg.read_status(s.paths.data_dir)["state"] == "done"
    tlscfg.write_request(s.paths.data_dir, {"action": "letsencrypt", "slots": ["admin"], "email": "it@firma.cz"})
    assert tlscfg.apply_request(s, run=_runner({"certbot"}), live=tmp_path / "le", **d) == 1
    st = tlscfg.read_status(s.paths.data_dir)
    assert st["state"] == "error" and "Let's Encrypt" in st["error"]
    assert tlscfg.status(d["public"])["admin"]["kind"] == "custom"  # běží dál ten předchozí
    assert tlscfg.apply_request(s, run=_runner(), **d) == 0  # bez žádosti nic


def test_doctor_tls(tmp_path):
    d, s, _ = _boot(tmp_path)
    c = doctor.check_tls(s, d["public"])
    assert [x.status for x in c] == [doctor.OK, doctor.OK] and "nedůvěryhodný" in c[0].detail
    cert, key = _pair(["a.firma.cz"], days=5)
    tlscfg.install("admin", cert, key, "letsencrypt", **d)
    assert doctor.check_tls(s, d["public"])[0].status == doctor.WARN
    cert, key = _pair(["jine.cz"])
    tlscfg.install("checkin", cert, key, "custom", **d)
    assert "neobsahuje" in doctor.check_tls(s, d["public"])[1].detail
    assert doctor.check_tls(s, tmp_path / "nic")[0].status == doctor.WARN


def test_tls_endpoint():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _env

    from mtcontroller.db import operator_scope
    from mtcontroller.web.app import AppState, create_app

    db, name, settings, box, app, tid, sid = _env()
    old_public = tlscfg.PUBLIC
    try:
        tmp = Path(tempfile.mkdtemp())
        settings.paths.data_dir = tmp
        hosts = tlscfg.hosts_of(settings)
        tlscfg.PUBLIC = tmp / "pub"
        tlscfg.own_ca("admin", [hosts["admin"]], ssl_dir=tmp / "ssl", public=tlscfg.PUBLIC, device_ca=tmp / "dca.crt")
        c = _login(create_app(AppState(settings, db, box, [])))
        t = c.get("/api/admin/system/tls").json()
        assert t["slots"]["admin"]["kind"] == "own_ca" and t["hosts"] == hosts and not t["pending"]
        r = c.post("/api/admin/system/tls", json_body={"action": "letsencrypt", "slots": ["admin"], "email": "x"})
        assert r.status_code == 400 and "e-mail" in r.json()["detail"]
        cert, key = _pair([hosts["admin"]])
        r = c.post("/api/admin/system/tls", json_body={"action": "custom", "slots": ["admin"],
                                                       "cert": cert.decode(), "key": key.decode()})
        assert r.status_code == 200, r.text
        req = json.loads(tlscfg.request_path(tmp).read_text())
        assert req["user"] == "admin@example.cz" and req["key"] == key.decode()
        assert c.post("/api/admin/system/tls", json_body={"action": "own_ca", "slots": ["admin"]}).status_code == 409
        with operator_scope(db) as tx:
            a = tx.fetchone("SELECT after FROM audit_log WHERE action = 'system.tls_request'")
        assert a and "key" not in json.dumps(a["after"]) and "BEGIN" not in json.dumps(a["after"])
        # ne-superadmin nic
        r = c.post(f"/api/admin/tenants/{tid}/users", json_body={"email": "t@firma.cz", "role": "tenant_admin"})
        t = _login(create_app(AppState(settings, db, box, [])), "t@firma.cz", r.json()["temp_password"])
        assert t.get("/api/admin/system/tls").status_code == 404
    finally:
        tlscfg.PUBLIC = old_public
        drop_db(name)
