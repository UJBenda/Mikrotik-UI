"""Zálohy konfigurace přes REST: `/export show-sensitive file=…`, přečtení
souboru po částech (`/file/read`, max 32 KiB na dotaz; záložně vlastnost
`contents` u souborů do ~60 KiB), smazání souboru na zařízení.

Export obsahuje hesla, proto se ukládá jen šifrovaně (SecretBox, kontext
vázaný na zařízení) a dešifruje se jen pro stažení/diff s auditem.
Experimentální: chování `/file/read` přes REST je potřeba ověřit na zařízení.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any

from .db import Database, Tx, tenant_scope
from .rosapi import RosApi, RosApiError
from .security import SealedSecret, SecretBox
from .services import audit

log = logging.getLogger("mtc.backup")

FILE = "mtc-backup"
CHUNK = 32768
MAX_SIZE = 8 * 1024 * 1024


class BackupError(Exception):
    pass


def _find_file(api: RosApi) -> dict | None:
    for f in api.get("/file") or []:
        name = str(f.get("name", ""))
        if name == f"{FILE}.rsc" or name.endswith(f"/{FILE}.rsc"):
            return f
    return None


def fetch_export(api: RosApi, wait_s: float = 20.0) -> str:
    old = _find_file(api)
    if old:
        api.delete(f"/file/{old['.id']}")
    try:
        api.post("/export", {"file": FILE, "show-sensitive": ""})
    except RosApiError as e:
        if e.status and e.status >= 400:
            # starší verze bez show-sensitive (citlivé údaje pak v exportu nejsou)
            api.post("/export", {"file": FILE})
        else:
            raise
    deadline = time.monotonic() + wait_s
    f, last_size = None, -1
    while time.monotonic() < deadline:
        f = _find_file(api)
        if f:
            size = int(str(f.get("size", "0")).split()[0] or 0)
            if size > 0 and size == last_size:
                break
            last_size = size
        time.sleep(1.0)
    if not f:
        raise BackupError("export soubor se na zařízení neobjevil")
    size = int(str(f.get("size", "0")).split()[0] or 0)
    if size > MAX_SIZE:
        raise BackupError(f"export je příliš velký ({size} B)")
    try:
        text = _read_chunks(api, str(f["name"]), size)
    except RosApiError:
        text = None
    if text is None or len(text.encode()) < size:
        contents = f.get("contents")
        if contents is None:
            raise BackupError("soubor nejde přečíst (/file/read ani contents)")
        text = contents
    try:
        api.delete(f"/file/{f['.id']}")
    except RosApiError:
        log.warning("nepodařilo se smazat %s na zařízení", f.get("name"))
    if not text.strip():
        raise BackupError("export je prázdný")
    return text


def _read_chunks(api: RosApi, name: str, size: int) -> str | None:
    parts: list[str] = []
    offset = 0
    while offset < max(size, 1):
        r = api.post("/file/read", {"file": name, "offset": str(offset), "chunk-size": str(CHUNK),
                                    "as-value": ""})
        if isinstance(r, list):
            r = r[0] if r else {}
        data = (r or {}).get("data")
        if data is None:
            return None
        if not data:
            break
        parts.append(data)
        offset += len(data.encode())
        if len(data.encode()) < CHUNK:
            break
    return "".join(parts)


def normalize(text: str) -> str:
    """Bez hlavičky s datem exportu – kvůli detekci skutečných změn."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[0].startswith("# ") and " by RouterOS " in lines[0]:
        lines = lines[1:]
    return "\n".join(lines).strip() + "\n"


def _ctx(device_id: Any) -> bytes:
    return f"backup:{device_id}".encode()


def store(db: Database, box: SecretBox, tenant_id: Any, device_id: Any, text: str,
          reason: str = "scheduled") -> dict:
    raw = text.encode()
    content_sha = hashlib.sha256(normalize(text).encode()).hexdigest()
    sealed = box.seal(raw, _ctx(device_id))
    with tenant_scope(db, tenant_id) as tx:
        prev = tx.fetchone(
            "SELECT content_sha FROM device_backup WHERE device_id = %(d)s ORDER BY id DESC LIMIT 1",
            {"d": str(device_id)})
        changed = prev is None or prev["content_sha"] != content_sha
        row = tx.fetchone(
            """INSERT INTO device_backup (tenant_id, device_id, reason, size_bytes, sha256, content_sha,
                   changed, wrapped_key, ciphertext, key_version)
               VALUES (%(t)s, %(d)s, %(r)s, %(sz)s, %(sha)s, %(csha)s, %(ch)s, %(w)s, %(c)s, %(kv)s)
               RETURNING id, taken_at, changed""",
            {"t": str(tenant_id), "d": str(device_id), "r": reason, "sz": len(raw),
             "sha": hashlib.sha256(raw).hexdigest(), "csha": content_sha, "ch": changed,
             "w": sealed.wrapped_key, "c": sealed.ciphertext, "kv": sealed.key_version})
        tx.execute("UPDATE device SET backup_at = now() WHERE id = %(d)s", {"d": str(device_id)})
        if changed and prev is not None:
            # změna konfigurace od poslední zálohy – pokud ji neprovedl controller, je to drift
            recent_plan = tx.fetchone(
                """SELECT 1 FROM config_plan WHERE device_id = %(d)s AND status = 'applied'
                   AND finished_at > now() - interval '1 day'""", {"d": str(device_id)})
            audit(tx, "device.config_changed", tenant_id=tenant_id, target=f"device:{device_id}",
                  after={"backup_id": row["id"], "by_controller": bool(recent_plan)})
    return row


def load_text(tx: Tx, box: SecretBox, device_id: Any, backup_id: int) -> str:
    row = tx.fetchone(
        "SELECT wrapped_key, ciphertext, key_version FROM device_backup WHERE id = %(i)s AND device_id = %(d)s",
        {"i": backup_id, "d": str(device_id)})
    if not row:
        raise BackupError("záloha neexistuje")
    return box.open(SealedSecret(bytes(row["wrapped_key"]), bytes(row["ciphertext"]), row["key_version"]),
                    _ctx(device_id)).decode()


_SECRET_RE = re.compile(r'((?:password|passphrase|secret|private-key|psk|authentication-key|key)=)("[^"]*"|\S+)')


def redact(text: str, key: bytes) -> str:
    """Pro diff v UI: hodnotu hesla nahradí značkou z HMAC s jednorázovým
    klíčem – změna hesla je v diffu vidět, hodnota z ní zjistit nejde."""
    def sub(m: re.Match) -> str:
        tag = hmac.new(key, m.group(2).encode(), hashlib.sha256).hexdigest()[:8]
        return f"{m.group(1)}***{tag}"

    return _SECRET_RE.sub(sub, text)


def diff(old: str, new: str, context: int = 3) -> str:
    key = os.urandom(16)
    return "".join(difflib.unified_diff(
        redact(normalize(old), key).splitlines(keepends=True),
        redact(normalize(new), key).splitlines(keepends=True),
        fromfile="předchozí", tofile="aktuální", n=context))
