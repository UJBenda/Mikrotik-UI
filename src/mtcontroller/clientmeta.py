"""Klienti v controlleru: popisek, ikona, poznámka a loga (jen v controlleru, na zařízení se nepíší).

Logo klienta: vlastní obrázek klienta → logo jeho výrobce (nahrané uživatelem, platí pro všechny
klienty výrobce) → ikona zvolená z nabídky → v UI odznak s iniciálami výrobce.
Obrázky jen PNG/JPEG/WebP do 64 KiB s kontrolou hlavičky souboru; SVG ne (může nést skript).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any

from . import leases, oui
from .db import Database, Tx, tenant_scope
from .services import NotFound, ServiceError, _s, audit

# ikony z nabídky (UI je kreslí sama); klíč je jen [a-z]
ICONS = ("phone", "tablet", "laptop", "desktop", "printer", "camera", "tv", "nas", "server", "iot", "voip",
         "speaker", "game", "watch", "car", "ap", "router", "switch", "light", "sensor", "lock", "other")
LOGO_MAX = 65536
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


def _ctrl_free(s: str) -> bool:
    return not any(ord(ch) < 32 and ch not in "\n\t" for ch in s)


def vendor_key(name: Any) -> str:
    """Klíč výrobce pro logo: malá písmena, bez právní formy a interpunkce („Apple, Inc.“ → „apple“)."""
    v = str(name or "").lower()
    v = re.sub(r"[,.]?\s*(inc|incorporated|ltd|llc|corp|corporation|co|gmbh|s\.?r\.?o|a\.?s|ag|sa|bv|limited|company|technologies|technology)\.?\b", "", v)
    v = re.sub(r"[^a-z0-9]+", " ", v).strip()
    return v[:120]


def sniff(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ServiceError("logo: jen PNG, JPEG nebo WebP")


def set_client(db: Database, tenant_id: Any, mac: str, data: dict, user_id: Any = None, ip: str | None = None) -> dict:
    """Popisek, ikona a poznámka klienta; měnit lze jen některé (chybějící klíč = beze změny)."""
    m = leases.norm_mac(mac)
    if not m:
        raise ServiceError("neplatná MAC adresa")
    with tenant_scope(db, tenant_id) as tx:
        old = tx.fetchone("SELECT note, label, icon FROM client_note WHERE mac = %(m)s", {"m": m})
        new = dict(old) if old else {"note": "", "label": "", "icon": ""}
        if "note" in data:
            note = str(data.get("note") or "").strip()
            if len(note) > 500 or not _ctrl_free(note):
                raise ServiceError("poznámka: max. 500 znaků bez řídicích znaků")
            new["note"] = note
        if "label" in data:
            label = str(data.get("label") or "").strip()
            if len(label) > 64 or not _ctrl_free(label) or "\n" in label:
                raise ServiceError("popisek: max. 64 znaků na jednom řádku")
            new["label"] = label
        if "icon" in data:
            icon = str(data.get("icon") or "")
            if icon and icon not in ICONS:
                raise ServiceError("neznámá ikona")
            new["icon"] = icon
        if any(new.values()):
            tx.execute(
                """INSERT INTO client_note (tenant_id, mac, note, label, icon, updated_by)
                   VALUES (%(t)s, %(m)s, %(n)s, %(l)s, %(i)s, %(u)s)
                   ON CONFLICT (tenant_id, mac) DO UPDATE SET note = EXCLUDED.note, label = EXCLUDED.label,
                       icon = EXCLUDED.icon, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                {"t": str(tenant_id), "m": m, "n": new["note"], "l": new["label"], "i": new["icon"], "u": _s(user_id)})
        else:
            tx.execute("DELETE FROM client_note WHERE mac = %(m)s", {"m": m})
        if (dict(old) if old else {"note": "", "label": "", "icon": ""}) != new:
            audit(tx, "client.meta", tenant_id=tenant_id, user_id=user_id, target=f"mac:{m}",
                  before=dict(old) if old else None, after=new, ip=ip)
    return {"mac": m, **new}


