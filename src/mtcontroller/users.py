"""Uživatelé tenanta a vlastní účet.

- Přidání uživatele: existující účet (podle e-mailu) jen dostane roli v tenantovi;
  nový účet dostane náhodné dočasné heslo, které se ukáže jednou (nikde se neloguje)
  a MFA si nastaví při prvním přihlášení.
- Role v tenantovi: tenant_admin, technician, readonly (superadmin jen z CLI).
- Sám sobě nikdo roli neubere ani se neodebere (aby se tenant nezamkl).
- Změna hesla vyžaduje staré heslo a ukončí ostatní relace.
- Reset MFA (ztracený telefon) smí jen superadmin; ukončí relace uživatele.
"""

from __future__ import annotations

from typing import Any

from .auth import Principal
from .db import Database, tenant_scope
from .security import hash_password, random_password, verify_password
from .services import NotFound, ServiceError, audit

TENANT_ROLES = ("tenant_admin", "technician", "readonly")


def list_users(db: Database, tenant_id: Any) -> list[dict]:
    with tenant_scope(db, tenant_id) as tx:
        return tx.fetchall(
            """SELECT u.id, u.email, u.is_active, rb.role::text AS role, u.totp_confirmed AS mfa,
                      (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked,
                      (SELECT max(s.created_at) FROM app_session s WHERE s.user_id = u.id) AS last_login
               FROM role_binding rb JOIN app_user u ON u.id = rb.user_id
               WHERE rb.tenant_id = %(t)s AND rb.site_id IS NULL ORDER BY u.email""",
            {"t": str(tenant_id)})


def add_user(db: Database, tenant_id: Any, email: str, role: str, actor: Principal,
             ip: str | None = None) -> dict:
    email = (email or "").strip().lower()
    if role not in TENANT_ROLES:
        raise ServiceError(f"role: {' | '.join(TENANT_ROLES)}")
    if "@" not in email or len(email) > 320 or any(c.isspace() for c in email):
        raise ServiceError("neplatný e-mail")
    temp = None
    with tenant_scope(db, tenant_id) as tx:
        u = tx.fetchone("SELECT id FROM app_user WHERE email = %(e)s", {"e": email})
        if u:
            uid = str(u["id"])
            if tx.fetchone("""SELECT 1 FROM role_binding WHERE user_id = %(u)s AND tenant_id = %(t)s
                              AND site_id IS NULL""", {"u": uid, "t": str(tenant_id)}):
                raise ServiceError("uživatel už v tenantovi je – změň mu roli")
        else:
            temp = random_password(18)
            uid = str(tx.fetchone("INSERT INTO app_user (email, password_hash) VALUES (%(e)s, %(h)s) RETURNING id",
                                  {"e": email, "h": hash_password(temp)})["id"])
        tx.execute("INSERT INTO role_binding (user_id, role, tenant_id) VALUES (%(u)s, %(r)s, %(t)s)",
                   {"u": uid, "r": role, "t": str(tenant_id)})
        audit(tx, "user.add", tenant_id=tenant_id, user_id=actor.user_id, target=f"user:{uid}",
              after={"email": email, "role": role, "new_account": temp is not None}, ip=ip)
    return {"user_id": uid, "email": email, "role": role, "temp_password": temp}


def _binding(tx, tenant_id: Any, user_id: str) -> dict:
    b = tx.fetchone("""SELECT rb.id, rb.role::text AS role, u.email FROM role_binding rb
                       JOIN app_user u ON u.id = rb.user_id
                       WHERE rb.user_id = %(u)s AND rb.tenant_id = %(t)s AND rb.site_id IS NULL""",
                    {"u": user_id, "t": str(tenant_id)})
    if not b:
        raise NotFound("uživatel v tenantovi není")
    return b


def set_role(db: Database, tenant_id: Any, user_id: str, role: str, actor: Principal,
             ip: str | None = None) -> None:
    if role not in TENANT_ROLES:
        raise ServiceError(f"role: {' | '.join(TENANT_ROLES)}")
    if user_id == actor.user_id:
        raise ServiceError("vlastní roli nelze měnit")
    with tenant_scope(db, tenant_id) as tx:
        b = _binding(tx, tenant_id, user_id)
        tx.execute("UPDATE role_binding SET role = %(r)s WHERE id = %(id)s", {"r": role, "id": str(b["id"])})
        audit(tx, "user.role", tenant_id=tenant_id, user_id=actor.user_id, target=f"user:{user_id}",
              before={"role": b["role"]}, after={"role": role}, ip=ip)


def remove_user(db: Database, tenant_id: Any, user_id: str, actor: Principal, ip: str | None = None) -> None:
    if user_id == actor.user_id:
        raise ServiceError("sám sebe odebrat nelze")
    with tenant_scope(db, tenant_id) as tx:
        b = _binding(tx, tenant_id, user_id)
        tx.execute("DELETE FROM role_binding WHERE id = %(id)s", {"id": str(b["id"])})
        # bez žádné role nemá smysl držet relace
        if not tx.fetchone("SELECT 1 FROM role_binding WHERE user_id = %(u)s", {"u": user_id}):
            tx.execute("DELETE FROM app_session WHERE user_id = %(u)s", {"u": user_id})
        audit(tx, "user.remove", tenant_id=tenant_id, user_id=actor.user_id, target=f"user:{user_id}",
              before={"email": b["email"], "role": b["role"]}, ip=ip)


def change_password(db: Database, actor: Principal, old: str, new: str, ip: str | None = None) -> None:
    if not new or len(new) < 12:
        raise ServiceError("nové heslo musí mít alespoň 12 znaků")
    if new == old:
        raise ServiceError("nové heslo musí být jiné než současné")
    failed = False
    with db.transaction() as tx:
        u = tx.fetchone("SELECT password_hash FROM app_user WHERE id = %(u)s FOR UPDATE", {"u": actor.user_id})
        if not u or not verify_password(old or "", u["password_hash"]):
            audit(tx, "auth.password_change_failed", user_id=actor.user_id, target=f"user:{actor.user_id}", ip=ip)
            failed = True  # audit se commitne, chyba až po transakci
        else:
            tx.execute("UPDATE app_user SET password_hash = %(h)s WHERE id = %(u)s",
                       {"h": hash_password(new), "u": actor.user_id})
            tx.execute("DELETE FROM app_session WHERE user_id = %(u)s AND id <> %(s)s",
                       {"u": actor.user_id, "s": actor.session_id})
            audit(tx, "auth.password_changed", user_id=actor.user_id, target=f"user:{actor.user_id}", ip=ip)
    if failed:
        raise ServiceError("současné heslo nesouhlasí")


def reset_mfa(db: Database, user_id: str, actor: Principal, ip: str | None = None) -> None:
    if not actor.superadmin:
        raise ServiceError("MFA může resetovat jen superadmin")
    with db.transaction() as tx:
        n = tx.execute("""UPDATE app_user SET totp_ciphertext = NULL, totp_wrapped_key = NULL,
                                 totp_confirmed = false, totp_last_step = NULL
                          WHERE id = %(u)s""", {"u": user_id})
        if not n:
            raise NotFound("uživatel neexistuje")
        tx.execute("DELETE FROM app_session WHERE user_id = %(u)s", {"u": user_id})
        audit(tx, "auth.mfa_reset", user_id=actor.user_id, target=f"user:{user_id}", ip=ip)
