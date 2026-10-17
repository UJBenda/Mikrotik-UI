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
    opts = routeros.FetchOpts(settings.security.device_check_certificate)
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


def register_device(db: Database, box: SecretBox, token: str, payload: dict,
                    ip: str | None = None) -> Registration:
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

    key = new_token()
    with operator_scope(db) as tx:
        tok = tx.fetchone(
            """SELECT id, tenant_id, site_id FROM adoption_token
               WHERE token_hash = %(h)s AND used_at IS NULL AND expires_at > now()
               FOR UPDATE""",
            {"h": token_hash(token)},
        )
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
        tok = tx.fetchone(
            """SELECT id, tenant_id, site_id FROM adoption_token
               WHERE token_hash = %(h)s AND used_at IS NULL AND expires_at > now()
               FOR UPDATE""",
            {"h": token_hash(token)},
        )
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
                       api_cert_fingerprint = NULL, tunnel_ip = NULL, last_checkin_at = now()
                   WHERE id = %(id)s""",
                {"m": model, "v": version, "mac": mac or None, "site": _s(tok["site_id"]),
                 "k": token_hash(key), "id": str(device_id)},
            )
        else:
            device_id = tx.fetchone(
                """INSERT INTO device (tenant_id, site_id, serial, model, ros_version, mac,
                                       state, checkin_key_hash, last_checkin_at)
                   VALUES (%(t)s, %(site)s, %(s)s, %(m)s, %(v)s, %(mac)s, 'pending', %(k)s, now())
                   RETURNING id""",
                {"t": str(tenant_id), "site": _s(tok["site_id"]), "s": serial, "m": model,
                 "v": version, "mac": mac or None, "k": token_hash(key)},
            )["id"]
        put_secret(tx, box, tenant_id, device_id, "checkin_key", key)
        tx.execute(
            "UPDATE adoption_token SET used_at = now(), used_by_device = %(d)s WHERE id = %(id)s",
            {"d": str(device_id), "id": str(tok["id"])},
        )
        audit(tx, "device.register", tenant_id=tenant_id, target=f"device:{device_id}",
              after={"serial": serial, "model": model, "version": version}, ip=ip)
    return Registration(device_id, tenant_id, key)


# --- schválení -------------------------------------------------------------------


def sstp_username(serial: str) -> str:
    return f"mtc-{serial}"


def approve_device(db: Database, settings: Settings, box: SecretBox, providers: list[TunnelProvider],
                   tenant_id: Any, device_id: Any, site_id: Any, role: str = "unknown",
                   user_id: Any = None, ip: str | None = None) -> list[str]:
    """Přidělí tunelovou IP, vygeneruje hesla, naplánuje provisioning a založí
    účty na koncentrátorech. Vrací seznam chyb koncentrátorů (worker je později
    dorovná), schválení samotné tím neselže."""
    if not settings.tunnel.concentrators:
        raise ServiceError("v konfiguraci není žádný koncentrátor (tunnel.concentrators)")
    if role not in DEVICE_ROLES:
        raise ServiceError("neplatná role zařízení")
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone(
            "SELECT id, serial, state, host(tunnel_ip) AS tunnel_ip FROM device WHERE id = %(d)s FOR UPDATE",
            {"d": str(device_id)},
        )
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] != "pending":
            raise ServiceError(f"zařízení není ve stavu pending (je {dev['state']})")
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        block = tx.fetchone("SELECT tunnel_block::text AS b FROM tenant WHERE id = %(t)s",
                            {"t": str(tenant_id)})["b"]
        used = [r["ip"] for r in tx.fetchall(
            "SELECT host(tunnel_ip) AS ip FROM device WHERE tunnel_ip IS NOT NULL")]
        tunnel_ip = dev["tunnel_ip"] or str(ipam.next_device_address([block], used))
        sstp_pw, api_pw = random_password(), random_password()
        put_secret(tx, box, tenant_id, device_id, "sstp_password", sstp_pw)
        put_secret(tx, box, tenant_id, device_id, "api_password", api_pw)
        concentrator = settings.tunnel.concentrators[0].name
        tx.execute(
            """UPDATE device SET state = 'provisioning', site_id = %(s)s, role = %(r)s,
                   tunnel_ip = %(ip)s, approved_by = %(u)s, approved_at = now(), concentrator = %(c)s
               WHERE id = %(d)s""",
            {"s": str(site_id), "r": role, "ip": tunnel_ip, "u": _s(user_id), "c": concentrator,
             "d": str(device_id)},
        )
        enqueue(tx, tenant_id, device_id, "provision", {}, user_id)
        audit(tx, "device.approve", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"site_id": str(site_id), "role": role, "tunnel_ip": tunnel_ip}, ip=ip)
        serial = dev["serial"]
        comment = f"{tenant_id}/{site_id}"
    account = TunnelAccount(sstp_username(serial), sstp_pw, tunnel_ip, comment)
    errors = []
    for p in providers:
        try:
            p.ensure_account(account)
        except Exception as e:  # koncentrátor nedostupný – dorovná worker
            log.warning("koncentrátor %s: ensure_account selhal: %s", p.name, e)
            errors.append(f"{p.name}: {e}")
    return errors


DEVICE_ROLES = {"unknown", "gateway", "switch", "ap", "ptp"}


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


# --- příkazy -----------------------------------------------------------------------

COMMAND_KINDS = {"provision", "switch_concentrator", "reboot", "deprovision", "apply_plan", "upgrade", "firmware",
                 "confirm_plan", "poe_cycle"}
USER_COMMANDS = {"reboot", "deprovision", "provision", "upgrade", "firmware", "poe_cycle"}


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
        if kind == "poe_cycle":
            from . import inventory, portcfg

            port = str((params or {}).get("port", ""))
            try:
                portcfg.poe_cycle_body(inventory.load(tx, device_id, ["ethernet"]), port)
            except (portcfg.PortError, ValueError) as e:
                raise ServiceError(str(e)) from e
            p = {"port": port}
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
    opts = routeros.FetchOpts(settings.security.device_check_certificate)
    key = get_secret(tx, box, dev["id"], "checkin_key") or ""
    ack_url = f"https://{settings.web.checkin_host}/api/checkin/ack?id={cmd['id']}"
    kind = cmd["kind"]
    if kind == "provision":
        conc = _concentrator(settings, dev.get("concentrator"))
        body = routeros.provision_body(routeros.ProvisionParams(
            mgmt_source=str(ipam.concentrator_address(settings.tunnel.pool)),
            connect_host=conc.connect_host,
            sstp_user=sstp_username(dev["serial"]),
            sstp_password=get_secret(tx, box, dev["id"], "sstp_password") or "",
            api_password=get_secret(tx, box, dev["id"], "api_password") or "",
        ), opts)
        return routeros.wrap_command(ack_url, key, body, opts)
    if kind == "switch_concentrator":
        conc = _concentrator(settings, cmd["params"].get("concentrator"))
        return routeros.wrap_command(ack_url, key, routeros.switch_concentrator_body(conc.connect_host), opts)
    if kind == "reboot":
        return routeros.wrap_command(ack_url, key, "", opts, tail=routeros.REBOOT_TAIL)
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
        body = pl["script"]
        if pl["revert_script"]:
            body = routeros.safe_apply_body(body, pl["revert_script"], f"{ack_url}&status=reverted", key, opts,
                                            int(pl["confirm_minutes"] or 5))
        return routeros.wrap_command(ack_url, key, body, opts)
    if kind == "confirm_plan":
        return routeros.wrap_command(ack_url, key, routeros.CONFIRM_BODY, opts)
    if kind == "poe_cycle":
        from . import inventory, portcfg

        return routeros.wrap_command(ack_url, key, portcfg.poe_cycle_body(
            inventory.load(tx, dev["id"], ["ethernet"]), str(cmd["params"].get("port", ""))), opts)
    raise ServiceError(f"neznámý příkaz {kind}")


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
    }


def checkin(db: Database, settings: Settings, box: SecretBox, device_ref: dict, body: dict,
            ip: str | None = None) -> str:
    """Zpracuje check-in a vrátí RouterOS skript k provedení (nebo prázdný)."""
    st = _parse_status(body)
    with tenant_scope(db, device_ref["tenant_id"]) as tx:
        dev = tx.fetchone(
            """SELECT id, serial, state, api_cert_fingerprint, concentrator, last_status
               FROM device WHERE id = %(d)s FOR UPDATE""",
            {"d": str(device_ref["id"])},
        )
        if not dev or dev["state"] == "removed":
            return routeros.NOTHING
        prev = dev["last_status"] or {}
        down = prev.get("sstp_down", 0) + 1 if st["sstp"] != "up" and dev["state"] in ("adopted", "offline", "provisioning") else 0
        st["sstp_down"] = down
        st["ip"] = ip
        new_state = "adopted" if dev["state"] == "offline" else dev["state"]
        fp_update = None
        if st["fp"]:
            if dev["state"] == "provisioning":
                fp_update = st["fp"]
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
        if down >= SSTP_FAILOVER_AFTER and len(settings.tunnel.concentrators) > 1 and dev["state"] != "provisioning":
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

        # zařízení se po aplikaci změny ozvalo → cesta ke správě funguje → potvrdit (zrušit návrat)
        from .plans import pending_confirm

        pc = pending_confirm(tx, dev["id"])
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
            confirm_done(tx, pid, status == "done")
            audit(tx, "plan.confirmed", tenant_id=tenant_id, target=f"device:{device_ref['id']}",
                  after={"plan_id": pid}, ip=ip)
        if row["kind"] == "apply_plan":
            from .plans import finish_plan

            finish_plan(tx, command_id, status == "done")
            # konfigurace se změnila – inventuru načíst co nejdřív znovu
            tx.execute("UPDATE device SET inventory_at = NULL WHERE id = %(d)s", {"d": str(device_ref["id"])})
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


def device_api(tx: Tx, box: SecretBox, dev: dict, timeout: float = 10.0):
    """REST klient na zařízení přes tunel: servisní účet + pinning certifikátu,
    který zařízení nahlásilo během provisioningu."""
    from .rosapi import RosApi

    if not dev.get("tunnel_ip") or not dev.get("fp"):
        raise ServiceError("zařízení nemá tunelovou IP nebo otisk certifikátu")
    pw = get_secret(tx, box, dev["id"], "api_password") or ""
    return RosApi(f"https://{dev['tunnel_ip']}", routeros.API_USER, pw, dev["fp"], timeout=timeout)
