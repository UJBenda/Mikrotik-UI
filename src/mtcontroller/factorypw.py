"""Výchozí hesla ze štítků zařízení (admin heslo z výroby), podle sériového čísla.

Zadávají se ručně nebo importem CSV ještě před adopcí; v DB jen šifrovaně
(SecretBox, kontext vázaný na tenanta a sériové číslo). Zobrazení hesla se
audituje. Heslo se do zařízení nikam neposílá – je to evidence pro technika.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from .db import Database, Tx, tenant_scope
from .security import SealedSecret, SecretBox
from .services import NotFound, ServiceError, _s, audit

SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
PW_RE = re.compile(r"^[\x20-\x7e]{1,64}$")
MAX_IMPORT = 2000


def _ctx(tenant_id: Any, serial: str) -> bytes:
    return f"factory:{tenant_id}:{serial}".encode()


def _check(serial: str, password: str, note: str | None) -> tuple[str, str, str | None]:
    serial = str(serial or "").strip().upper()
    password = str(password or "").strip()
    note = (str(note).strip()[:200] or None) if note else None
    if not SERIAL_RE.match(serial):
        raise ServiceError(f"neplatné sériové číslo {serial!r}")
    if not PW_RE.match(password):
        raise ServiceError(f"{serial}: heslo 1–64 znaků bez diakritiky")
    if note and any(ord(c) < 32 for c in note):
        raise ServiceError(f"{serial}: poznámka bez řídicích znaků")
    return serial, password, note


def _upsert(tx: Tx, box: SecretBox, tenant_id: Any, serial: str, password: str, note: str | None, user_id: Any) -> None:
    sealed = box.seal(password.encode(), _ctx(tenant_id, serial))
    tx.execute(
        """INSERT INTO factory_password (tenant_id, serial, wrapped_key, ciphertext, key_version, note, updated_by)
           VALUES (%(t)s, %(s)s, %(w)s, %(c)s, %(kv)s, %(n)s, %(u)s)
           ON CONFLICT (tenant_id, serial) DO UPDATE SET wrapped_key = EXCLUDED.wrapped_key,
               ciphertext = EXCLUDED.ciphertext, key_version = EXCLUDED.key_version,
               note = coalesce(EXCLUDED.note, factory_password.note), updated_by = EXCLUDED.updated_by,
               updated_at = now()""",
        {"t": str(tenant_id), "s": serial, "w": sealed.wrapped_key, "c": sealed.ciphertext,
         "kv": sealed.key_version, "n": note, "u": _s(user_id)})


def listing(db: Database, tenant_id: Any) -> list[dict]:
    with tenant_scope(db, tenant_id) as tx:
        return tx.fetchall(
            """SELECT f.serial, f.note, f.updated_at, d.id AS device_id, d.identity, d.model, d.state::text AS state
               FROM factory_password f LEFT JOIN device d ON d.serial = f.serial AND d.state <> 'removed'
               ORDER BY f.serial""")


def save(db: Database, box: SecretBox, tenant_id: Any, serial: str, password: str, note: str | None = None,
         user_id: Any = None, ip: str | None = None) -> dict:
    serial, password, note = _check(serial, password, note)
    with tenant_scope(db, tenant_id) as tx:
        _upsert(tx, box, tenant_id, serial, password, note, user_id)
        audit(tx, "factory_password.save", tenant_id=tenant_id, user_id=user_id, target=f"serial:{serial}",
              after={"note": note}, ip=ip)
    return {"serial": serial, "note": note}


def import_csv(db: Database, box: SecretBox, tenant_id: Any, text: str, user_id: Any = None,
               ip: str | None = None) -> dict:
    """Řádky `sériové číslo;heslo;poznámka` (oddělovač ; , nebo tab, hlavička se přeskočí)."""
    if len(text) > 200_000:
        raise ServiceError("import je příliš velký")
    sample = text[:2000]
    delim = ";" if sample.count(";") >= sample.count(",") and ";" in sample else "\t" if "\t" in sample else ","
    rows, errors = [], []
    for n, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim), 1):
        if not row or not "".join(row).strip():
            continue
        if n == 1 and re.match(r"(?i)^\s*(serial|s[eé]riov|sn\b)", row[0]):
            continue
        if len(row) < 2:
            errors.append(f"řádek {n}: čekám sériové číslo{delim}heslo")
            continue
        try:
            rows.append(_check(row[0], row[1], row[2] if len(row) > 2 else None))
        except ServiceError as e:
            errors.append(f"řádek {n}: {e}")
    if len(rows) > MAX_IMPORT:
        raise ServiceError(f"nejvýš {MAX_IMPORT} řádků najednou")
    with tenant_scope(db, tenant_id) as tx:
        for serial, password, note in rows:
            _upsert(tx, box, tenant_id, serial, password, note, user_id)
        audit(tx, "factory_password.import", tenant_id=tenant_id, user_id=user_id, target=f"tenant:{tenant_id}",
              after={"imported": len(rows), "errors": len(errors)}, ip=ip)
    return {"imported": len(rows), "errors": errors[:50]}


def reveal(db: Database, box: SecretBox, tenant_id: Any, serial: str, user_id: Any = None, ip: str | None = None) -> str:
    serial = str(serial or "").strip().upper()
    with tenant_scope(db, tenant_id) as tx:
        r = tx.fetchone("SELECT wrapped_key, ciphertext, key_version FROM factory_password WHERE serial = %(s)s",
                        {"s": serial})
        if not r:
            raise NotFound("heslo pro toto sériové číslo není uložené")
        pw = box.open(SealedSecret(bytes(r["wrapped_key"]), bytes(r["ciphertext"]), r["key_version"]),
                      _ctx(tenant_id, serial)).decode()
        # zobrazení hesla je citlivá akce – zapsat (bez hesla)
        audit(tx, "factory_password.reveal", tenant_id=tenant_id, user_id=user_id, target=f"serial:{serial}", ip=ip)
    return pw


def delete(db: Database, tenant_id: Any, serial: str, user_id: Any = None, ip: str | None = None) -> None:
    serial = str(serial or "").strip().upper()
    with tenant_scope(db, tenant_id) as tx:
        if not tx.execute("DELETE FROM factory_password WHERE serial = %(s)s", {"s": serial}):
            raise NotFound("heslo pro toto sériové číslo není uložené")
        audit(tx, "factory_password.delete", tenant_id=tenant_id, user_id=user_id, target=f"serial:{serial}", ip=ip)
