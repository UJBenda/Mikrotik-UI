"""Uživatelé tenanta: přidání, role, odebrání, změna hesla, reset MFA."""

from helpers import drop_db
from test_flow import PASSWORD, _login
from test_modules import _env

from mtcontroller import auth, services
from mtcontroller.db import operator_scope


def _sessions(db, email):
    with operator_scope(db) as tx:
        return tx.fetchone("""SELECT count(*) AS n FROM app_session s JOIN app_user u ON u.id = s.user_id
                              WHERE u.email = %(e)s""", {"e": email})["n"]


def test_add_role_remove_and_isolation():
    db, name, settings, box, app, tid, sid = _env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/users"
        r = c.post(base, json_body={"email": "Technik@Firma.cz", "role": "technician"})
        assert r.status_code == 201, r.text
        temp = r.json()["temp_password"]
        uid = r.json()["user_id"]
        assert temp and len(temp) >= 12 and r.json()["email"] == "technik@firma.cz"
        assert c.post(base, json_body={"email": "technik@firma.cz", "role": "readonly"}).status_code == 400
        assert c.post(base, json_body={"email": "x@y.cz", "role": "superadmin"}).status_code == 400
        # dočasné heslo nikde v auditu
        with operator_scope(db) as tx:
            blob = str(tx.fetchall("SELECT after FROM audit_log WHERE action = 'user.add'"))
        assert temp not in blob and "new_account" in blob

        # technik se přihlásí dočasným heslem, nastaví MFA, ale uživatele spravovat nesmí
        t = _login(app, "technik@firma.cz", temp)
        assert t.get(base).status_code == 404
        assert t.get(f"/api/admin/tenants/{tid}/devices").status_code == 200

        # existující účet z jiného tenanta jen dostane roli (bez nového hesla)
        other = services.create_tenant(db, settings, "Jina")
        r = c.post(f"/api/admin/tenants/{other['id']}/users", json_body={"email": "technik@firma.cz", "role": "readonly"})
        assert r.status_code == 201 and r.json()["temp_password"] is None

        users = {u["email"]: u for u in c.get(base).json()}
        assert users["technik@firma.cz"]["role"] == "technician" and users["technik@firma.cz"]["mfa"] is True
        assert c.patch(f"{base}/{uid}", json_body={"role": "tenant_admin"}).status_code == 200
        assert t.get(base).status_code == 200  # nová role platí hned
        # sám sobě nic
        me = t.get("/api/admin/me").json()
        assert me["tenants"][str(tid)] == "tenant_admin"
        assert t.patch(f"{base}/{uid}", json_body={"role": "readonly"}).status_code == 400
        assert t.request("DELETE", f"{base}/{uid}").status_code == 400
        # odebrání z tenanta (v jiném tenantovi zůstává → relace se nemažou)
        assert c.request("DELETE", f"{base}/{uid}").status_code == 200
        assert t.get(base).status_code == 404
        assert _sessions(db, "technik@firma.cz") >= 1
        assert c.request("DELETE", f"/api/admin/tenants/{other['id']}/users/{uid}").status_code == 200
        assert _sessions(db, "technik@firma.cz") == 0  # bez role žádné relace
    finally:
        drop_db(name)


def test_change_password_and_mfa_reset():
    db, name, settings, box, app, tid, sid = _env()
    try:
        c = _login(app)
        r = c.post(f"/api/admin/tenants/{tid}/users", json_body={"email": "op@firma.cz", "role": "technician"})
        temp, uid = r.json()["temp_password"], r.json()["user_id"]
        o = _login(app, "op@firma.cz", temp)
        auth.login(db, "op@firma.cz", temp, 8)  # druhá relace (jiný prohlížeč)
        assert _sessions(db, "op@firma.cz") == 2
        r = o.post("/api/admin/me/password", json_body={"old": "spatne-heslo-123", "new": "NoveHeslo-12345"})
        assert r.status_code == 400
        with operator_scope(db) as tx:  # neúspěch zapsán i přes chybu
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action = 'auth.password_change_failed'")["n"] == 1
        assert o.post("/api/admin/me/password", json_body={"old": temp, "new": "kratke"}).status_code == 400
        assert o.post("/api/admin/me/password", json_body={"old": temp, "new": "NoveHeslo-12345"}).status_code == 200
        assert _sessions(db, "op@firma.cz") == 1  # ostatní relace ukončené, tahle zůstala
        assert o.get("/api/admin/me").status_code == 200
        auth.login(db, "op@firma.cz", "NoveHeslo-12345", 8)  # nové heslo platí

        # reset MFA smí jen superadmin
        assert o.post(f"/api/admin/users/{uid}/reset-mfa").status_code == 404
        assert c.post(f"/api/admin/users/{uid}/reset-mfa").status_code == 200
        assert _sessions(db, "op@firma.cz") == 0
        _, info = auth.login(db, "op@firma.cz", "NoveHeslo-12345", 8)
        assert info["mfa"] == "setup"
        # superadmin svoje heslo (PASSWORD) – špatné staré nepustí
        assert c.post("/api/admin/me/password", json_body={"old": PASSWORD + "x", "new": "JineHeslo-123456"}).status_code == 400
    finally:
        drop_db(name)
