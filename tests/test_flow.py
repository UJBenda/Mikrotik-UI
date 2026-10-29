"""End-to-end: přihlášení s MFA → tenant, lokalita → adopční token →
zařízení se zaregistruje → schválení → provisioning přes check-in → ack →
worker ověří REST → adopted → reboot / deprovision. Plus izolace tenantů."""

import re
import tempfile
import time
from pathlib import Path

from helpers import AsgiClient, FakeProvider, drop_db, fresh_db, make_settings, new_box

from mtcontroller import auth, services, totp
from mtcontroller.db import operator_scope, tenant_scope
from mtcontroller.web.app import AppState, create_app
from mtcontroller.worker import __main__ as worker

PASSWORD = "velmi-tajne-heslo-123"


def _setup(concentrators=1, provider_fail=False, local=False):
    db, name = fresh_db()
    tmp = Path(tempfile.mkdtemp())
    settings = make_settings(tmp, concentrators, local=local)
    box = new_box()
    providers = [] if local else [FakeProvider(f"vpn{i}", provider_fail) for i in range(1, concentrators + 1)]
    app = create_app(AppState(settings, db, box, providers))
    from mtcontroller.web import admin as _admin
    _admin._attempts.clear()
    with operator_scope(db) as tx:
        uid = auth.create_user(tx, "admin@example.cz", PASSWORD)
        tx.execute("INSERT INTO role_binding (user_id, role) VALUES (%(u)s, 'superadmin')", {"u": uid})
    return db, name, settings, box, providers, app