def _logo_key(scope: str, key: str) -> str:
    if scope == "client":
        k = leases.norm_mac(key)
        if not k:
            raise ServiceError("neplatná MAC adresa")
        return k
    if scope == "vendor":
        k = vendor_key(key)
        if not k:
            raise ServiceError("neplatný výrobce")
        return k
    raise ServiceError("logo: rozsah vendor nebo client")


def put_logo(db: Database, tenant_id: Any, scope: str, key: str, b64: Any, user_id: Any = None,
             ip: str | None = None) -> dict:
    k = _logo_key(scope, key)
    try:
        raw = base64.b64decode(str(b64 or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ServiceError("logo: neplatná data") from None
    if not raw or len(raw) > LOGO_MAX:
        raise ServiceError(f"logo: max. {LOGO_MAX // 1024} KiB")
    mime = sniff(raw)
    sha = hashlib.sha256(raw).hexdigest()[:16]
    with tenant_scope(db, tenant_id) as tx:
        tx.execute(
            """INSERT INTO client_logo (tenant_id, scope, key, mime, data, sha, updated_by)
               VALUES (%(t)s, %(s)s, %(k)s, %(m)s, %(d)s, %(h)s, %(u)s)
               ON CONFLICT (tenant_id, scope, key) DO UPDATE SET mime = EXCLUDED.mime, data = EXCLUDED.data,
                   sha = EXCLUDED.sha, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            {"t": str(tenant_id), "s": scope, "k": k, "m": mime, "d": raw, "h": sha, "u": _s(user_id)})
        audit(tx, "client.logo", tenant_id=tenant_id, user_id=user_id, target=f"{scope}:{k}",
              after={"mime": mime, "bytes": len(raw), "sha": sha}, ip=ip)
    return {"scope": scope, "key": k, "sha": sha}


def delete_logo(db: Database, tenant_id: Any, scope: str, key: str, user_id: Any = None, ip: str | None = None) -> None:
    k = _logo_key(scope, key)
    with tenant_scope(db, tenant_id) as tx:
        r = tx.fetchone("DELETE FROM client_logo WHERE scope = %(s)s AND key = %(k)s RETURNING sha", {"s": scope, "k": k})
        if not r:
            raise NotFound("logo neexistuje")
        audit(tx, "client.logo_delete", tenant_id=tenant_id, user_id=user_id, target=f"{scope}:{k}", ip=ip)


def get_logo(tx: Tx, scope: str, key: str) -> dict | None:
    k = _logo_key(scope, key)
    r = tx.fetchone("SELECT mime, data, sha FROM client_logo WHERE scope = %(s)s AND key = %(k)s", {"s": scope, "k": k})
    return dict(r, data=bytes(r["data"])) if r else None


def annotate(tx: Tx, rows: list[dict]) -> list[dict]:
    """Doplní klientům (mac) výrobce, popisek, ikonu, poznámku a odkaz na logo."""
    meta = {r["mac"]: r for r in tx.fetchall("SELECT mac, note, label, icon FROM client_note")}
    logos = {(r["scope"], r["key"]): r["sha"] for r in tx.fetchall("SELECT scope, key, sha FROM client_logo")}
    for c in rows:
        mac = c.get("mac") or ""
        c.setdefault("vendor", oui.vendor(mac))
        m = meta.get(mac) or {}
        c["note"], c["label"], c["icon"] = m.get("note", ""), m.get("label", ""), m.get("icon", "")
        vk = vendor_key(c.get("vendor"))
        if ("client", mac) in logos:
            c["logo"] = f"client/{mac}?v={logos[('client', mac)]}"
        elif vk and ("vendor", vk) in logos:
            c["logo"] = f"vendor/{vk}?v={logos[('vendor', vk)]}"
        else:
            c["logo"] = None
        c["vendor_key"] = vk or None
    return rows


def list_logos(tx: Tx) -> list[dict]:
    return [dict(r) for r in tx.fetchall(
        "SELECT scope, key, sha, mime, octet_length(data) AS bytes, updated_at FROM client_logo ORDER BY scope, key")]
