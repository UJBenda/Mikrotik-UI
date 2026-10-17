"""Přihlášení, MFA (TOTP), relace a oprávnění.

Postup: heslo → relace s mfa_ok=false → (první přihlášení) nastavení TOTP →
ověření kódu → mfa_ok=true. Do API se smí jen s mfa_ok. Po 5 chybných
pokusech se účet na 15 minut zamkne. TOTP kód nejde použít dvakrát.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any

from . import totp
from .db import Database, Tx
from .security import SealedSecret, SecretBox, hash_password, new_token, token_hash, verify_password
from .services import ServiceError, audit

MAX_FAILED = 5
LOCK_MINUTES = 15
# hash pro porovnání při neexistujícím uživateli (aby čas odpovědi neprozradil, zda účet existuje)
_DUMMY_HASH = hash_password("dummy-password-for-timing")

READ, OPERATE, MANAGE = "read", "operate", "manage"
_ROLE_LEVEL = {"readonly": 1, "technician": 2, "tenant_admin": 3, "superadmin": 4}
_ACTION_LEVEL = {READ: 1, OPERATE: 2, MANAGE: 3}


class AuthError(Exception):
    pass


@dataclass
class Principal:
    session_id: str
    user_id: str
    email: str
    mfa_ok: bool
    csrf: str
    superadmin: bool = False
    tenant_roles: dict[str, str] = field(default_factory=dict)  # tenant_id -> role

    def can(self, tenant_id: Any, action: str) -> bool:
        if not self.mfa_ok:
            return False
        if self.superadmin:
            return True
        role = self.tenant_roles.get(str(tenant_id))
        return bool(role) and _ROLE_LEVEL[role] >= _ACTION_LEVEL[action]


def _ctx(user_id: Any) -> bytes:
    return f"user:{user_id}:totp".encode()


def create_user(tx: Tx, email: str, password: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise ServiceError("neplatný e-mail")
    row = tx.fetchone(
        "INSERT INTO app_user (email, password_hash) VALUES (%(e)s, %(h)s) RETURNING id",
        {"e": email, "h": hash_password(password)},
    )
    return str(row["id"])


def login(db: Database, email: str, password: str, session_hours: int,
          ip: str | None = None) -> tuple[str, dict]:
    """Vrátí (token relace, info). Chyby jsou záměrně neurčité.
    Neúspěšný pokus se zapíše a commitne dřív, než se vyhodí chyba."""
    email = (email or "").strip().lower()
    failed = False
    with db.transaction() as tx:
        user = tx.fetchone(
            """SELECT id, password_hash, is_active, totp_confirmed,
                      locked_until IS NOT NULL AND locked_until > now() AS locked
               FROM app_user WHERE email = %(e)s FOR UPDATE""",
            {"e": email},
        )
        if not user:
            verify_password(password or "", _DUMMY_HASH)
            failed = True
        elif not user["is_active"] or user["locked"]:
            audit(tx, "auth.login_blocked", user_id=user["id"], target=f"user:{user['id']}", ip=ip)
            failed = True
        elif not verify_password(password or "", user["password_hash"]):
            tx.execute(
                """UPDATE app_user SET failed_logins = failed_logins + 1,
                       locked_until = CASE WHEN failed_logins + 1 >= %(m)s
                                           THEN now() + make_interval(mins => %(l)s) END
                   WHERE id = %(id)s""",
                {"m": MAX_FAILED, "l": LOCK_MINUTES, "id": str(user["id"])},
            )
            audit(tx, "auth.login_failed", user_id=user["id"], target=f"user:{user['id']}", ip=ip)
            failed = True
        else:
            token = new_token()
            tx.execute("UPDATE app_user SET failed_logins = 0, locked_until = NULL WHERE id = %(id)s",
                       {"id": str(user["id"])})
            tx.execute(
                """INSERT INTO app_session (token_hash, user_id, csrf_token, expires_at, source_ip)
                   VALUES (%(h)s, %(u)s, %(c)s, now() + make_interval(hours => %(hr)s), %(ip)s)""",
                {"h": token_hash(token), "u": str(user["id"]), "c": secrets.token_hex(32),
                 "hr": session_hours, "ip": ip},
            )
            audit(tx, "auth.password_ok", user_id=user["id"], target=f"user:{user['id']}", ip=ip)
    if failed:
        raise AuthError("neplatné přihlašovací údaje")
    return token, {"mfa": "verify" if user["totp_confirmed"] else "setup"}


def load_principal(db: Database, token: str | None) -> Principal | None:
    if not token or len(token) > 100:
        return None
    with db.transaction() as tx:
        s = tx.fetchone(
            """SELECT s.id, s.user_id, s.mfa_ok, s.csrf_token, u.email
               FROM app_session s JOIN app_user u ON u.id = s.user_id
               WHERE s.token_hash = %(h)s AND s.expires_at > now() AND u.is_active""",
            {"h": token_hash(token)},
        )
        if not s:
            return None
        tx.execute(
            "UPDATE app_session SET last_seen_at = now() WHERE id = %(id)s AND last_seen_at < now() - interval '1 minute'",
            {"id": str(s["id"])},
        )
        roles = tx.fetchall("SELECT role, tenant_id FROM role_binding WHERE user_id = %(u)s",
                            {"u": str(s["user_id"])})
    p = Principal(str(s["id"]), str(s["user_id"]), s["email"], s["mfa_ok"], s["csrf_token"])
    for r in roles:
        if r["role"] == "superadmin":
            p.superadmin = True
        elif r["tenant_id"]:
            t = str(r["tenant_id"])
            cur = p.tenant_roles.get(t)
            if not cur or _ROLE_LEVEL[r["role"]] > _ROLE_LEVEL[cur]:
                p.tenant_roles[t] = r["role"]
    return p


def check_csrf(p: Principal, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(p.csrf, header)


def mfa_setup(db: Database, box: SecretBox, p: Principal) -> dict:
    with db.transaction() as tx:
        u = tx.fetchone("SELECT totp_confirmed FROM app_user WHERE id = %(id)s FOR UPDATE",
                        {"id": p.user_id})
        if u["totp_confirmed"]:
            raise ServiceError("MFA už je nastavené")
        secret = totp.new_secret()
        sealed = box.seal(secret.encode(), _ctx(p.user_id))
        tx.execute(
            "UPDATE app_user SET totp_wrapped_key = %(w)s, totp_ciphertext = %(c)s WHERE id = %(id)s",
            {"w": sealed.wrapped_key, "c": sealed.ciphertext, "id": p.user_id},
        )
    return {"secret": secret, "uri": totp.provisioning_uri(secret, p.email)}


def mfa_verify(db: Database, box: SecretBox, p: Principal, code: str, ip: str | None = None) -> None:
    failed = False
    with db.transaction() as tx:
        u = tx.fetchone(
            """SELECT totp_wrapped_key, totp_ciphertext, totp_confirmed, totp_last_step
               FROM app_user WHERE id = %(id)s FOR UPDATE""",
            {"id": p.user_id},
        )
        if not u["totp_ciphertext"]:
            raise AuthError("MFA není nastavené")
        secret = box.open(SealedSecret(bytes(u["totp_wrapped_key"]), bytes(u["totp_ciphertext"]), 1),
                          _ctx(p.user_id)).decode()
        step = totp.verify(secret, code or "")
        if step is None or (u["totp_last_step"] is not None and step <= u["totp_last_step"]):
            audit(tx, "auth.mfa_failed", user_id=p.user_id, target=f"user:{p.user_id}", ip=ip)
            failed = True
        else:
            tx.execute("UPDATE app_user SET totp_confirmed = true, totp_last_step = %(s)s WHERE id = %(id)s",
                       {"s": step, "id": p.user_id})
            tx.execute("UPDATE app_session SET mfa_ok = true WHERE id = %(id)s", {"id": p.session_id})
            audit(tx, "auth.login", user_id=p.user_id, target=f"user:{p.user_id}",
                  after={"mfa_enrolled": not u["totp_confirmed"]}, ip=ip)
    if failed:
        raise AuthError("neplatný kód")


def logout(db: Database, p: Principal) -> None:
    with db.transaction() as tx:
        tx.execute("DELETE FROM app_session WHERE id = %(id)s", {"id": p.session_id})


def purge_expired_sessions(tx: Tx) -> int:
    return tx.execute("DELETE FROM app_session WHERE expires_at < now() - interval '1 day'")