def _login(app, email="admin@example.cz", password=PASSWORD, box=None, db=None):
    c = AsgiClient(app, "admin.test")
    r = c.post("/api/admin/login", json_body={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.csrf = r.json()["csrf"]
    if r.json()["mfa"] == "setup":
        s = c.post("/api/admin/mfa/setup").json()
        secret = s["secret"]
        c.totp_secret = secret
    else:
        secret = c.totp_secret
    r = c.post("/api/admin/mfa/verify", json_body={"code": totp.totp(secret)})
    assert r.status_code == 200, r.text
    return c


def _device_post(app, path, key=None, body=None):
    d = AsgiClient(app, "checkin.test", client_ip="203.0.113.5")
    headers = {"authorization": f"Bearer {key}"} if key else {}
    if path == "/api/checkin" and isinstance(body, dict) and "sv" not in body:
        # aktuální skript check-inu (bez `sv` by controller poslal jeho přeinstalaci)
        from mtcontroller.routeros import CHECKIN_REV
        body = {**body, "sv": CHECKIN_REV}
    return d.post(path, json_body=body or {}, headers=headers)


def test_old_checkin_script_gets_reinstalled():
    """Zařízení se starším skriptem check-inu (bez `sv`) dostane jednou jeho přeinstalaci:
    nový skript při chybě TLS sám dosadí CA controlleru (stejný otisk)."""
    from test_portcfg import _setup as ports_setup

    db, name, settings, box, app, tid, sw = ports_setup()
    try:
        s = _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up", "sv": 0}).text
        assert 'scheduler add name="mtc-checkin"' in s and "sv" in s
        cid = int(re.search(r"ack\\\?id=(\d+)", s).group(1))
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-SW1")
        # podruhé už ne (ani se starým skriptem – nejvýš jednou za den)
        assert "nic k provedeni" in _device_post(app, "/api/checkin", "key-SW1", {"sstp": "up", "sv": 0}).text
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'device.checkin_update'")["n"] == 1
    finally:
        drop_db(name)


def test_full_adoption_flow():
    db, name, settings, box, providers, app = _setup()
    try:
        c = _login(app)
        # bez MFA/CSRF nic
        assert AsgiClient(app, "admin.test").get("/api/admin/tenants").status_code == 401

        t = c.post("/api/admin/tenants", json_body={"name": "Firma A"})
        assert t.status_code == 201, t.text
        tid = t.json()["id"]
        assert t.json()["tunnel_block"] == "10.200.1.0/24"
        s = c.post(f"/api/admin/tenants/{tid}/sites", json_body={"name": "Praha"})
        sid = s.json()["id"]

        tok = c.post(f"/api/admin/tenants/{tid}/adoption-tokens", json_body={"site_id": sid})
        assert tok.status_code == 201, tok.text
        cmd, token = tok.json()["command"], tok.json()["token"]
        assert f"https://checkin.test/adopt/{token}" in cmd
        assert "check-certificate=yes-without-crl" in cmd

        # check-in endpointy nejsou na admin hostname a naopak
        assert c.get(f"/adopt/{token}").status_code == 404
        assert AsgiClient(app, "checkin.test").get("/api/admin/me").status_code == 404

        # krok 1: skript
        dev = AsgiClient(app, "checkin.test", client_ip="203.0.113.5")
        r = dev.get(f"/adopt/{token}")
        assert r.status_code == 200 and "/adopt/" + token + "/register" in r.text

        # krok 2: registrace
        reg = {"serial": "HG409ABC123", "model": "RB5009UG+S+", "version": "7.20.1 (stable)", "mac": "48:A9:8A:01:02:03"}
        r = dev.post(f"/adopt/{token}/register", json_body=reg)
        assert r.status_code == 200, r.text
        key = re.search(r"Authorization: Bearer ([A-Za-z0-9_-]+)", r.text).group(1)
        assert 'name="mtc-checkin"' in r.text and "interval=1m" in r.text

        # token je jednorázový
        assert dev.post(f"/adopt/{token}/register", json_body=reg).status_code == 404
        assert dev.get(f"/adopt/{token}").status_code == 404

        # check-in ve stavu pending: nic k provedení
        r = _device_post(app, "/api/checkin", key, {"version": "7.20.1 (stable)", "sstp": "none", "cpu": 3})
        assert r.status_code == 200 and "nic k provedeni" in r.text
        assert _device_post(app, "/api/checkin", "spatny-klic").status_code == 401

        devs = c.get(f"/api/admin/tenants/{tid}/devices").json()
        assert len(devs) == 1 and devs[0]["state"] == "pending"
        did = devs[0]["id"]

        # schválení bez CSRF neprojde
        csrf, c.csrf = c.csrf, None
        assert c.post(f"/api/admin/tenants/{tid}/devices/{did}/approve", json_body={"site_id": sid}).status_code == 403
        c.csrf = csrf
        r = c.post(f"/api/admin/tenants/{tid}/devices/{did}/approve", json_body={"site_id": sid, "role": "gateway"})
        assert r.status_code == 200 and r.json()["concentrator_errors"] == [], r.text
        acct = providers[0].accounts["mtc-HG409ABC123"]
        assert acct.remote_address == "10.200.1.1"

        # hesla nejsou v DB v čitelné podobě
        with operator_scope(db) as tx:
            blobs = tx.fetchall("SELECT ciphertext FROM device_secret")
        assert all(acct.password.encode() not in bytes(b["ciphertext"]) for b in blobs)

        # další check-in doručí provisioning
        r = _device_post(app, "/api/checkin", key, {"version": "7.20.1 (stable)", "sstp": "none", "cpu": 3})
        assert "provisioning zacina" in r.text
        assert f'password="{acct.password}"' in r.text
        assert 'connect-to="vpn1.test"' in r.text
        assert 'address="10.200.0.1/32"' in r.text
        cid = int(re.search(r"/api/checkin/ack\\?\?id=(\d+)", r.text).group(1))
        # stejný příkaz se nedoručí dvakrát
        r2 = _device_post(app, "/api/checkin", key, {"sstp": "down"})
        assert "nic k provedeni" in r2.text

        # ack + nahlášený otisk certifikátu
        r = _device_post(app, f"/api/checkin/ack?id={cid}&status=done", key)
        assert r.text == "ok\n"
        fp = "cd" * 32
        _device_post(app, "/api/checkin", key, {"sstp": "up", "fp": fp})

        # worker: REST přes tunel – podvrhneme RosApi
        calls = []

        class FakeApi:
            def __init__(self, url, user, pw, f, timeout=0):
                calls.append((url, user, pw, f))

            def get(self, path):
                return {"name": "router-praha"} if path == "/system/identity" else {"board-name": "RB5009UG+S+"}

        orig = worker.RosApi
        worker.RosApi = FakeApi
        try:
            assert worker.verify_provisioning(db, box) == 1
        finally:
            worker.RosApi = orig
        assert calls == [("https://10.200.1.1", "mtc-ctl", calls[0][2], fp)]
        dev_row = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert dev_row["state"] == "adopted" and dev_row["identity"] == "router-praha"

        # offline a zpět online
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET last_checkin_at = now() - interval '10 minutes'")
        assert worker.mark_offline(db) == 1
        _device_post(app, "/api/checkin", key, {"sstp": "up", "fp": fp})
        assert c.get(f"/api/admin/tenants/{tid}/devices").json()[0]["state"] == "adopted"

        # reboot: ack proběhne před restartem
        r = c.post(f"/api/admin/tenants/{tid}/devices/{did}/commands", json_body={"kind": "reboot"})
        assert r.status_code == 201
        r = _device_post(app, "/api/checkin", key, {"sstp": "up", "fp": fp})
        assert r.text.index("/api/checkin/ack") < r.text.index("/system reboot")

        # neznámý příkaz odmítnut
        assert c.post(f"/api/admin/tenants/{tid}/devices/{did}/commands", json_body={"kind": "rm -rf"}).status_code == 400

        # deprovision → removed, klíč zneplatněn, účet na koncentrátoru zakázán workerem
        r = c.post(f"/api/admin/tenants/{tid}/devices/{did}/commands", json_body={"kind": "deprovision"})
        rb_id = None
        with tenant_scope(db, tid) as tx:
            rb_id = tx.fetchone("SELECT id FROM device_command WHERE kind = 'reboot'")["id"]
        _device_post(app, f"/api/checkin/ack?id={rb_id}&status=done", key)
        r = _device_post(app, "/api/checkin", key, {"sstp": "up", "fp": fp})
        assert '/system script remove [find name="mtc-checkin"]' in r.text
        dp_id = int(re.search(r"ack\\?\?id=(\d+)", r.text).group(1))
        _device_post(app, f"/api/checkin/ack?id={dp_id}&status=done", key)
        assert _device_post(app, "/api/checkin", key, {}).status_code == 401
        worker.reconcile_tunnels(db, box, providers)
        assert "mtc-HG409ABC123" in providers[0].disabled

        # audit zachytil celý průběh
        actions = [a["action"] for a in c.get(f"/api/admin/tenants/{tid}/audit").json()]
        for a in ("device.register", "device.approve", "device.adopted", "device.removed"):
            assert a in actions, (a, actions)
    finally:
        drop_db(name)


def test_tenant_isolation_via_api():
    db, name, settings, box, providers, app = _setup()
    try:
        admin = _login(app)
        ta = admin.post("/api/admin/tenants", json_body={"name": "A"}).json()["id"]
        tb = admin.post("/api/admin/tenants", json_body={"name": "B"}).json()["id"]
        sb = admin.post(f"/api/admin/tenants/{tb}/sites", json_body={"name": "Brno"}).json()["id"]
        with operator_scope(db) as tx:
            u = auth.create_user(tx, "tech@a.cz", PASSWORD)
            tx.execute("INSERT INTO role_binding (user_id, role, tenant_id) VALUES (%(u)s, 'technician', %(t)s)",
                       {"u": u, "t": ta})
        tech = _login(app, "tech@a.cz")
        assert [t["id"] for t in tech.get("/api/admin/tenants").json()] == [ta]
        assert tech.get(f"/api/admin/tenants/{tb}/devices").status_code == 404
        assert tech.post(f"/api/admin/tenants/{tb}/adoption-tokens", json_body={"site_id": sb}).status_code == 404
        # technik nesmí zakládat lokality ani tenanty
        assert tech.post(f"/api/admin/tenants/{ta}/sites", json_body={"name": "X"}).status_code == 403
        assert tech.post("/api/admin/tenants", json_body={"name": "C"}).status_code == 403
        # lokalitu cizího tenanta nejde použít ani přes vlastního tenanta
        assert tech.post(f"/api/admin/tenants/{ta}/adoption-tokens", json_body={"site_id": sb}).status_code == 404
    finally:
        drop_db(name)


def test_device_cannot_be_stolen_by_other_tenant():
    db, name, settings, box, providers, app = _setup()
    try:
        c = _login(app)
        ta = c.post("/api/admin/tenants", json_body={"name": "A"}).json()["id"]
        tb = c.post("/api/admin/tenants", json_body={"name": "B"}).json()["id"]
        reg = {"serial": "SER1234", "model": "hAP ax3", "version": "7.20 (stable)", "mac": ""}
        t1 = c.post(f"/api/admin/tenants/{ta}/adoption-tokens", json_body={}).json()["token"]
        assert _device_post(app, f"/adopt/{t1}/register", body=reg).status_code == 200
        t2 = c.post(f"/api/admin/tenants/{tb}/adoption-tokens", json_body={}).json()["token"]
        r = _device_post(app, f"/adopt/{t2}/register", body=reg)
        assert r.status_code == 400 and "spravovane" in r.text
        # RouterOS v6 odmítnut
        t3 = c.post(f"/api/admin/tenants/{ta}/adoption-tokens", json_body={}).json()["token"]
        r = _device_post(app, f"/adopt/{t3}/register", body=dict(reg, serial="OLD1", version="6.49.10"))
        assert r.status_code == 400
    finally:
        drop_db(name)


def test_login_lockout_and_totp_replay():
    db, name, settings, box, providers, app = _setup()
    try:
        c = AsgiClient(app, "admin.test", client_ip="192.0.2.50")
        for _ in range(5):
            assert c.post("/api/admin/login", json_body={"email": "admin@example.cz", "password": "spatne-heslo-xx"}).status_code == 401
        # účet zamčený i se správným heslem
        assert c.post("/api/admin/login", json_body={"email": "admin@example.cz", "password": PASSWORD}).status_code == 401
        with operator_scope(db) as tx:
            tx.execute("UPDATE app_user SET locked_until = NULL, failed_logins = 0")
        c2 = _login(app)
        code = totp.totp(c2.totp_secret)
        # nové přihlášení stejným kódem (replay) neprojde
        c3 = AsgiClient(app, "admin.test", client_ip="192.0.2.51")
        r = c3.post("/api/admin/login", json_body={"email": "admin@example.cz", "password": PASSWORD})
        c3.csrf = r.json()["csrf"]
        assert r.json()["mfa"] == "verify"
        assert c3.post("/api/admin/mfa/verify", json_body={"code": code}).status_code == 401
        # bez dokončeného MFA nejsou data
        assert c3.get("/api/admin/tenants").status_code == 401
    finally:
        drop_db(name)


def test_concentrator_failover_after_tunnel_down():
    db, name, settings, box, providers, app = _setup(concentrators=2)
    try:
        c = _login(app)
        tid = c.post("/api/admin/tenants", json_body={"name": "A"}).json()["id"]
        sid = c.post(f"/api/admin/tenants/{tid}/sites", json_body={"name": "P"}).json()["id"]
        tok = c.post(f"/api/admin/tenants/{tid}/adoption-tokens", json_body={"site_id": sid}).json()["token"]
        r = _device_post(app, f"/adopt/{tok}/register",
                         body={"serial": "FO1234", "model": "x", "version": "7.20", "mac": ""})
        key = re.search(r"Bearer ([A-Za-z0-9_-]+)", r.text).group(1)
        did = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]["id"]
        c.post(f"/api/admin/tenants/{tid}/devices/{did}/approve", json_body={"site_id": sid})
        assert set(p.name for p in providers if "mtc-FO1234" in p.accounts) == {"vpn1", "vpn2"}
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'adopted', adopted_at = now()")
            tx.execute("UPDATE device_command SET status = 'done'")
        out = ""
        for _ in range(services.SSTP_FAILOVER_AFTER):
            out = _device_post(app, "/api/checkin", key, {"sstp": "down"}).text
        assert 'connect-to="vpn2.test"' in out
    finally:
        drop_db(name)


def test_approve_survives_concentrator_outage():
    db, name, settings, box, providers, app = _setup(provider_fail=True)
    try:
        c = _login(app)
        tid = c.post("/api/admin/tenants", json_body={"name": "A"}).json()["id"]
        sid = c.post(f"/api/admin/tenants/{tid}/sites", json_body={"name": "P"}).json()["id"]
        tok = c.post(f"/api/admin/tenants/{tid}/adoption-tokens", json_body={"site_id": sid}).json()["token"]
        _device_post(app, f"/adopt/{tok}/register", body={"serial": "OUT1234", "model": "x", "version": "7.20", "mac": ""})
        did = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]["id"]
        r = c.post(f"/api/admin/tenants/{tid}/devices/{did}/approve", json_body={"site_id": sid})
        assert r.status_code == 200 and r.json()["concentrator_errors"]
        providers[0].fail = False
        worker.reconcile_tunnels(db, box, providers)
        assert "mtc-OUT1234" in providers[0].accounts
    finally:
        drop_db(name)


