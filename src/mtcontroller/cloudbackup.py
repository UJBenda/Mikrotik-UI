"""Kopie záloh controlleru do cloudu: Nextcloud (WebDAV), Google Drive, OneDrive.

Záloha (.mtcbak) je šifrovaná heslem záloh už na serveru – cloud ji jen uchovává, nemusí být
důvěryhodný. Cílů může být víc; každý má vlastní počet uchovávaných záloh (starší controller
v cloudu sám smaže – jen své soubory `mtc-*.mtcbak`).

Přihlášení:
- Nextcloud: uživatel + heslo aplikace (Nastavení → Zabezpečení → Vytvořit heslo aplikace).
- Google Drive a OneDrive: OAuth „device code“ – administrace ukáže kód, správce ho zadá na
  google.com/device / microsoft.com/devicelogin. Potřebuje vlastní registraci aplikace
  (Google Cloud: OAuth klient „TVs and Limited Input devices“, scope drive.file – controller
  vidí jen soubory, které sám vytvořil; Microsoft Entra: veřejný klient, Files.ReadWrite.AppFolder
  – jen složka Aplikace/<název aplikace>). Postup je v docs/install.md.

Tajemství (heslo aplikace, client secret, refresh token, rozpracovaný device code) jsou jen
v `backup_target` šifrovaně (SecretBox, kontext `backup_target:<id>`). Nahrává worker (uživatel
mtcontroller, zálohy čte přes skupinu) ve vlastním vlákně, ať velký upload nezdrží ostatní kroky.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import Database, Tx, operator_scope
from .security import SealedSecret, SecretBox
from .services import NotFound, ServiceError, audit

log = logging.getLogger("mtcontroller.cloudbackup")

KINDS = ("nextcloud", "gdrive", "onedrive")
KIND_LABEL = {"nextcloud": "Nextcloud", "gdrive": "Google Drive", "onedrive": "OneDrive"}
NAME_RE = re.compile(r"^mtc-[A-Za-z0-9-]{1,40}-\d{8}-\d{6}\.mtcbak$")
CHUNK = 10 * 1024 * 1024          # Nextcloud v2 ≥ 5 MiB; OneDrive násobek 320 KiB; Google násobek 256 KiB
SMALL = CHUNK                     # menší soubor jedním PUT
TIMEOUT = 120

# koncové body (testy je přesměrují na napodobené API)
URLS = {
    "google_device": "https://oauth2.googleapis.com/device/code",
    "google_token": "https://oauth2.googleapis.com/token",
    "google_api": "https://www.googleapis.com/drive/v3",
    "google_upload": "https://www.googleapis.com/upload/drive/v3/files",
    "ms_login": "https://login.microsoftonline.com",
    "ms_graph": "https://graph.microsoft.com/v1.0",
}
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.file"
MS_SCOPE = "offline_access Files.ReadWrite.AppFolder"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class CloudError(ServiceError):
    pass


# --- HTTP ---------------------------------------------------------------------------------


@dataclass
class Resp:
    status: int
    headers: dict
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"{}")
        except ValueError:
            return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # 308 u Google znamená „pokračuj v nahrávání“, ne přesměrování
    def redirect_request(self, *a, **kw):  # noqa: ANN002
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
         timeout: float = TIMEOUT) -> Resp:
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise CloudError("jen HTTPS")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as r:  # noqa: S310 – URL z ověřené konfigurace
            return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Resp(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")
    except (urllib.error.URLError, OSError) as e:
        raise CloudError(f"spojení selhalo: {getattr(e, 'reason', e)}") from e


Http = Callable[..., Resp]


def _form(d: dict) -> bytes:
    return urllib.parse.urlencode(d).encode()


def _err(r: Resp, what: str) -> CloudError:
    j = r.json()
    msg = ""
    if isinstance(j, dict):
        e = j.get("error")
        msg = j.get("error_description") or (e.get("message") if isinstance(e, dict) else e) or ""
    if not msg:
        msg = r.body.decode(errors="replace")[:200].strip()
    return CloudError(f"{what}: HTTP {r.status} {msg or ''}".strip())


def _chunks(path: Path, size: int):
    with open(path, "rb") as fh:
        off = 0
        while True:
            b = fh.read(size)
            if not b:
                return
            yield off, b
            off += len(b)


# --- záznam cíle --------------------------------------------------------------------------


@dataclass
class Target:
    id: str
    kind: str
    name: str
    enabled: bool
    keep: int
    config: dict
    secret: dict
    status: dict


def _ctx(tid: str) -> bytes:
    return f"backup_target:{tid}".encode()


def _open_secret(box: SecretBox, r: dict) -> dict:
    if not r.get("wrapped_key"):
        return {}
    raw = box.open(SealedSecret(bytes(r["wrapped_key"]), bytes(r["ciphertext"]), r["key_version"]), _ctx(str(r["id"])))
    return json.loads(raw)


def _seal(box: SecretBox, tid: str, secret: dict) -> dict:
    s = box.seal(json.dumps(secret).encode(), _ctx(tid))
    return {"w": s.wrapped_key, "c": s.ciphertext, "kv": s.key_version}


def _row(tx: Tx, tid: str) -> dict:
    r = tx.fetchone("SELECT * FROM backup_target WHERE id = %(i)s", {"i": tid})
    if not r:
        raise NotFound("cíl záloh neexistuje")
    return r


def load(tx: Tx, box: SecretBox, tid: str) -> Target:
    r = _row(tx, tid)
    return Target(str(r["id"]), r["kind"], r["name"], r["enabled"], r["keep"], r["config"] or {},
                  _open_secret(box, r), r["status"] or {})


def store_secret(tx: Tx, box: SecretBox, tid: str, secret: dict) -> None:
    s = _seal(box, tid, secret)
    tx.execute("UPDATE backup_target SET wrapped_key = %(w)s, ciphertext = %(c)s, key_version = %(kv)s, "
               "updated_at = now() WHERE id = %(i)s", {**s, "i": tid})


def set_status(tx: Tx, tid: str, **kw) -> None:
    tx.execute("UPDATE backup_target SET status = status || %(s)s WHERE id = %(i)s", {"s": kw, "i": tid})


# --- ověření vstupu z UI ------------------------------------------------------------------

_HOST_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~%/-]*)?$")
_FOLDER = re.compile(r"^[^\x00-\x1f\\:*?\"<>|]{1,120}$")
_CLIENT = re.compile(r"^[A-Za-z0-9._-]{8,200}$")
_TENANT = re.compile(r"^(common|consumers|organizations|[0-9a-fA-F-]{36}|[A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def _folder(v: Any, default: str) -> str:
    f = str(v or default).strip().strip("/")
    if not _FOLDER.match(f) or any(p in ("", ".", "..") for p in f.split("/")):
        raise ServiceError("neplatná složka")
    return f


def validate(kind: str, data: dict, old_secret: dict | None = None) -> tuple[dict, dict]:
    """(config, secret) z formuláře. Prázdné heslo/secret při úpravě = ponechat původní."""
    old = old_secret or {}
    if kind == "nextcloud":
        url = str(data.get("url") or "").strip().rstrip("/")
        if not _HOST_URL.match(url):
            raise ServiceError("adresa Nextcloudu ve tvaru https://cloud.firma.cz")
        user = str(data.get("user") or "").strip()
        if not re.match(r"^[^\x00-\x1f/\\]{1,64}$", user):
            raise ServiceError("uživatel Nextcloudu")
        pw = str(data.get("password") or "") or old.get("password", "")
        if not 8 <= len(pw) <= 200:
            raise ServiceError("heslo aplikace z Nextcloudu (Nastavení → Zabezpečení)")
        return {"url": url, "user": user, "folder": _folder(data.get("folder"), "MikroTik-Controller")}, \
            {"password": pw}
    if kind == "gdrive":
        cid = str(data.get("client_id") or "").strip()
        if not _CLIENT.match(cid) or not cid.endswith(".apps.googleusercontent.com"):
            raise ServiceError("Client ID z Google Cloud (…apps.googleusercontent.com)")
        cs = str(data.get("client_secret") or "") or old.get("client_secret", "")
        if not 8 <= len(cs) <= 200:
            raise ServiceError("Client secret z Google Cloud")
        sec = {k: v for k, v in old.items() if k == "refresh_token"} if old.get("client_id") == cid else {}
        return {"client_id": cid, "folder": _folder(data.get("folder"), "MikroTik-Controller")}, \
            {**sec, "client_id": cid, "client_secret": cs}
    if kind == "onedrive":
        cid = str(data.get("client_id") or "").strip()
        if not re.match(r"^[0-9a-fA-F-]{36}$", cid):
            raise ServiceError("Application (client) ID z Microsoft Entra (GUID)")
        ten = str(data.get("tenant") or "common").strip()
        if not _TENANT.match(ten):
            raise ServiceError("tenant: common, consumers, organizations nebo ID / doména")
        sec = {k: v for k, v in old.items() if k == "refresh_token"} if old.get("client_id") == cid else {}
        return {"client_id": cid, "tenant": ten}, {**sec, "client_id": cid}
    raise ServiceError("neznámý typ cíle")


def needs_auth(t: Target) -> bool:
    return t.kind in ("gdrive", "onedrive") and not t.secret.get("refresh_token")


def public(r: dict) -> dict:
    """Pro UI – bez tajemství."""
    st = r["status"] or {}
    return {"id": str(r["id"]), "kind": r["kind"], "kind_label": KIND_LABEL[r["kind"]], "name": r["name"],
            "enabled": r["enabled"], "keep": r["keep"], "config": r["config"] or {},
            "has_secret": r["wrapped_key"] is not None, "status": {k: v for k, v in st.items() if k != "device"},
            "auth": st.get("device"), "authorized": bool(st.get("authorized"))}


def listing(db: Database) -> list[dict]:
    with operator_scope(db) as tx:
        return [public(r) for r in tx.fetchall("SELECT * FROM backup_target ORDER BY created_at")]


def create(db: Database, box: SecretBox, data: dict, user_id: Any = None, ip: str | None = None) -> str:
    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        raise ServiceError("neznámý typ cíle")
    cfg, sec = validate(kind, data)
    name = str(data.get("name") or KIND_LABEL[kind]).strip()[:80] or KIND_LABEL[kind]
    keep = _keep(data.get("keep", 14))
    tid = str(uuid.uuid4())
    with operator_scope(db) as tx:
        tx.execute("""INSERT INTO backup_target (id, kind, name, keep, config, created_by, status)
                      VALUES (%(i)s, %(k)s, %(n)s, %(keep)s, %(c)s, %(u)s, %(s)s)""",
                   {"i": tid, "k": kind, "n": name, "keep": keep, "c": cfg, "u": user_id,
                    "s": {"authorized": kind == "nextcloud"}})
        store_secret(tx, box, tid, sec)
        audit(tx, "system.backup_target_create", user_id=user_id, target=f"backup_target:{tid}",
              after={"kind": kind, "name": name, "keep": keep, "config": cfg}, ip=ip)
    return tid


def _keep(v: Any) -> int:
    try:
        k = int(v)
    except (TypeError, ValueError):
        raise ServiceError("počet záloh 1–365") from None
    if not 1 <= k <= 365:
        raise ServiceError("počet záloh 1–365")
    return k


def update(db: Database, box: SecretBox, tid: str, data: dict, user_id: Any = None, ip: str | None = None) -> None:
    with operator_scope(db) as tx:
        t = load(tx, box, tid)
        cfg, sec = t.config, t.secret
        if any(k in data for k in ("url", "user", "password", "folder", "client_id", "client_secret", "tenant")):
            cfg, sec = validate(t.kind, {**t.config, **data}, t.secret)
            if t.kind == "gdrive" and cfg.get("folder") == t.config.get("folder"):
                cfg["folder_id"] = t.config.get("folder_id")
            cfg = {k: v for k, v in cfg.items() if v is not None}
        name = str(data.get("name", t.name)).strip()[:80] or t.name
        keep = _keep(data.get("keep", t.keep))
        enabled = bool(data.get("enabled", t.enabled))
        tx.execute("""UPDATE backup_target SET name = %(n)s, keep = %(k)s, enabled = %(e)s, config = %(c)s,
                      updated_at = now() WHERE id = %(i)s""", {"n": name, "k": keep, "e": enabled, "c": cfg, "i": tid})
        if sec != t.secret:
            store_secret(tx, box, tid, sec)
            if t.kind != "nextcloud" and not sec.get("refresh_token"):
                set_status(tx, tid, authorized=False)
        audit(tx, "system.backup_target_update", user_id=user_id, target=f"backup_target:{tid}",
              before={"name": t.name, "keep": t.keep, "enabled": t.enabled, "config": t.config},
              after={"name": name, "keep": keep, "enabled": enabled, "config": cfg,
                     "secret_changed": sec != t.secret}, ip=ip)


def delete(db: Database, tid: str, user_id: Any = None, ip: str | None = None) -> None:
    with operator_scope(db) as tx:
        r = _row(tx, tid)
        tx.execute("DELETE FROM backup_target WHERE id = %(i)s", {"i": tid})
        audit(tx, "system.backup_target_delete", user_id=user_id, target=f"backup_target:{tid}",
              before={"kind": r["kind"], "name": r["name"], "config": r["config"]}, ip=ip)


def request_upload(db: Database, tid: str, user_id: Any = None, ip: str | None = None) -> None:
    """„Odeslat teď“ – worker nahraje poslední zálohu při nejbližším kole."""
    with operator_scope(db) as tx:
        _row(tx, tid)
        set_status(tx, tid, force=True, next_try=None)
        audit(tx, "system.backup_target_upload_request", user_id=user_id, target=f"backup_target:{tid}", ip=ip)


# --- OAuth device code (Google, Microsoft) ------------------------------------------------


def _ms(t: Target, path: str) -> str:
    return f"{URLS['ms_login']}/{t.config.get('tenant') or 'common'}/oauth2/v2.0/{path}"


def device_start(db: Database, box: SecretBox, tid: str, user_id: Any = None, ip: str | None = None,
                 req: Http = http) -> dict:
    """Začne přihlášení: vrátí kód a adresu, kam ho zadat."""
    with operator_scope(db) as tx:
        t = load(tx, box, tid)
    if t.kind == "gdrive":
        r = req("POST", URLS["google_device"], {"Content-Type": "application/x-www-form-urlencoded"},
                _form({"client_id": t.secret["client_id"], "scope": GOOGLE_SCOPE}))
    elif t.kind == "onedrive":
        r = req("POST", _ms(t, "devicecode"), {"Content-Type": "application/x-www-form-urlencoded"},
                _form({"client_id": t.secret["client_id"], "scope": MS_SCOPE}))
    else:
        raise ServiceError("Nextcloud se přihlašuje heslem aplikace")
    if r.status != 200:
        raise _err(r, "přihlášení nejde zahájit (zkontroluj Client ID a typ aplikace)")
    j = r.json()
    if not j.get("device_code") or not j.get("user_code"):
        raise CloudError("odpověď bez kódu zařízení")
    url = j.get("verification_url") or j.get("verification_uri") or ""
    dev = {"user_code": str(j["user_code"])[:32], "url": str(url)[:200],
           "expires_at": time.time() + int(j.get("expires_in") or 900), "interval": max(5, int(j.get("interval") or 5)),
           "next_poll": time.time() + max(5, int(j.get("interval") or 5))}
    with operator_scope(db) as tx:
        store_secret(tx, box, tid, {**t.secret, "device_code": j["device_code"]})
        set_status(tx, tid, device=dev)
        audit(tx, "system.backup_target_auth_start", user_id=user_id, target=f"backup_target:{tid}", ip=ip)
    return dev


def device_poll(db: Database, box: SecretBox, tid: str, req: Http = http, now: float | None = None) -> dict:
    """Dotaz, jestli už správce kód zadal. Vrací {state: pending|done|error, …}."""
    now = now or time.time()
    with operator_scope(db) as tx:
        t = load(tx, box, tid)
    dev = t.status.get("device")
    if not dev or not t.secret.get("device_code"):
        return {"state": "done" if t.secret.get("refresh_token") else "none"}
    if now > dev["expires_at"]:
        _device_end(db, box, t, error="kód vypršel – začni přihlášení znovu")
        return {"state": "error", "error": "kód vypršel – začni přihlášení znovu"}
    if now < dev.get("next_poll", 0):
        return {"state": "pending", **dev}
    form = {"client_id": t.secret["client_id"], "device_code": t.secret["device_code"], "grant_type": DEVICE_GRANT}
    if t.kind == "gdrive":
        form["client_secret"] = t.secret["client_secret"]
        r = req("POST", URLS["google_token"], {"Content-Type": "application/x-www-form-urlencoded"}, _form(form))
    else:
        r = req("POST", _ms(t, "token"), {"Content-Type": "application/x-www-form-urlencoded"}, _form(form))
    j = r.json()
    if r.status == 200 and j.get("refresh_token"):
        sec = {k: v for k, v in t.secret.items() if k != "device_code"} | {"refresh_token": j["refresh_token"]}
        with operator_scope(db) as tx:
            store_secret(tx, box, t.id, sec)
            tx.execute("UPDATE backup_target SET status = (status - 'device') || %(s)s WHERE id = %(i)s",
                       {"s": {"authorized": True, "authorized_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                              "last_error": None}, "i": t.id})
            audit(tx, "system.backup_target_authorized", target=f"backup_target:{t.id}")
        return {"state": "done"}
    err = j.get("error") if isinstance(j.get("error"), str) else ""
    if err in ("authorization_pending", "slow_down"):
        step = dev["interval"] + (5 if err == "slow_down" else 0)
        dev = {**dev, "interval": step, "next_poll": now + step}
        with operator_scope(db) as tx:
            set_status(tx, t.id, device=dev)
        return {"state": "pending", **dev}
    if r.status == 200:
        msg = "přihlášení nevrátilo refresh token (chybí offline přístup)"
    else:
        msg = {"access_denied": "přístup odmítnut", "authorization_declined": "přístup odmítnut",
               "expired_token": "kód vypršel"}.get(err) or str(_err(r, "přihlášení selhalo"))
    _device_end(db, box, t, error=msg)
    return {"state": "error", "error": msg}


def _device_end(db: Database, box: SecretBox, t: Target, error: str) -> None:
    with operator_scope(db) as tx:
        store_secret(tx, box, t.id, {k: v for k, v in t.secret.items() if k != "device_code"})
        tx.execute("UPDATE backup_target SET status = (status - 'device') || %(s)s WHERE id = %(i)s",
                   {"s": {"auth_error": error}, "i": t.id})


def _access_token(db: Database, box: SecretBox, t: Target, req: Http) -> str:
    rt = t.secret.get("refresh_token")
    if not rt:
        raise CloudError("cíl není přihlášený – v administraci klikni na Přihlásit")
    if t.kind == "gdrive":
        r = req("POST", URLS["google_token"], {"Content-Type": "application/x-www-form-urlencoded"},
                _form({"client_id": t.secret["client_id"], "client_secret": t.secret["client_secret"],
                       "refresh_token": rt, "grant_type": "refresh_token"}))
    else:
        r = req("POST", _ms(t, "token"), {"Content-Type": "application/x-www-form-urlencoded"},
                _form({"client_id": t.secret["client_id"], "refresh_token": rt, "grant_type": "refresh_token",
                       "scope": MS_SCOPE}))
    j = r.json()
    if r.status != 200 or not j.get("access_token"):
        if j.get("error") == "invalid_grant":
            with operator_scope(db) as tx:
                store_secret(tx, box, t.id, {k: v for k, v in t.secret.items() if k != "refresh_token"})
                set_status(tx, t.id, authorized=False)
            raise CloudError("přihlášení vypršelo nebo bylo odvoláno – přihlas cíl znovu")
        raise _err(r, "obnovení přístupu selhalo")
    # Microsoft refresh token rotuje – nový hned uložit
    if j.get("refresh_token") and j["refresh_token"] != rt:
        t.secret = {**t.secret, "refresh_token": j["refresh_token"]}
        with operator_scope(db) as tx:
            store_secret(tx, box, t.id, t.secret)
    return j["access_token"]


# --- poskytovatelé ------------------------------------------------------------------------


class Nextcloud:
    def __init__(self, t: Target, req: Http):
        c = t.config
        self.req = req
        import base64
        tok = base64.b64encode(f"{c['user']}:{t.secret['password']}".encode()).decode()
        self.auth = {"Authorization": f"Basic {tok}", "OCS-APIRequest": "true"}
        self.base = c["url"]
        self.user = urllib.parse.quote(c["user"], safe="")
        self.folder = c["folder"]
        self.dav = f"{self.base}/remote.php/dav/files/{self.user}/" + "/".join(
            urllib.parse.quote(p, safe="") for p in self.folder.split("/"))

    def _ensure_folder(self) -> None:
        path = f"{self.base}/remote.php/dav/files/{self.user}"
        for p in self.folder.split("/"):
            path += "/" + urllib.parse.quote(p, safe="")
            r = self.req("MKCOL", path, self.auth)
            if r.status == 401:
                raise CloudError("Nextcloud odmítl přihlášení (uživatel / heslo aplikace)")
            if r.status not in (201, 405):
                raise _err(r, f"složku {self.folder} nejde vytvořit")

    def check(self) -> str:
        self._ensure_folder()
        self.list()
        return f"{self.base} · {self.folder}"

    def upload(self, path: Path) -> None:
        self._ensure_folder()
        dest = f"{self.dav}/{urllib.parse.quote(path.name)}"
        size = path.stat().st_size
        if size <= SMALL:
            r = self.req("PUT", dest, {**self.auth, "Content-Type": "application/octet-stream"}, path.read_bytes())
            if r.status not in (200, 201, 204):
                raise _err(r, "nahrání selhalo")
            return
        # chunked upload v2: uploads/<user>/<id>/<n>, pak MOVE .file → cíl
        up = f"{self.base}/remote.php/dav/uploads/{self.user}/mtc-{uuid.uuid4().hex}"
        hdr = {**self.auth, "Destination": dest}
        r = self.req("MKCOL", up, hdr)
        if r.status not in (201, 405):
            raise _err(r, "nahrání nejde zahájit")
        try:
            for n, (off, b) in enumerate(_chunks(path, CHUNK), 1):
                r = self.req("PUT", f"{up}/{n}", {**hdr, "OC-Total-Length": str(size),
                                                       "Content-Type": "application/octet-stream"}, b)
                if r.status not in (200, 201, 204):
                    raise _err(r, f"nahrání části {n} selhalo")
            r = self.req("MOVE", f"{up}/.file", {**hdr, "Overwrite": "T", "OC-Total-Length": str(size)},
                         timeout=900)
            if r.status not in (200, 201, 204):
                raise _err(r, "dokončení nahrání selhalo")
        except Exception:
            self.req("DELETE", up, self.auth)
            raise

    def list(self) -> list[dict]:
        r = self.req("PROPFIND", self.dav, {**self.auth, "Depth": "1", "Content-Type": "application/xml"},
                     b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/>'
                     b'<d:getlastmodified/></d:prop></d:propfind>')
        if r.status == 401:
            raise CloudError("Nextcloud odmítl přihlášení (uživatel / heslo aplikace)")
        if r.status != 207:
            raise _err(r, "výpis složky selhal")
        out = []
        for resp in ET.fromstring(r.body).iter("{DAV:}response"):
            href = urllib.parse.unquote(resp.findtext("{DAV:}href") or "")
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if NAME_RE.match(name):
                out.append({"id": name, "name": name, "size": int(resp.findtext(".//{DAV:}getcontentlength") or 0)})
        return out

    def delete(self, item: dict) -> None:
        r = self.req("DELETE", f"{self.dav}/{urllib.parse.quote(item['name'])}", self.auth)
        if r.status not in (200, 204, 404):
            raise _err(r, "smazání staré zálohy selhalo")


class GoogleDrive:
    def __init__(self, t: Target, req: Http, token: str, save_config: Callable[[dict], None]):
        self.t, self.req, self.save = t, req, save_config
        self.auth = {"Authorization": f"Bearer {token}"}

    def _folder_id(self, create: bool = True) -> str:
        fid = self.t.config.get("folder_id")
        if fid:
            r = self.req("GET", f"{URLS['google_api']}/files/{urllib.parse.quote(fid)}?fields=id,trashed", self.auth)
            if r.status == 200 and not r.json().get("trashed"):
                return fid
        if not create:
            return ""
        body = json.dumps({"name": self.t.config["folder"], "mimeType": "application/vnd.google-apps.folder"}).encode()
        r = self.req("POST", f"{URLS['google_api']}/files?fields=id", {**self.auth, "Content-Type": "application/json"},
                     body)
        if r.status not in (200, 201) or not r.json().get("id"):
            raise _err(r, "složku na Google Drive nejde vytvořit")
        fid = r.json()["id"]
        self.t.config = {**self.t.config, "folder_id": fid}
        self.save(self.t.config)
        return fid

    def check(self) -> str:
        r = self.req("GET", f"{URLS['google_api']}/about?fields=user(emailAddress)", self.auth)
        who = (r.json().get("user") or {}).get("emailAddress", "") if r.status == 200 else ""
        self._folder_id()
        return f"{who} · {self.t.config['folder']}".strip(" ·")

    def upload(self, path: Path) -> None:
        fid = self._folder_id()
        size = path.stat().st_size
        meta = json.dumps({"name": path.name, "parents": [fid]}).encode()
        r = self.req("POST", f"{URLS['google_upload']}?uploadType=resumable",
                     {**self.auth, "Content-Type": "application/json; charset=UTF-8",
                      "X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(size)}, meta)
        loc = r.headers.get("location")
        if r.status != 200 or not loc:
            raise _err(r, "nahrání na Google Drive nejde zahájit")
        if size == 0:
            raise CloudError("prázdný soubor")
        for off, b in _chunks(path, CHUNK):
            end = off + len(b) - 1
            r = self.req("PUT", loc, {"Content-Length": str(len(b)), "Content-Range": f"bytes {off}-{end}/{size}"}, b)
            last = end + 1 == size
            if last and r.status in (200, 201):
                return
            if not last and r.status == 308:
                continue
            raise _err(r, "nahrání na Google Drive selhalo")
        raise CloudError("nahrání na Google Drive nedokončeno")

    def list(self) -> list[dict]:
        fid = self._folder_id(create=False)
        if not fid:
            return []
        out, token = [], ""
        q = urllib.parse.quote(f"'{fid}' in parents and trashed = false")
        while True:
            url = f"{URLS['google_api']}/files?q={q}&fields=nextPageToken,files(id,name,size)&pageSize=1000"
            r = self.req("GET", url + (f"&pageToken={urllib.parse.quote(token)}" if token else ""), self.auth)
            if r.status != 200:
                raise _err(r, "výpis Google Drive selhal")
            j = r.json()
            out += [{"id": f["id"], "name": f["name"], "size": int(f.get("size") or 0)}
                    for f in j.get("files", []) if NAME_RE.match(f.get("name", ""))]
            token = j.get("nextPageToken") or ""
            if not token:
                return out

    def delete(self, item: dict) -> None:
        r = self.req("DELETE", f"{URLS['google_api']}/files/{urllib.parse.quote(item['id'])}", self.auth)
        if r.status not in (200, 204, 404):
            raise _err(r, "smazání staré zálohy selhalo")


class OneDrive:
    def __init__(self, t: Target, req: Http, token: str):
        self.t, self.req = t, req
        self.auth = {"Authorization": f"Bearer {token}"}
        self.root = f"{URLS['ms_graph']}/me/drive/special/approot"

    def check(self) -> str:
        r = self.req("GET", f"{self.root}?$select=name,webUrl", self.auth)
        if r.status != 200:
            raise _err(r, "složka aplikace na OneDrive není dostupná")
        return f"OneDrive · Aplikace/{r.json().get('name', '')}"

    def upload(self, path: Path) -> None:
        size = path.stat().st_size
        r = self.req("POST", f"{self.root}:/{urllib.parse.quote(path.name)}:/createUploadSession",
                     {**self.auth, "Content-Type": "application/json"},
                     json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode())
        url = r.json().get("uploadUrl")
        if r.status != 200 or not url:
            raise _err(r, "nahrání na OneDrive nejde zahájit")
        try:
            for off, b in _chunks(path, CHUNK):
                end = off + len(b) - 1
                # uploadUrl je předem podepsaná – bez hlavičky Authorization
                r = self.req("PUT", url, {"Content-Length": str(len(b)), "Content-Range": f"bytes {off}-{end}/{size}"}, b)
                if end + 1 == size and r.status in (200, 201):
                    return
                if end + 1 < size and r.status == 202:
                    continue
                raise _err(r, "nahrání na OneDrive selhalo")
        except Exception:
            self.req("DELETE", url, {})
            raise
        raise CloudError("nahrání na OneDrive nedokončeno")

    def list(self) -> list[dict]:
        out = []
        url = f"{self.root}/children?$select=id,name,size&$top=200"
        while url:
            r = self.req("GET", url, self.auth)
            if r.status != 200:
                raise _err(r, "výpis OneDrive selhal")
            j = r.json()
            out += [{"id": f["id"], "name": f["name"], "size": int(f.get("size") or 0)}
                    for f in j.get("value", []) if NAME_RE.match(f.get("name", ""))]
            url = j.get("@odata.nextLink") or ""
        return out

    def delete(self, item: dict) -> None:
        r = self.req("DELETE", f"{URLS['ms_graph']}/me/drive/items/{urllib.parse.quote(item['id'])}", self.auth)
        if r.status not in (200, 204, 404):
            raise _err(r, "smazání staré zálohy selhalo")


def provider(db: Database, box: SecretBox, t: Target, req: Http = http):
    if t.kind == "nextcloud":
        return Nextcloud(t, req)
    token = _access_token(db, box, t, req)
    if t.kind == "gdrive":
        def save(cfg: dict) -> None:
            with operator_scope(db) as tx:
                tx.execute("UPDATE backup_target SET config = %(c)s WHERE id = %(i)s", {"c": cfg, "i": t.id})
        return GoogleDrive(t, req, token, save)
    return OneDrive(t, req, token)


def check(db: Database, box: SecretBox, tid: str, req: Http = http) -> str:
    """„Ověřit“ v UI: přihlášení a složka (bez nahrávání)."""
    with operator_scope(db) as tx:
        t = load(tx, box, tid)
    where = provider(db, box, t, req).check()
    with operator_scope(db) as tx:
        set_status(tx, tid, where=where, checked_at=_now())
    return where


# --- nahrávání (worker) -------------------------------------------------------------------


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def newest_local(backup_dir: Path) -> Path | None:
    files = sorted((p for p in backup_dir.glob("mtc-*.mtcbak") if NAME_RE.match(p.name)),
                   key=lambda p: p.name[-22:])
    return files[-1] if files else None


def backoff(attempts: int) -> float:
    return min(15 * 60 * 2 ** max(attempts - 1, 0), 6 * 3600)


def upload_one(db: Database, box: SecretBox, t: Target, path: Path, req: Http = http) -> dict:
    """Nahraje soubor a promaže v cíli staré zálohy (nechá `keep` posledních). Zapíše stav a audit."""
    t0 = time.monotonic()
    try:
        p = provider(db, box, t, req)
        p.upload(path)
        removed = []
        items = sorted(p.list(), key=lambda x: x["name"][-22:])
        for item in items[:-t.keep] if len(items) > t.keep else []:
            if item["name"] != path.name:
                p.delete(item)
                removed.append(item["name"])
    except Exception as e:  # noqa: BLE001 – stav a audit chyby
        msg = str(e)[:300] if isinstance(e, CloudError) else f"{type(e).__name__}: {e}"[:300]
        attempts = int(t.status.get("attempts") or 0) + 1
        with operator_scope(db) as tx:
            set_status(tx, t.id, last_error=msg, last_error_at=_now(), attempts=attempts, force=False,
                       failed_file=path.name, next_try=time.time() + backoff(attempts))
            audit(tx, "system.backup_upload_failed", target=f"backup_target:{t.id}",
                  after={"file": path.name, "error": msg, "attempts": attempts})
        log.warning("záloha %s → %s selhala: %s", path.name, t.name, msg)
        return {"ok": False, "error": msg}
    secs = round(time.monotonic() - t0, 1)
    with operator_scope(db) as tx:
        set_status(tx, t.id, last_ok_at=_now(), last_file=path.name, last_size=path.stat().st_size,
                   last_seconds=secs, last_error=None, attempts=0, force=False, next_try=None, failed_file=None,
                   remote_count=min(len(items), t.keep))
        audit(tx, "system.backup_upload", target=f"backup_target:{t.id}",
              after={"file": path.name, "size": path.stat().st_size, "seconds": secs, "removed": removed})
    return {"ok": True, "removed": removed}


def pending(db: Database, box: SecretBox, backup_dir: Path, now: float | None = None) -> list[tuple[Target, Path]]:
    newest = newest_local(backup_dir)
    if not newest:
        return []
    now = now or time.time()
    out = []
    with operator_scope(db) as tx:
        ids = [str(r["id"]) for r in tx.fetchall("SELECT id FROM backup_target WHERE enabled ORDER BY created_at")]
        targets = [load(tx, box, i) for i in ids]
    for t in targets:
        if needs_auth(t):
            continue
        st = t.status
        if st.get("force"):
            out.append((t, newest))
        elif st.get("last_file") != newest.name and now >= float(st.get("next_try") or 0):
            out.append((t, newest))
    return out


_lock = threading.Lock()


def sync(db: Database, box: SecretBox, backup_dir: Path, req: Http = http, background: bool = True) -> int:
    """Krok workeru: nahraje nejnovější zálohu tam, kde ještě není. Velký upload běží ve vlákně
    (krok hned vrátí); souběžně nikdy dva."""
    todo = pending(db, box, backup_dir)
    if not todo:
        return 0
    if not _lock.acquire(blocking=False):
        return 0

    def work() -> None:
        try:
            for t, path in todo:
                upload_one(db, box, t, path, req)
        finally:
            _lock.release()

    if background:
        threading.Thread(target=work, name="mtc-cloud-backup", daemon=True).start()
    else:
        work()
    return len(todo)
