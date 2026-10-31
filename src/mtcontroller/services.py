"""Doménová logika: tenanti, lokality, adopce, schválení, check-in, příkazy.

Funkce berou `db` (rozhraní z db.py) a řídí si transakce samy. Každá akce
měnící stav zapisuje audit_log. Přístup k datům tenanta jde přes tenant_scope;
operator_scope jen tam, kde tenant ještě není známý (vyhledání podle tokenu
nebo klíče) nebo jde o operaci provozovatele.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from . import ipam, routeros
from .config import Settings
from .db import Database, DatabaseError, Tx, operator_scope, tenant_scope
from .security import SealedSecret, SecretBox, new_token, random_password, token_hash
from .tunnel import TunnelAccount, TunnelProvider

log = logging.getLogger("mtc.services")


class ServiceError(Exception):
    """Chyba, kterou lze ukázat uživateli (400/409)."""


class NotFound(ServiceError):
    pass


# --- audit -------------------------------------------------------------------


def audit(tx: Tx, action: str, *, tenant_id: Any = None, user_id: Any = None,
          target: str | None = None, before: dict | None = None,
          after: dict | None = None, ip: str | None = None) -> None:
    tx.execute(
        """INSERT INTO audit_log (tenant_id, user_id, action, target, before, after, source_ip)
           VALUES (%(t)s, %(u)s, %(a)s, %(tg)s, %(b)s, %(af)s, %(ip)s)""",
        {"t": _s(tenant_id), "u": _s(user_id), "a": action, "tg": target,
         "b": before, "af": after, "ip": ip},
    )
    log.info("audit %s target=%s tenant=%s user=%s", action, target, tenant_id, user_id)


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


# --- tajemství -----------------------------------------------------------------


def _ctx(device_id: Any, kind: str) -> bytes:
    return f"device:{device_id}:{kind}".encode()


def put_secret(tx: Tx, box: SecretBox, tenant_id: Any, device_id: Any, kind: str, value: str) -> None:
    sealed = box.seal(value.encode(), _ctx(device_id, kind))
    tx.execute(
        """INSERT INTO device_secret (tenant_id, device_id, kind, wrapped_key, ciphertext, key_version)
           VALUES (%(t)s, %(d)s, %(k)s, %(w)s, %(c)s, %(v)s)
           ON CONFLICT (device_id, kind) DO UPDATE
             SET wrapped_key = EXCLUDED.wrapped_key, ciphertext = EXCLUDED.ciphertext,
                 key_version = EXCLUDED.key_version, rotated_at = now()""",
        {"t": str(tenant_id), "d": str(device_id), "k": kind, "w": sealed.wrapped_key,
         "c": sealed.ciphertext, "v": sealed.key_version},
    )


def get_secret(tx: Tx, box: SecretBox, device_id: Any, kind: str) -> str | None:
    row = tx.fetchone(
        "SELECT wrapped_key, ciphertext, key_version FROM device_secret WHERE device_id = %(d)s AND kind = %(k)s",
        {"d": str(device_id), "k": kind},
    )
    if not row:
        return None
    sealed = SealedSecret(bytes(row["wrapped_key"]), bytes(row["ciphertext"]), row["key_version"])
    return box.open(sealed, _ctx(device_id, kind)).decode()


# --- tenanti a lokality ------------------------------------------------------------


def create_tenant(db: Database, settings: Settings, name: str, user_id: Any = None,
                  ip: str | None = None) -> dict:
    name = _clean_name(name)
    with operator_scope(db) as tx:
        used = [r["b"] for r in tx.fetchall("SELECT tunnel_block::text AS b FROM tenant")]
        block = ipam.next_tenant_block(settings.tunnel.pool, settings.tunnel.tenant_block_prefix, used)
        try:
            row = tx.fetchone(
                """INSERT INTO tenant (name, tunnel_block) VALUES (%(n)s, %(b)s)
                   RETURNING id, name, tunnel_block::text AS tunnel_block, created_at""",
                {"n": name, "b": str(block)},
            )
        except DatabaseError as e:
            if e.is_unique_violation:
                raise ServiceError(f"tenant {name!r} už existuje") from e
            raise
        audit(tx, "tenant.create", tenant_id=row["id"], user_id=user_id,
              target=f"tenant:{row['id']}", after={"name": name, "block": str(block)}, ip=ip)
        return row


def create_site(db: Database, tenant_id: Any, name: str, address: str | None = None,
                user_id: Any = None, ip: str | None = None) -> dict:
    name = _clean_name(name)
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM tenant WHERE id = %(t)s", {"t": str(tenant_id)}):
            raise NotFound("tenant neexistuje")
        try:
            row = tx.fetchone(
                """INSERT INTO site (tenant_id, name, address) VALUES (%(t)s, %(n)s, %(a)s)
                   RETURNING id, tenant_id, name, address, created_at""",
                {"t": str(tenant_id), "n": name, "a": address},
            )
        except DatabaseError as e:
            if e.is_unique_violation:
                raise ServiceError(f"lokalita {name!r} už existuje") from e
            raise
        audit(tx, "site.create", tenant_id=tenant_id, user_id=user_id,
              target=f"site:{row['id']}", after={"name": name}, ip=ip)
        return row


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 200 or any(ord(c) < 32 for c in name):
        raise ServiceError("neplatné jméno")
    return name


# --- adopce --------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptionTokenOut:
    token: str
    expires_at: Any
    command: str


def create_adoption_token(db: Database, settings: Settings, tenant_id: Any, site_id: Any,
                          user_id: Any, ip: str | None = None) -> AdoptionTokenOut:
    token = new_token()
    ttl = settings.security.adoption_token_ttl_minutes
    with tenant_scope(db, tenant_id) as tx:
        if site_id and not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        row = tx.fetchone(
            """INSERT INTO adoption_token (tenant_id, site_id, token_hash, created_by, expires_at)
               VALUES (%(t)s, %(s)s, %(h)s, %(u)s, now() + make_interval(mins => %(ttl)s))
               RETURNING id, expires_at""",
            {"t": str(tenant_id), "s": _s(site_id), "h": token_hash(token), "u": str(user_id), "ttl": ttl},
        )
        audit(tx, "adoption_token.create", tenant_id=tenant_id, user_id=user_id,
              target=f"adoption_token:{row['id']}", after={"site_id": _s(site_id), "ttl_min": ttl}, ip=ip)
    base = f"https://{settings.web.checkin_host}"
    opts = routeros.fetch_opts(settings)
    return AdoptionTokenOut(token, row["expires_at"], routeros.adopt_one_liner(base, token, opts))


def adoption_token_valid(db: Database, token: str) -> bool:
    with operator_scope(db) as tx:
        return tx.fetchone(
            """SELECT 1 FROM adoption_token WHERE token_hash = %(h)s
               AND used_at IS NULL AND expires_at > now()""",
            {"h": token_hash(token)},
        ) is not None


_SERIAL = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_VERSION = re.compile(r"^[0-9A-Za-z. ()_-]{1,50}$")
_MODEL = re.compile(r"^[\x20-\x7e]{0,100}$")
_MAC = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$|^$")


@dataclass(frozen=True)
class Registration:
    device_id: uuid.UUID
    tenant_id: uuid.UUID
    checkin_key: str


def _claim(tx: Tx, token: str, enroll_key: str | None) -> dict | None:
    """Oprávnění k registraci: jednorázový adopční token, nebo klíč lokality (výchozí konfigurace)."""
    if enroll_key is not None:
        r = tx.fetchone("""SELECT tenant_id, site_id FROM site_policy WHERE module = 'enroll'
                           AND data->>'key_hash' = %(h)s""", {"h": token_hash(enroll_key)})
        return {"id": None, "tenant_id": r["tenant_id"], "site_id": r["site_id"]} if r else None
    return tx.fetchone(
        """SELECT id, tenant_id, site_id FROM adoption_token
           WHERE token_hash = %(h)s AND used_at IS NULL AND expires_at > now()
           FOR UPDATE""",
        {"h": token_hash(token)},
    )


def register_device(db: Database, box: SecretBox, token: str, payload: dict,
                    ip: str | None = None, enroll_key: str | None = None) -> Registration:
    serial = str(payload.get("serial", ""))
    model = str(payload.get("model", ""))[:100]
    version = str(payload.get("version", ""))
    mac = str(payload.get("mac", ""))
    if not _SERIAL.match(serial):
        raise ServiceError("neplatné sériové číslo")
    if not _VERSION.match(version) or not _MODEL.match(model) or not _MAC.match(mac):
        raise ServiceError("neplatná identita zařízení")
    if not version.startswith("7."):
        raise ServiceError(f"podporován je jen RouterOS v7 (zařízení má {version})")
    from .defaults import DEVICE_ROLE

    profile = payload.get("role") if payload.get("role") in DEVICE_ROLE else None

    key = new_token()
    with operator_scope(db) as tx:
        tok = _claim(tx, token, enroll_key)
        if not tok:
            raise NotFound("adopční token je neplatný, použitý nebo prošlý")
        tenant_id = tok["tenant_id"]
        existing = tx.fetchone("SELECT id, tenant_id, state FROM device WHERE serial = %(s)s", {"s": serial})
        if existing and (existing["tenant_id"] != tenant_id or existing["state"] not in ("pending", "removed")):
            # odmítnutí se zapíše (commit), token zůstává nepoužitý
            audit(tx, "device.register_rejected", tenant_id=tenant_id, target=f"serial:{serial}",
                  after={"reason": "already managed", "state": existing["state"]}, ip=ip)
            rejected = True
        else:
            rejected = False
    if rejected:
        raise ServiceError("zařízení je už spravované; nejdřív ho odeber ze správy")
    with operator_scope(db) as tx:
        tok = _claim(tx, token, enroll_key)
        if not tok:
            raise NotFound("adopční token je neplatný, použitý nebo prošlý")
        tenant_id = tok["tenant_id"]
        existing = tx.fetchone("SELECT id, tenant_id, state FROM device WHERE serial = %(s)s FOR UPDATE",
                               {"s": serial})
        if existing and (existing["tenant_id"] != tenant_id or existing["state"] not in ("pending", "removed")):
            raise ServiceError("zařízení je už spravované; nejdřív ho odeber ze správy")
        if existing:
            device_id = existing["id"]
            tx.execute(
                """UPDATE device SET state = 'pending', model = %(m)s, ros_version = %(v)s, mac = %(mac)s,
                       site_id = coalesce(%(site)s, site_id), checkin_key_hash = %(k)s,
                       api_cert_fingerprint = NULL, tunnel_ip = NULL, last_checkin_at = now(),
                       role = coalesce(%(r)s::device_role, role)
                   WHERE id = %(id)s""",
                {"m": model, "v": version, "mac": mac or None, "site": _s(tok["site_id"]),
                 "k": token_hash(key), "id": str(device_id), "r": DEVICE_ROLE.get(profile)},
            )
        else:
            device_id = tx.fetchone(
                """INSERT INTO device (tenant_id, site_id, serial, model, ros_version, mac,
                                       state, checkin_key_hash, last_checkin_at, role)
                   VALUES (%(t)s, %(site)s, %(s)s, %(m)s, %(v)s, %(mac)s, 'pending', %(k)s, now(),
                           coalesce(%(r)s::device_role, 'unknown'))
                   RETURNING id""",
                {"t": str(tenant_id), "site": _s(tok["site_id"]), "s": serial, "m": model,
                 "v": version, "mac": mac or None, "k": token_hash(key), "r": DEVICE_ROLE.get(profile)},
            )["id"]
        put_secret(tx, box, tenant_id, device_id, "checkin_key", key)
        if tok["id"] is not None:
            tx.execute(
                "UPDATE adoption_token SET used_at = now(), used_by_device = %(d)s WHERE id = %(id)s",
                {"d": str(device_id), "id": str(tok["id"])},
            )
        if profile in ("switch", "ap", "switch_ap", "switch_capsman"):
            # role řízeného režimu podle výchozí konfigurace (AP / switch)
            tx.execute(
                """INSERT INTO device_intent (tenant_id, device_id, role) VALUES (%(t)s, %(d)s, %(r)s)
                   ON CONFLICT (device_id) DO UPDATE SET role = EXCLUDED.role""",
                {"t": str(tenant_id), "d": str(device_id), "r": DEVICE_ROLE[profile]})
        audit(tx, "device.register", tenant_id=tenant_id, target=f"device:{device_id}",
              after={"serial": serial, "model": model, "version": version, "via": "enroll" if enroll_key else "token",
                     "profile": profile}, ip=ip)
    return Registration(device_id, tenant_id, key)


# --- klíč lokality pro výchozí konfiguraci (ohlášení bez jednorázového tokenu) ----------------

def site_enroll_key(db: Database, box: SecretBox, tenant_id: Any, site_id: Any, rotate: bool = False,
                    user_id: Any = None, ip: str | None = None) -> str:
    """Klíč lokality vložený do výchozí konfigurace. V DB jen hash (vyhledání) a šifrovaná
    podoba (aby šel znovu zobrazit v UI). Rotace = staré skripty přestanou fungovat."""
    import base64

    ctx = f"enroll:{site_id}".encode()
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = 'enroll'", {"s": str(site_id)})
        if r and not rotate:
            d = r["data"]
            return box.open(SealedSecret(base64.b64decode(d["wrapped"]), base64.b64decode(d["ct"]), d["kv"]), ctx).decode()
        key = new_token()
        sealed = box.seal(key.encode(), ctx)
        data = {"key_hash": token_hash(key), "wrapped": base64.b64encode(sealed.wrapped_key).decode(),
                "ct": base64.b64encode(sealed.ciphertext).decode(), "kv": sealed.key_version}
        tx.execute(
            """INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
               VALUES (%(t)s, %(s)s, 'enroll', %(d)s, %(u)s)
               ON CONFLICT (site_id, module) DO UPDATE SET data = EXCLUDED.data, updated_by = EXCLUDED.updated_by,
                   updated_at = now()""", {"t": str(tenant_id), "s": str(site_id), "d": data, "u": _s(user_id)})
        audit(tx, "site.enroll_key", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"rotated": bool(r)}, ip=ip)
    return key


def enroll_key_valid(db: Database, key: str) -> bool:
    if not key or len(key) > 100:
        return False
    with operator_scope(db) as tx:
        return tx.fetchone("SELECT 1 FROM site_policy WHERE module = 'enroll' AND data->>'key_hash' = %(h)s",
                           {"h": token_hash(key)}) is not None


# --- schválení -------------------------------------------------------------------


def sstp_username(serial: str) -> str:
    return f"mtc-{serial}"


def approve_device(db: Database, settings: Settings, box: SecretBox, providers: list[TunnelProvider],
                   tenant_id: Any, device_id: Any, site_id: Any, role: str = "unknown",
                   user_id: Any = None, ip: str | None = None, identity: str | None = None,
                   target: str | None = None, ap_group: str | None = None, can_apply: bool = True) -> list[str]:
    """Přidělí tunelovou IP, vygeneruje hesla, naplánuje provisioning a založí
    účty na koncentrátorech. Vrací seznam chyb koncentrátorů (worker je později
    dorovná), schválení samotné tím neselže.
    `target` (switch / ap / monitor) = adopce na jeden krok: worker zařízení dovede do řízení."""
    if target is not None:
        from .adoptflow import TARGETS

        if target not in TARGETS:
            raise ServiceError("role: switch, AP + switch, nebo jen sledovat")
        role = {"switch": "switch", "ap": "ap"}.get(target, role if role in ("gateway", "ptp") else "gateway")
    local = settings.tunnel.local
    if not local and not settings.tunnel.concentrators:
        raise ServiceError("v konfiguraci není žádný koncentrátor (tunnel.concentrators)")
    if role not in DEVICE_ROLES:
        raise ServiceError("neplatná role zařízení")
    identity = check_identity(identity) if identity not in (None, "") else None
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone(
            "SELECT id, serial, state, identity, host(tunnel_ip) AS tunnel_ip FROM device WHERE id = %(d)s FOR UPDATE",
            {"d": str(device_id)},
        )
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] != "pending":
            raise ServiceError(f"zařízení není ve stavu pending (je {dev['state']})")
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        if local:
            # lokální režim: správa na adresu, ze které se zařízení hlásí (check-in ji aktualizuje)
            tunnel_ip = dev["tunnel_ip"]
            if not tunnel_ip:
                raise ServiceError("adresa zařízení zatím není známá – počkej na jeho další check-in")
            sstp_pw, concentrator = "", None
        else:
            block = tx.fetchone("SELECT tunnel_block::text AS b FROM tenant WHERE id = %(t)s",
                                {"t": str(tenant_id)})["b"]
            used = [r["ip"] for r in tx.fetchall(
                "SELECT host(tunnel_ip) AS ip FROM device WHERE tunnel_ip IS NOT NULL")]
            tunnel_ip = dev["tunnel_ip"] or str(ipam.next_device_address([block], used))
            sstp_pw = random_password()
            put_secret(tx, box, tenant_id, device_id, "sstp_password", sstp_pw)
            concentrator = settings.tunnel.concentrators[0].name
        api_pw = random_password()
        put_secret(tx, box, tenant_id, device_id, "api_password", api_pw)
        tx.execute(
            """UPDATE device SET state = 'provisioning', site_id = %(s)s, role = %(r)s,
                   tunnel_ip = %(ip)s, approved_by = %(u)s, approved_at = now(), concentrator = %(c)s
               WHERE id = %(d)s""",
            {"s": str(site_id), "r": role, "ip": tunnel_ip, "u": _s(user_id), "c": concentrator,
             "d": str(device_id)},
        )
        enqueue(tx, tenant_id, device_id, "provision", {}, user_id)
        if target is not None:
            from . import adoptflow

            if ap_group is not None:
                groups = {g["id"] for g in ((_site_intent(tx, site_id) or {}).get("wifi") or {}).get("groups") or []}
                if ap_group not in groups:
                    raise ServiceError("skupina AP neexistuje")
            adoptflow.start(tx, tenant_id, device_id, target, ap_group, user_id, can_apply)
        if identity and identity != dev["identity"]:
            _identity_unique(tx, site_id, device_id, identity)
            # jméno až po provisioningu (fronta jde po pořadí)
            enqueue(tx, tenant_id, device_id, "set_identity", {"name": identity, "old": dev["identity"]}, user_id)
        audit(tx, "device.approve", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"site_id": str(site_id), "role": role, "tunnel_ip": tunnel_ip, "identity": identity,
                     "target": target, "ap_group": ap_group}, ip=ip)
        serial = dev["serial"]
        comment = f"{tenant_id}/{site_id}"
    errors: list[str] = []
    if local:
        return errors
    account = TunnelAccount(sstp_username(serial), sstp_pw, tunnel_ip, comment)
    for p in providers:
        try:
            p.ensure_account(account)
        except Exception as e:  # koncentrátor nedostupný – dorovná worker
            log.warning("koncentrátor %s: ensure_account selhal: %s", p.name, e)
            errors.append(f"{p.name}: {e}")
    return errors


DEVICE_ROLES = {"unknown", "gateway", "switch", "ap", "ptp"}


def _site_intent(tx: Tx, site_id: Any) -> dict | None:
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = 'intent'", {"s": str(site_id)})
    return r["data"] if r else None


def update_device(db: Database, tenant_id: Any, device_id: Any, changes: dict,
                  user_id: Any = None, ip: str | None = None) -> dict:
    """Úprava schváleného zařízení v controlleru (lokalita, role, poznámka).
    Na zařízení nic nemění."""
    allowed = {"site_id", "role", "note"}
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ServiceError(f"lze měnit jen {', '.join(sorted(allowed))}")
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT site_id, role::text AS role, note, state FROM device WHERE id = %(d)s",
                          {"d": str(device_id)})
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] in ("pending", "removed"):
            raise ServiceError("čekající zařízení se upravuje při schválení")
        new = {"site_id": str(dev["site_id"]) if dev["site_id"] else None, "role": dev["role"], "note": dev["note"]}
        if "site_id" in changes:
            sid = str(changes["site_id"] or "")
            try:
                uuid.UUID(sid)
            except ValueError:
                raise ServiceError("neplatná lokalita") from None
            if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": sid}):
                raise NotFound("lokalita neexistuje")
            new["site_id"] = sid
        if "role" in changes:
            if changes["role"] not in DEVICE_ROLES:
                raise ServiceError("neplatná role zařízení")
            new["role"] = changes["role"]
        if "note" in changes:
            note = str(changes["note"] or "").strip()
            if len(note) > 500 or not re.fullmatch(r"[^\x00-\x08\x0b-\x1f\x7f]*", note):
                raise ServiceError("poznámka: max. 500 znaků bez řídicích znaků")
            new["note"] = note
        before = {k: (str(dev[k]) if k == "site_id" and dev[k] else dev[k]) for k in new}
        if new == before:
            return new
        tx.execute("UPDATE device SET site_id = %(s)s, role = %(r)s, note = %(n)s WHERE id = %(d)s",
                   {"s": new["site_id"], "r": new["role"], "n": new["note"], "d": str(device_id)})
        audit(tx, "device.update", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              before={k: v for k, v in before.items() if v != new[k]},
              after={k: v for k, v in new.items() if v != before[k]}, ip=ip)
        return new


def forget_device(db: Database, tenant_id: Any, device_id: Any, user_id: Any = None, ip: str | None = None) -> dict:
    """Odebrání jen v controlleru – pro zařízení, které nemůže provést úklid samo
    (nedostupné, zaseknuté v provisioningu, rozbité). Zneplatní klíče, zruší frontu
    a plány; účet na koncentrátoru vypne worker. Zařízení pak jde adoptovat znovu.
    Na zařízení zůstanou objekty mtc – adopce je přepíše, nebo je smaž ručně."""
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT serial, state FROM device WHERE id = %(d)s FOR UPDATE", {"d": str(device_id)})
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] == "removed":
            raise ServiceError("zařízení už je odebrané")
        d = str(device_id)
        n_cmd = tx.execute("""UPDATE device_command SET status = 'cancelled', finished_at = now()
                              WHERE device_id = %(d)s AND status IN ('queued', 'sent')""", {"d": d})
        tx.execute("""UPDATE config_plan SET status = 'discarded'
                      WHERE device_id = %(d)s AND status IN ('draft', 'queued', 'confirming')""", {"d": d})
        tx.execute("UPDATE alert SET resolved_at = now() WHERE device_id = %(d)s AND resolved_at IS NULL", {"d": d})
        tx.execute("DELETE FROM device_secret WHERE device_id = %(d)s", {"d": d})
        tx.execute("DELETE FROM device_intent WHERE device_id = %(d)s", {"d": d})
        tx.execute(
            """UPDATE device SET state = 'removed', checkin_key_hash = NULL, tunnel_ip = NULL,
                   api_cert_fingerprint = NULL, managed_at = NULL WHERE id = %(d)s""", {"d": d})
        audit(tx, "device.forgotten", tenant_id=tenant_id, user_id=user_id, target=f"device:{d}",
              before={"state": dev["state"]}, after={"serial": dev["serial"], "cancelled_commands": n_cmd}, ip=ip)
    return {"serial": dev["serial"], "previous_state": dev["state"]}


# --- identita (jméno zařízení v RouterOS) ------------------------------------------

IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


def check_identity(name: Any) -> str:
    """Jméno zařízení: ASCII písmena, číslice, tečka, pomlčka, podtržítko (skripty jsou čisté ASCII,
    jméno se používá v CAPsMANu a v DNS/LLDP), 1–63 znaků."""
    name = str(name or "").strip()
    if not IDENTITY_RE.fullmatch(name):
        raise ServiceError("jméno zařízení: 1–63 znaků, písmena bez diakritiky, číslice, . - _ (bez mezer)")
    return name


def identity_body(name: Any) -> str:
    return f"/system identity set name={routeros.q(check_identity(name))}\n"


def cap_names_body(names: list) -> str:
    """CAP se drží CAPsMANu podle jména – při přejmenování hostitele dočasně obě jména."""
    names = [check_identity(n) for n in names]
    if not names:
        raise ServiceError("cap_names bez jmen")
    return f"/interface wifi cap set caps-man-names={routeros.q(','.join(names))}\n"


def _identity_unique(tx: Tx, site_id: Any, device_id: Any, name: str) -> None:
    if tx.fetchone("""SELECT 1 FROM device WHERE site_id = %(s)s AND id <> %(d)s AND state <> 'removed'
                      AND lower(identity) = lower(%(n)s)""", {"s": str(site_id), "d": str(device_id), "n": name}):
        raise ServiceError(f"jméno {name} už na lokalitě má jiné zařízení")


def _capsman_aps(tx: Tx, device_id: Any) -> list[str]:
    """Řízená AP lokality, jejichž CAPsMAN běží na tomto zařízení (CAP se drží jeho jména)."""
    r = tx.fetchone("""SELECT p.site_id FROM site_policy p JOIN device d ON d.site_id = p.site_id
                       WHERE d.id = %(d)s AND p.module = 'intent'
                         AND p.data->'wifi'->>'capsman_device_id' = %(ds)s::text""",
                    {"d": str(device_id), "ds": str(device_id)})
    if not r:
        return []
    return [str(x["id"]) for x in tx.fetchall(
        """SELECT id FROM device WHERE site_id = %(s)s AND role = 'ap' AND managed_at IS NOT NULL
             AND state IN ('adopted', 'offline') AND id <> %(d)s""", {"s": str(r["site_id"]), "d": str(device_id)})]


def set_identity(db: Database, tenant_id: Any, device_id: Any, name: Any, user_id: Any = None,
                 ip: str | None = None) -> dict:
    """Přejmenování zařízení v RouterOS (/system identity). V DB se jméno změní až po potvrzení
    zařízením. Běží-li na něm CAPsMAN, AP nejdřív dostanou staré i nové jméno (bez výpadku Wi-Fi)
    a po přejmenování už jen nové."""
    name = check_identity(name)
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT state, identity, site_id FROM device WHERE id = %(d)s", {"d": str(device_id)})
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] not in ("provisioning", "adopted", "offline"):
            raise ServiceError(f"jméno nelze poslat zařízení ve stavu {dev['state']}")
        if dev["site_id"]:
            _identity_unique(tx, dev["site_id"], device_id, name)
        if name == dev["identity"]:
            return {"name": name, "unchanged": True}
        # starší čekající přejmenování nahradit
        tx.execute("""UPDATE device_command SET status = 'cancelled', finished_at = now() WHERE device_id = %(d)s
                      AND kind = 'set_identity' AND status = 'queued'""", {"d": str(device_id)})
        aps = _capsman_aps(tx, device_id)
        for ap in aps:
            enqueue(tx, tenant_id, ap, "cap_names", {"names": [n for n in (dev["identity"], name) if n]}, user_id)
        cid = enqueue(tx, tenant_id, device_id, "set_identity", {"name": name, "old": dev["identity"]}, user_id)
        audit(tx, "device.identity", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              before={"identity": dev["identity"]}, after={"identity": name, "command_id": cid, "caps": len(aps)}, ip=ip)
    return {"name": name, "command_id": cid, "aps": aps}


def _identity_done(tx: Tx, tenant_id: Any, device_id: Any, command_id: int) -> None:
    """Zařízení se přejmenovalo: jméno do DB, AP s CAPsMANem na něm už jen na nové jméno."""
    c = tx.fetchone("SELECT params FROM device_command WHERE id = %(c)s", {"c": command_id})
    name = ((c or {}).get("params") or {}).get("name")
    if not name:
        return
    tx.execute("UPDATE device SET identity = %(n)s, inventory_at = NULL WHERE id = %(d)s",
               {"n": name, "d": str(device_id)})
    for ap in _capsman_aps(tx, device_id):
        enqueue(tx, tenant_id, ap, "cap_names", {"names": [name]})


# --- příkazy -----------------------------------------------------------------------

COMMAND_KINDS = {"provision", "switch_concentrator", "reboot", "deprovision", "apply_plan", "upgrade", "firmware",
                 "confirm_plan", "poe_cycle", "wifi_package", "set_identity", "cap_names", "syslog", "port_bounce",
                 "checkin_update"}
USER_COMMANDS = {"reboot", "deprovision", "provision", "upgrade", "firmware", "poe_cycle", "wifi_package", "port_bounce"}


def enqueue(tx: Tx, tenant_id: Any, device_id: Any, kind: str, params: dict,
            user_id: Any = None) -> int:
    if kind not in COMMAND_KINDS:
        raise ServiceError(f"neznámý příkaz {kind}")
    row = tx.fetchone(
        """INSERT INTO device_command (tenant_id, device_id, kind, params, created_by)
           VALUES (%(t)s, %(d)s, %(k)s, %(p)s, %(u)s) RETURNING id""",
        {"t": str(tenant_id), "d": str(device_id), "k": kind, "p": params, "u": _s(user_id)},
    )
    return row["id"]


def request_command(db: Database, tenant_id: Any, device_id: Any, kind: str,
                    user_id: Any = None, ip: str | None = None, params: dict | None = None) -> int:
    """Příkaz vyžádaný uživatelem (reboot, deprovision, opakovaný provision,
    aktualizace RouterOS a firmwaru)."""
    if kind not in USER_COMMANDS:
        raise ServiceError("tento příkaz nelze vyžádat ručně")
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT state, ros_version FROM device WHERE id = %(d)s", {"d": str(device_id)})
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] not in ("provisioning", "adopted", "offline"):
            raise ServiceError(f"příkaz nelze poslat zařízení ve stavu {dev['state']}")
        p: dict = {}
        if kind in ("upgrade", "firmware"):
            if dev["state"] != "adopted":
                raise ServiceError("aktualizaci lze poslat jen spravovanému zařízení, které je online")
            if tx.fetchone("""SELECT 1 FROM device_command WHERE device_id = %(d)s
                              AND kind IN ('upgrade', 'firmware') AND status IN ('queued', 'sent')""",
                           {"d": str(device_id)}):
                raise ServiceError("aktualizace tohoto zařízení už čeká na provedení")
        if kind == "upgrade":
            p = _upgrade_params(tx, device_id, dev["ros_version"] or "", str((params or {}).get("version", "")))
        if kind in ("poe_cycle", "port_bounce"):
            from . import inventory, portcfg

            port = str((params or {}).get("port", ""))
            try:
                inv = inventory.load(tx, device_id, ["ethernet", "interfaces"])
                if kind == "poe_cycle":
                    portcfg.poe_cycle_body(inv, port)
                else:
                    portcfg.bounce_body(inv, port)
            except (portcfg.PortError, ValueError) as e:
                raise ServiceError(str(e)) from e
            p = {"port": port}
        if kind == "wifi_package":
            from . import inventory, packages

            if dev["state"] != "adopted":
                raise ServiceError("výměnu balíčku lze poslat jen zařízení, které je online")
            try:
                packages.switch_body(inventory.load(tx, device_id, ["packages", "resource", "wifi", "wireless", "wifi_radios"]))
            except ValueError as e:
                raise ServiceError(str(e)) from e
        cid = enqueue(tx, tenant_id, device_id, kind, p, user_id)
        audit(tx, f"device.command.{kind}", tenant_id=tenant_id, user_id=user_id,
              target=f"device:{device_id}", after={"command_id": cid, **p}, ip=ip)
        return cid


def _upgrade_params(tx: Tx, device_id: Any, current: str, requested: str) -> dict:
    """Cílová verze musí být ta, kterou zařízení samo nahlásilo jako dostupnou
    ve svém kanálu (uživatel potvrzuje přesně to, co viděl), novější než
    stávající a bez kritického známého problému."""
    from . import ros_versions

    row = tx.fetchone("""SELECT data->>'latest-version' AS v, data->>'channel' AS ch FROM device_inventory
                         WHERE device_id = %(d)s AND section = 'update'""", {"d": str(device_id)})
    latest = (row or {}).get("v") or ""
    if not latest:
        raise ServiceError("dostupná verze zatím není známá – počkej na kontrolu aktualizací")
    if requested != latest:
        raise ServiceError(f"zařízení nabízí verzi {latest}, ne {requested or '?'} – obnov stránku")
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?(beta\d+|rc\d+)?", latest):
        raise ServiceError(f"neočekávaný formát verze {latest!r}")
    if ros_versions.parse(latest) <= ros_versions.parse(current):
        raise ServiceError(f"verze {latest} není novější než nainstalovaná {current}")
    critical = [i.text for i in ros_versions.issues_for(latest) if i.severity == "critical"]
    if critical:
        raise ServiceError(f"verze {latest} má kritický známý problém: {critical[0]}")
    return {"version": latest, "channel": (row or {}).get("ch") or ""}


def render_command(tx: Tx, settings: Settings, box: SecretBox, cmd: dict, dev: dict) -> str:
    opts = routeros.fetch_opts(settings)
    key = get_secret(tx, box, dev["id"], "checkin_key") or ""
    ack_url = f"https://{settings.web.checkin_host}/api/checkin/ack?id={cmd['id']}"
    kind = cmd["kind"]
    if kind == "provision":
        body = routeros.provision_body(mgmt_params(
            settings, dev["serial"], dev.get("concentrator"),
            get_secret(tx, box, dev["id"], "sstp_password") or "",
            get_secret(tx, box, dev["id"], "api_password") or ""), opts)
        return routeros.wrap_command(ack_url, key, body, opts)
    if kind == "switch_concentrator":
        if settings.tunnel.local:
            raise ServiceError("lokální režim nemá koncentrátory")
        conc = _concentrator(settings, cmd["params"].get("concentrator"))
        return routeros.wrap_command(ack_url, key, routeros.switch_concentrator_body(conc.connect_host), opts)
    if kind == "reboot":
        return routeros.wrap_command(ack_url, key, "", opts, tail=routeros.REBOOT_TAIL)
    if kind == "checkin_update":
        url = f"https://{settings.web.checkin_host}/api/checkin"
        return routeros.wrap_command(ack_url, key, routeros.trust_ca_body(opts) + routeros.checkin_install(url, key, opts), opts)
    if kind == "deprovision":
        return routeros.wrap_command(ack_url, key, "", opts, tail=routeros.DEPROVISION_TAIL)
    if kind == "upgrade":
        version = str(cmd["params"].get("version", ""))
        return routeros.wrap_command(ack_url, key, routeros.upgrade_body(version), opts,
                                     tail=routeros.upgrade_tail(version))
    if kind == "firmware":
        return routeros.wrap_command(ack_url, key, routeros.FIRMWARE_BODY, opts, tail=routeros.FIRMWARE_TAIL)
    if kind == "apply_plan":
        from .plans import plan_script

        pl = plan_script(tx, int(cmd["params"]["plan_id"]), dev["id"])
        if pl["module"] == "takeover":
            from . import takeover

            return takeover.render_takeover(tx, settings, box, cmd, dev, ack_url, key)
        body = pl["script"]
        if pl["module"] == "capsman":
            # hesla SSID jen při doručení (v DB je skript s tokeny)
            from . import takeover, wificfg

            cache: dict[str, list] = {}

            def ppsk(h: str, i: int) -> str | None:
                # jedno dešifrování na SSID, ne na každé heslo
                if h not in cache:
                    sid = takeover._sid_from_hex(h)
                    cache[h] = takeover.ppsk_load(tx, box, sid) if sid else []
                return cache[h][i]["psk"] if 0 <= i < len(cache[h]) else None

            body = wificfg.fill_secrets(body, lambda h: takeover.ssid_psk(tx, box, h), ppsk)
        if pl["revert_script"]:
            body = routeros.safe_apply_body(body, pl["revert_script"], f"{ack_url}&status=reverted", key, opts,
                                            int(pl["confirm_minutes"] or 5))
        return routeros.wrap_command(ack_url, key, body, opts)
    if kind == "confirm_plan":
        mod = tx.fetchone("SELECT module FROM config_plan WHERE id = %(i)s", {"i": int(cmd["params"].get("plan_id") or 0)})
        if mod and mod["module"] == "takeover":
            from .switchcfg import TAKEOVER_CONFIRM

            return routeros.wrap_command(ack_url, key, TAKEOVER_CONFIRM, opts)
        return routeros.wrap_command(ack_url, key, routeros.CONFIRM_BODY, opts)
    if kind == "wifi_package":
        from . import inventory, packages

        inv = inventory.load(tx, dev["id"], ["packages", "resource", "wifi", "wireless", "wifi_radios"])
        return routeros.wrap_command(ack_url, key, packages.switch_body(inv, opts.check_certificate), opts,
                                     tail=packages.SWITCH_TAIL)
    if kind == "syslog":
        from . import devlog

        return routeros.wrap_command(ack_url, key, devlog.render(settings, cmd, dev), opts)
    if kind == "set_identity":
        return routeros.wrap_command(ack_url, key, identity_body(cmd["params"].get("name")), opts)
    if kind == "cap_names":
        return routeros.wrap_command(ack_url, key, cap_names_body(cmd["params"].get("names") or []), opts)
    if kind == "poe_cycle":
        from . import inventory, portcfg

        return routeros.wrap_command(ack_url, key, portcfg.poe_cycle_body(
            inventory.load(tx, dev["id"], ["ethernet"]), str(cmd["params"].get("port", ""))), opts)
    if kind == "port_bounce":
        from . import inventory, portcfg

        return routeros.wrap_command(ack_url, key, portcfg.bounce_body(
            inventory.load(tx, dev["id"], ["ethernet", "interfaces"]), str(cmd["params"].get("port", ""))), opts)
    raise ServiceError(f"neznámý příkaz {kind}")


def set_local_address(tx: Tx, tenant_id: Any, dev: dict, ip: str | None) -> None:
    """Lokální režim: zapamatuje adresu, ze které se zařízení hlásí – na ni pak jde
    správa (REST). Sloupec tunnel_ip tu znamená „adresa správy“. Adresu, kterou už
    drží jiné zařízení (NAT, souběh DHCP), nepřebírá – to by přesměrovalo správu."""
    if not ip or ip == dev.get("tunnel_ip"):
        return
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return
    if a.is_loopback or a.is_unspecified or a.is_multicast:
        return
    if tx.fetchone("SELECT 1 FROM device WHERE tunnel_ip = %(ip)s::inet AND id <> %(d)s",
                   {"ip": ip, "d": str(dev["id"])}):
        log.warning("zařízení %s se hlásí z %s, kterou už má jiné zařízení – adresa správy beze změny", dev["id"], ip)
        return
    tx.execute("UPDATE device SET tunnel_ip = %(ip)s::inet WHERE id = %(d)s", {"ip": ip, "d": str(dev["id"])})
    audit(tx, "device.address", tenant_id=tenant_id, target=f"device:{dev['id']}",
          before={"ip": dev.get("tunnel_ip")}, after={"ip": ip}, ip=ip)
    dev["tunnel_ip"] = ip


def mgmt_params(settings: Settings, serial: str, concentrator: str | None, sstp_pw: str,
                api_pw: str) -> routeros.ProvisionParams:
    """Parametry správy zařízení: přes tunel (koncentrátor src-natuje controller
    na svou tunelovou adresu), nebo v lokálním režimu přímo z adresy controlleru."""
    if settings.tunnel.local:
        return routeros.ProvisionParams(mgmt_source=settings.tunnel.controller_ip, connect_host="",
                                        sstp_user=sstp_username(serial), sstp_password="",
                                        api_password=api_pw, local=True)
    conc = _concentrator(settings, concentrator)
    return routeros.ProvisionParams(mgmt_source=str(ipam.concentrator_address(settings.tunnel.pool)),
                                    connect_host=conc.connect_host, sstp_user=sstp_username(serial),
                                    sstp_password=sstp_pw, api_password=api_pw)


def _concentrator(settings: Settings, name: str | None):
    for c in settings.tunnel.concentrators:
        if c.name == name:
            return c
    return settings.tunnel.concentrators[0]


# --- check-in ----------------------------------------------------------------------

_FP = re.compile(r"^[0-9a-f]{64}$")
SSTP_FAILOVER_AFTER = 5  # počet check-inů po sobě s neběžícím tunelem


def lookup_device_by_key(db: Database, key: str) -> dict | None:
    if not key or len(key) > 100:
        return None
    with operator_scope(db) as tx:
        return tx.fetchone(
            "SELECT id, tenant_id FROM device WHERE checkin_key_hash = %(h)s AND state <> 'removed'",
            {"h": token_hash(key)},
        )


def _parse_status(body: dict) -> dict:
    def s(k: str, n: int) -> str:
        v = str(body.get(k, ""))[:n]
        return v if re.fullmatch(r"[\x20-\x7e]*", v) else ""

    cpu = body.get("cpu")
    fp = s("fp", 100).replace(":", "").lower()
    return {
        "version": s("version", 50),
        "uptime": s("uptime", 40),
        "cpu": cpu if isinstance(cpu, int) and 0 <= cpu <= 100 else None,
        "sstp": body.get("sstp") if body.get("sstp") in ("up", "down", "none") else "unknown",
        "fp": fp if _FP.match(fp) else "",
        # 1 = na zařízení běží bootstrap převzetí (čeká na potvrzení)
        "tk": body.get("tk") == 1,
        # verze skriptu check-inu (starší ji neposílají = 0)
        "sv": body.get("sv") if isinstance(body.get("sv"), int) and 0 <= body.get("sv") < 1000 else 0,
    }


def checkin(db: Database, settings: Settings, box: SecretBox, device_ref: dict, body: dict,
            ip: str | None = None) -> str:
    """Zpracuje check-in a vrátí RouterOS skript k provedení (nebo prázdný)."""
    st = _parse_status(body)
    with tenant_scope(db, device_ref["tenant_id"]) as tx:
        dev = tx.fetchone(
            """SELECT id, serial, state, api_cert_fingerprint, concentrator, last_status, host(tunnel_ip) AS tunnel_ip
               FROM device WHERE id = %(d)s FOR UPDATE""",
            {"d": str(device_ref["id"])},
        )
        if not dev or dev["state"] == "removed":
            return routeros.NOTHING
        prev = dev["last_status"] or {}
        down = prev.get("sstp_down", 0) + 1 if st["sstp"] != "up" and dev["state"] in ("adopted", "offline", "provisioning") else 0
        if settings.tunnel.local:
            down = 0  # bez tunelu; správa jde na adresu z check-inu
            set_local_address(tx, device_ref["tenant_id"], dev, ip)
        st["sstp_down"] = down
        st["ip"] = ip
        new_state = "adopted" if dev["state"] == "offline" else dev["state"]
        fp_update = None
        from . import takeover

        in_takeover = st["tk"] and takeover.takeover_active(tx, dev["id"])
        if st["fp"]:
            if dev["state"] == "provisioning" or (in_takeover and st["fp"] != dev["api_cert_fingerprint"]):
                # po resetu (převzetí) má zařízení nový certifikát REST – nahlásilo ho autentizovaně
                fp_update = st["fp"]
                if dev["state"] != "provisioning":
                    audit(tx, "device.cert_renewed", tenant_id=device_ref["tenant_id"], target=f"device:{dev['id']}",
                          before={"fp": dev["api_cert_fingerprint"]}, after={"fp": st["fp"], "reason": "takeover"}, ip=ip)
            elif dev["api_cert_fingerprint"] and st["fp"] != dev["api_cert_fingerprint"]:
                audit(tx, "device.cert_changed", tenant_id=device_ref["tenant_id"],
                      target=f"device:{dev['id']}", before={"fp": dev["api_cert_fingerprint"]},
                      after={"fp": st["fp"]}, ip=ip)
        tx.execute(
            """UPDATE device SET last_checkin_at = now(), last_status = %(st)s, state = %(ns)s,
                   ros_version = coalesce(nullif(%(v)s, ''), ros_version),
                   api_cert_fingerprint = coalesce(%(fp)s, api_cert_fingerprint)
               WHERE id = %(d)s""",
            {"st": st, "ns": new_state, "v": st["version"], "fp": fp_update, "d": str(dev["id"])},
        )
        if new_state != dev["state"]:
            audit(tx, "device.online", tenant_id=device_ref["tenant_id"], target=f"device:{dev['id']}", ip=ip)

        # automatické přepnutí koncentrátoru, když tunel dlouho neběží
        if not settings.tunnel.local and down >= SSTP_FAILOVER_AFTER and len(settings.tunnel.concentrators) > 1 and dev["state"] != "provisioning":
            names = [c.name for c in settings.tunnel.concentrators]
            cur = dev["concentrator"] if dev["concentrator"] in names else names[0]
            nxt = names[(names.index(cur) + 1) % len(names)]
            pending = tx.fetchone(
                """SELECT 1 FROM device_command WHERE device_id = %(d)s AND kind = 'switch_concentrator'
                   AND status IN ('queued', 'sent')""", {"d": str(dev["id"])})
            if not pending:
                enqueue(tx, device_ref["tenant_id"], dev["id"], "switch_concentrator", {"concentrator": nxt})
                tx.execute("UPDATE device SET concentrator = %(c)s, last_status = last_status || '{\"sstp_down\": 0}'::jsonb WHERE id = %(d)s",
                           {"c": nxt, "d": str(dev["id"])})
                audit(tx, "device.concentrator_failover", tenant_id=device_ref["tenant_id"],
                      target=f"device:{dev['id']}", before={"concentrator": cur}, after={"concentrator": nxt})

        # starší skript check-inu (např. bez samoopravy CA controlleru) → jednou přeinstalovat
        if st["sv"] < routeros.CHECKIN_REV and new_state == "adopted" and not tx.fetchone(
                """SELECT 1 FROM device_command WHERE device_id = %(d)s AND kind = 'checkin_update'
                   AND (status IN ('queued', 'sent') OR created_at > now() - interval '1 day')""", {"d": str(dev["id"])}):
            enqueue(tx, device_ref["tenant_id"], dev["id"], "checkin_update", {"rev": routeros.CHECKIN_REV})
            audit(tx, "device.checkin_update", tenant_id=device_ref["tenant_id"], target=f"device:{dev['id']}",
                  before={"sv": st["sv"]}, after={"sv": routeros.CHECKIN_REV}, ip=ip)

        # zařízení se po aplikaci změny ozvalo → cesta ke správě funguje → potvrdit (zrušit návrat)
        from .plans import pending_confirm

        pc = pending_confirm(tx, dev["id"])
        # převzetí: potvrdit až když běží bootstrap (tk) i tunel – jinak by se potvrdilo
        # ozvání ze staré konfigurace nebo zařízení bez správy přes tunel
        mgmt_up = st["sstp"] == "up" or settings.tunnel.local
        if pc and pc["module"] == "takeover" and not (st["tk"] and mgmt_up):
            if pc["overdue"] and not st["tk"]:
                # po termínu se zařízení ozvalo bez běžícího bootstrapu = vrátilo se ze zálohy
                takeover.finished(tx, device_ref["tenant_id"], pc["id"], False)
            pc = None
        if pc:
            cid = enqueue(tx, device_ref["tenant_id"], dev["id"], "confirm_plan", {"plan_id": pc["id"]})
            tx.execute("UPDATE device_command SET plan_id = %(p)s WHERE id = %(c)s", {"p": pc["id"], "c": cid})
        cmd = tx.fetchone(
            """SELECT id, kind, params FROM device_command
               WHERE device_id = %(d)s AND status = 'queued'
               ORDER BY (kind = 'confirm_plan') DESC, id LIMIT 1
               FOR UPDATE SKIP LOCKED""",
            {"d": str(dev["id"])},
        )
        if not cmd:
            return routeros.NOTHING
        dev_full = dict(dev, concentrator=tx.fetchone(
            "SELECT concentrator FROM device WHERE id = %(d)s", {"d": str(dev["id"])})["concentrator"])
        script = render_command(tx, settings, box, cmd, dev_full)
        tx.execute(
            """UPDATE device_command SET status = 'sent', sent_at = now(), attempts = attempts + 1
               WHERE id = %(id)s""",
            {"id": cmd["id"]},
        )
        return script


def ack(db: Database, device_ref: dict, command_id: int, status: str, ip: str | None = None) -> bool:
    if status not in ("done", "failed", "reverted"):
        raise ServiceError("neplatný stav")
    tenant_id = device_ref["tenant_id"]
    if status == "reverted":
        # zařízení samo vrátilo nepotvrzenou změnu (commit-confirm)
        from .plans import rolled_back

        with tenant_scope(db, tenant_id) as tx:
            ok = tx.fetchone("""SELECT 1 FROM device_command WHERE id = %(id)s AND device_id = %(d)s
                                AND kind = 'apply_plan'""", {"id": command_id, "d": str(device_ref["id"])})
            row = rolled_back(tx, command_id) if ok else None
            if row:
                from . import takeover

                takeover.rolled_back(tx, tenant_id, row["id"])
                audit(tx, "plan.rolled_back", tenant_id=tenant_id, target=f"device:{device_ref['id']}",
                      after={"plan_id": row["id"], "summary": row["summary"]}, ip=ip)
            return bool(row)
    with tenant_scope(db, tenant_id) as tx:
        row = tx.fetchone(
            """UPDATE device_command SET status = %(st)s, finished_at = now()
               WHERE id = %(id)s AND device_id = %(d)s AND status = 'sent'
               RETURNING kind""",
            {"st": status, "id": command_id, "d": str(device_ref["id"])},
        )
        if not row:
            return False
        audit(tx, f"device.command.{status}", tenant_id=tenant_id, target=f"device:{device_ref['id']}",
              after={"command_id": command_id, "kind": row["kind"]}, ip=ip)
        if row["kind"] == "confirm_plan":
            from .plans import confirm_done

            pid = tx.fetchone("SELECT plan_id FROM device_command WHERE id = %(c)s", {"c": command_id})["plan_id"]
            confirm_done(tx, pid, status == "done", tenant_id)
            audit(tx, "plan.confirmed" if status == "done" else "plan.confirm_failed", tenant_id=tenant_id,
                  target=f"device:{device_ref['id']}", after={"plan_id": pid}, ip=ip)
        if row["kind"] == "apply_plan":
            from .plans import finish_plan

            finish_plan(tx, command_id, status == "done")
            # konfigurace se změnila – inventuru načíst co nejdřív znovu
            tx.execute("UPDATE device SET inventory_at = NULL WHERE id = %(d)s", {"d": str(device_ref["id"])})
        if row["kind"] == "set_identity" and status == "done":
            _identity_done(tx, tenant_id, device_ref["id"], command_id)
        if row["kind"] == "deprovision" and status == "done":
            tx.execute(
                """UPDATE device SET state = 'removed', checkin_key_hash = NULL, tunnel_ip = NULL,
                       api_cert_fingerprint = NULL WHERE id = %(d)s""",
                {"d": str(device_ref["id"])},
            )
            tx.execute("DELETE FROM device_secret WHERE device_id = %(d)s AND kind <> 'checkin_key'",
                       {"d": str(device_ref["id"])})
            audit(tx, "device.removed", tenant_id=tenant_id, target=f"device:{device_ref['id']}", ip=ip)
        return True


def parse_json_body(raw: bytes, limit: int = 16384) -> dict:
    if len(raw) > limit:
        raise ServiceError("tělo požadavku je příliš velké")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError) as e:
        raise ServiceError("neplatný JSON") from e
    if not isinstance(data, dict):
        raise ServiceError("očekáván JSON objekt")
    return data


def valid_ip(v: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address(v)) if v else None
    except ValueError:
        return None


# --- okamžité doručení (kick) ---------------------------------------------------------------------

KICK_ENABLED = True       # testy vypínají (zařízení neexistují)
KICK_FILE = "mtc-kick.rsc"
# provision mění heslo vlastního účtu mtc-ctl (chce politiku password) – ten jen schedulerem
_NO_KICK = {"provision"}


def kick_needed(tx: Tx, device_id: Any) -> bool:
    """Čeká na zařízení něco, co si vyzvedne check-inem? (příkaz ve frontě nebo potvrzení změny)"""
    nxt = tx.fetchone(
        """SELECT kind FROM device_command WHERE device_id = %(d)s AND status = 'queued'
           ORDER BY (kind = 'confirm_plan') DESC, id LIMIT 1""", {"d": str(device_id)})
    if nxt:
        return nxt["kind"] not in _NO_KICK
    return tx.fetchone(
        """SELECT 1 FROM config_plan p WHERE p.device_id = %(d)s AND p.status = 'confirming' AND p.module <> 'takeover'
             AND NOT EXISTS (SELECT 1 FROM device_command c WHERE c.plan_id = p.id AND c.kind = 'confirm_plan'
                             AND c.status IN ('queued', 'sent'))""", {"d": str(device_id)}) is not None


def kick(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, device_id: Any, delay: float = 0.0) -> None:
    """Okamžitý check-in přes REST v tunelu: zařízení si příkaz vyzvedne hned a nečeká
    na scheduler (1 min). Spustí kód check-inu přes /execute (na pozadí zařízení)
    s vlastním souborem příkazu. Best-effort: při chybě doručí scheduler jako dřív."""
    if not KICK_ENABLED:
        return
    import threading
    import time

    def run() -> None:
        api = None
        try:
            if delay:
                time.sleep(delay)
            with tenant_scope(db, tenant_id) as tx:
                d = tx.fetchone(
                    """SELECT id, state, host(tunnel_ip) AS tunnel_ip, api_cert_fingerprint AS fp FROM device
                       WHERE id = %(d)s""", {"d": str(device_id)})
                if not d or d["state"] != "adopted" or not d["tunnel_ip"] or not d["fp"] or not kick_needed(tx, device_id):
                    return
                key = get_secret(tx, box, device_id, "checkin_key") or ""
                api = device_api(tx, box, d, timeout=5.0)
            opts = routeros.fetch_opts(settings)
            src = routeros.checkin_source(f"https://{settings.web.checkin_host}/api/checkin", key, opts, cmd_file=KICK_FILE)
            api.request("POST", "/execute", {"script": src})
            log.info("kick %s: check-in vyvolán", device_id)
        except Exception as e:  # zařízení nedostupné – doručí scheduler
            log.info("kick %s se nepovedl: %s", device_id, e)
        finally:
            close = getattr(api, "close", None)
            if close:
                close()

    threading.Thread(target=run, name=f"kick-{device_id}", daemon=True).start()


def kick_for_command(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, command_id: int) -> None:
    if not KICK_ENABLED:
        return
    with tenant_scope(db, tenant_id) as tx:
        r = tx.fetchone("SELECT device_id FROM device_command WHERE id = %(c)s", {"c": command_id})
    if r:
        kick(db, settings, box, tenant_id, r["device_id"])


def device_api(tx: Tx, box: SecretBox, dev: dict, timeout: float = 10.0):
    """REST klient na zařízení přes tunel: servisní účet + pinning certifikátu,
    který zařízení nahlásilo během provisioningu."""
    from .rosapi import RosApi

    if not dev.get("tunnel_ip") or not dev.get("fp"):
        raise ServiceError("zařízení nemá tunelovou IP nebo otisk certifikátu")
    pw = get_secret(tx, box, dev["id"], "api_password") or ""
    return RosApi(f"https://{dev['tunnel_ip']}", routeros.API_USER, pw, dev["fp"], timeout=timeout)