def test_local_mode_manages_devices_directly():
    """Lokální režim: žádné koncentrátory ani tunely, správa jde na adresu, ze které se zařízení hlásí."""
    db, name, settings, box, providers, app = _setup(local=True)
    try:
        c = _login(app)
        assert c.get("/api/admin/me").json()["mode"] == "local"
        tid = c.post("/api/admin/tenants", json_body={"name": "Doma"}).json()["id"]
        sid = c.post(f"/api/admin/tenants/{tid}/sites", json_body={"name": "Dům"}).json()["id"]
        tok = c.post(f"/api/admin/tenants/{tid}/adoption-tokens", json_body={"site_id": sid}).json()["token"]
        lan = AsgiClient(app, "checkin.test", client_ip="192.168.88.2")
        r = lan.post(f"/adopt/{tok}/register", json_body={"serial": "LOC1234", "model": "hAP ax2", "version": "7.20", "mac": ""})
        assert r.status_code == 200, r.text
        key = re.search(r"Bearer ([A-Za-z0-9_-]+)", r.text).group(1)
        did = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]["id"]
        # adresa správy ještě není známá (zatím žádný check-in)
        r = c.post(f"/api/admin/tenants/{tid}/devices/{did}/approve", json_body={"site_id": sid})
        assert r.status_code == 400 and "check-in" in r.text

        def ci(body, ip="192.168.88.2"):
            return lan.__class__(app, "checkin.test", client_ip=ip).post(
                "/api/checkin", json_body=body, headers={"authorization": f"Bearer {key}"})

        ci({"version": "7.20", "sstp": "none"})
        r = c.post(f"/api/admin/tenants/{tid}/devices/{did}/approve", json_body={"site_id": sid})
        assert r.status_code == 200 and r.json()["concentrator_errors"] == [], r.text
        dev = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert dev["tunnel_ip"] == "192.168.88.2" and dev["concentrator"] is None

        r = ci({"version": "7.20", "sstp": "none"})
        assert "provisioning zacina" in r.text
        assert "sstp-client add" not in r.text and 'sstp-client remove [find name="mtc-sstp"]' in r.text
        assert 'address="192.168.88.10/32"' in r.text
        assert 'chain=input src-address="192.168.88.10" protocol=tcp' in r.text
        assert "action=drop" not in r.text
        with operator_scope(db) as tx:
            assert not tx.fetchall("SELECT 1 FROM device_secret WHERE kind = 'sstp_password'")

        # DHCP změnila adresu → správa jde na novou; tunel „none“ nespouští failover ani alarm
        for _ in range(services.SSTP_FAILOVER_AFTER + 1):
            ci({"sstp": "none"}, ip="192.168.88.7")
        dev = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert dev["tunnel_ip"] == "192.168.88.7", dev
        assert (dev["last_status"] or {}).get("sstp_down") == 0
        with operator_scope(db) as tx:
            kinds = [r["kind"] for r in tx.fetchall("SELECT kind FROM device_command")]
            acts = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log")]
        assert "switch_concentrator" not in kinds
        assert "device.address" in acts
        # cizí adresu (už ji má jiné zařízení) nepřebírá
        with operator_scope(db) as tx:
            tx.execute("""INSERT INTO device (tenant_id, serial, model, ros_version, state, tunnel_ip)
                          VALUES (%(t)s, 'OTHER1', 'x', '7.20', 'pending', '192.168.88.9')""", {"t": tid})
        ci({"sstp": "none"}, ip="192.168.88.9")
        mine = [d for d in c.get(f"/api/admin/tenants/{tid}/devices").json() if d["serial"] == "LOC1234"][0]
        assert mine["tunnel_ip"] == "192.168.88.7"
    finally:
        drop_db(name)
