"""Zálohy do cloudu (Nextcloud, Google Drive, OneDrive) proti napodobeným API, plán záloh
a hlídání (alerty provozovatele)."""

import base64
import datetime as dt
import json
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path

from helpers import drop_db

from mtcontroller import backupplan, cloudbackup, sysalerts
from mtcontroller.cloudbackup import Resp
from mtcontroller.db import operator_scope
from mtcontroller.services import ServiceError

KB256, KB320 = 256 * 1024, 320 * 1024


def _q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class FakeNextcloud:
    def __init__(self, user="mtc", password="app-pass-1234"):
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.base = "https://cloud.test/remote.php/dav"
        self.dirs = {f"/files/{user}"}
        self.files: dict[str, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = headers or {}
        self.calls.append((method, url))
        if headers.get("Authorization") != self.auth:
            return Resp(401, {}, b"")
        path = urllib.parse.unquote(url[len(self.base):])
        if method == "MKCOL":
            if path.startswith("/uploads/"):
                self.uploads[path] = {}
                return Resp(201, {}, b"")
            if path in self.dirs:
                return Resp(405, {}, b"")
            assert path.rsplit("/", 1)[0] in self.dirs, "rodič musí existovat"
            self.dirs.add(path)
            return Resp(201, {}, b"")
        if method == "PUT":
            up = path.rsplit("/", 1)
            if up[0] in self.uploads:
                assert headers.get("Destination", "").startswith(self.base)
                self.uploads[up[0]][int(up[1])] = body
                return Resp(201, {}, b"")
            assert path.rsplit("/", 1)[0] in self.dirs
            self.files[path] = body
            return Resp(201, {}, b"")
        if method == "MOVE":
            up = path.rsplit("/", 1)[0]
            dest = urllib.parse.unquote(headers["Destination"][len(self.base):])
            parts = self.uploads.pop(up)
            self.files[dest] = b"".join(parts[k] for k in sorted(parts))
            assert int(headers["OC-Total-Length"]) == len(self.files[dest])
            return Resp(201, {}, b"")
        if method == "PROPFIND":
            items = [p for p in self.files if p.rsplit("/", 1)[0] == path]
            xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(
                f"<d:response><d:href>/remote.php/dav{urllib.parse.quote(p)}</d:href><d:propstat><d:prop>"
                f"<d:getcontentlength>{len(self.files[p])}</d:getcontentlength></d:prop></d:propstat></d:response>"
                for p in items) + "</d:multistatus>"
            return Resp(207, {}, xml.encode())
        if method == "DELETE":
            self.files.pop(path, None)
            self.uploads.pop(path, None)
            return Resp(204, {}, b"")
        return Resp(400, {}, b"")


class FakeGoogle:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.polls = 0
        self.refresh = "rt-google"
        self.n = 0

    def _id(self):
        self.n += 1
        return f"g{self.n}"

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = headers or {}
        U = cloudbackup.URLS
        if url == U["google_device"]:
            f = dict(urllib.parse.parse_qsl(body.decode()))
            assert f["scope"] == cloudbackup.GOOGLE_SCOPE
            return Resp(200, {}, json.dumps({"device_code": "DEV-G", "user_code": "ABCD-EFGH",
                                             "verification_url": "https://www.google.com/device",
                                             "expires_in": 1800, "interval": 5}).encode())
        if url == U["google_token"]:
            f = dict(urllib.parse.parse_qsl(body.decode()))
            assert f["client_secret"] == "GOCSPX-secret"
            if f["grant_type"] == cloudbackup.DEVICE_GRANT:
                self.polls += 1
                if self.polls < 2:
                    return Resp(428, {}, b'{"error": "authorization_pending"}')
                return Resp(200, {}, json.dumps({"access_token": "at", "refresh_token": self.refresh}).encode())
            if f["refresh_token"] != self.refresh:
                return Resp(400, {}, b'{"error": "invalid_grant"}')
            return Resp(200, {}, b'{"access_token": "at"}')
        if headers.get("Authorization") not in ("Bearer at", None):
            return Resp(401, {}, b"")
        if url.startswith(U["google_upload"]) and method == "POST":
            meta = json.loads(body)
            sid = self._id()
            self.sessions[sid] = {"meta": meta, "data": b"", "size": int(headers["X-Upload-Content-Length"])}
            return Resp(200, {"location": f"https://upload.test/s/{sid}"}, b"")
        if url.startswith("https://upload.test/s/"):
            s = self.sessions[url.rsplit("/", 1)[1]]
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", headers["Content-Range"])
            a, b, total = map(int, m.groups())
            assert a == len(s["data"]) and b - a + 1 == len(body) and total == s["size"]
            s["data"] += body
            if len(s["data"]) < total:
                assert len(body) % KB256 == 0
                return Resp(308, {"range": f"bytes=0-{b}"}, b"")
            fid = self._id()
            self.files[fid] = {"name": s["meta"]["name"], "parents": s["meta"]["parents"], "data": s["data"]}
            return Resp(200, {}, json.dumps({"id": fid}).encode())
        api = U["google_api"]
        if url.startswith(f"{api}/about"):
            return Resp(200, {}, b'{"user": {"emailAddress": "it@firma.cz"}}')
        if url.startswith(f"{api}/files?fields=id") and method == "POST":
            fid = self._id()
            self.files[fid] = {"name": json.loads(body)["name"], "folder": True, "parents": []}
            return Resp(200, {}, json.dumps({"id": fid}).encode())
        if url.startswith(f"{api}/files?") and method == "GET":
            parent = re.match(r"'([^']+)' in parents", _q(url)["q"]).group(1)
            files = [{"id": k, "name": v["name"], "size": str(len(v["data"]))} for k, v in self.files.items()
                     if parent in v.get("parents", [])]
            return Resp(200, {}, json.dumps({"files": files}).encode())
        m = re.match(re.escape(api) + r"/files/([^?]+)", url)
        if m:
            fid = m.group(1)
            if fid not in self.files:
                return Resp(404, {}, b"{}")
            if method == "DELETE":
                del self.files[fid]
                return Resp(204, {}, b"")
            return Resp(200, {}, json.dumps({"id": fid, "trashed": False}).encode())
        return Resp(400, {}, url.encode())


class FakeOneDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.sessions = {}
        self.polls = 0
        self.refresh = "rt-ms-1"
        self.n = 0

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = headers or {}
        login = cloudbackup.URLS["ms_login"] + "/common/oauth2/v2.0/"
        graph = cloudbackup.URLS["ms_graph"]
        if url == login + "devicecode":
            return Resp(200, {}, json.dumps({"device_code": "DEV-M", "user_code": "F7KQ2MXP",
                                             "verification_uri": "https://microsoft.com/devicelogin",
                                             "expires_in": 900, "interval": 5}).encode())
        if url == login + "token":
            f = dict(urllib.parse.parse_qsl(body.decode()))
            if f["grant_type"] == cloudbackup.DEVICE_GRANT:
                self.polls += 1
                if self.polls == 1:
                    return Resp(400, {}, b'{"error": "slow_down"}')
                return Resp(200, {}, json.dumps({"access_token": "at", "refresh_token": self.refresh}).encode())
            if f["refresh_token"] != self.refresh:
                return Resp(400, {}, b'{"error": "invalid_grant", "error_description": "AADSTS70000"}')
            n = int(self.refresh.rsplit("-", 1)[1]) + 1
            self.refresh = f"rt-ms-{n}"  # rotace
            return Resp(200, {}, json.dumps({"access_token": "at", "refresh_token": self.refresh}).encode())
        root = f"{graph}/me/drive/special/approot"
        if url.startswith("https://up.test/"):
            assert "Authorization" not in headers  # předem podepsaná URL
            s = self.sessions[url]
            if method == "DELETE":
                return Resp(204, {}, b"")
            a, b, total = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)", headers["Content-Range"]).groups())
            assert a == len(s["data"])
            s["data"] += body
            if len(s["data"]) < total:
                assert len(body) % KB320 == 0
                return Resp(202, {}, b"{}")
            self.n += 1
            self.files[f"i{self.n}"] = {"name": s["name"], "data": s["data"]}
            return Resp(201, {}, b"{}")
        if headers.get("Authorization") != "Bearer at":
            return Resp(401, {}, b"")
        m = re.match(re.escape(root) + r":/([^:]+):/createUploadSession", url)
        if m:
            u = f"https://up.test/{len(self.sessions)}"
            self.sessions[u] = {"name": urllib.parse.unquote(m.group(1)), "data": b""}
            return Resp(200, {}, json.dumps({"uploadUrl": u}).encode())
        if url.startswith(root + "/children"):
            items = [{"id": k, "name": v["name"], "size": len(v["data"])} for k, v in self.files.items()]
            # stránkování: po dvou
            skip = int(_q(url).get("skip", 0))
            page = items[skip:skip + 2]
            nxt = f"{root}/children?skip={skip + 2}" if skip + 2 < len(items) else None
            return Resp(200, {}, json.dumps({"value": page, **({"@odata.nextLink": nxt} if nxt else {})}).encode())
        if url.startswith(root):
            return Resp(200, {}, b'{"name": "MikroTik Controller"}')
        m = re.match(re.escape(graph) + r"/me/drive/items/(.+)", url)
        if m and method == "DELETE":
            self.files.pop(m.group(1), None)
            return Resp(204, {}, b"")
        return Resp(400, {}, url.encode())


def _backups(d: Path, n: int, size: int = 3 * 1024 * 1024 + 123) -> list[Path]:
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = d / f"mtc-vm-202610{10 + i:02d}-023000.mtcbak"
        p.write_bytes(os.urandom(size))
        out.append(p)
    return out


def _env():
    from test_modules import _env as env
    return env()


def test_plan_validation_and_due():
    p = backupplan.validate({"time": "03:15", "days": [7, 1, 1], "keep": "30"})
    assert p == {"enabled": True, "time": "03:15", "days": [1, 7], "keep": 30}
    for bad in ({"time": "25:00"}, {"days": []}, {"days": [8]}, {"keep": 0}, {"time": "3:15"}):
        try:
            backupplan.validate(bad)
            raise AssertionError(bad)
        except ServiceError:
            pass
    tz = dt.timezone(dt.timedelta(hours=2))
    mon = dt.datetime(2026, 10, 19, 3, 20, tzinfo=tz)  # pondělí po 03:15
    assert backupplan.due(p, mon, last_backup=None)
    assert not backupplan.due(p, mon.replace(hour=3, minute=0), None)  # ještě nebyl čas
    assert not backupplan.due(p, mon, last_backup=mon.replace(minute=16))  # už hotovo (i ručně)
    assert backupplan.due(p, mon.replace(hour=22), last_backup=mon - dt.timedelta(days=1))  # dohnat týž den
    assert not backupplan.due(p, mon + dt.timedelta(days=1), None)  # úterý není v plánu
    err = mon.replace(minute=17)
    assert not backupplan.due(p, mon.replace(minute=40), None, last_error_at=err)  # po chybě hodinu pauza
    assert backupplan.due(p, mon.replace(hour=4, minute=30), None, last_error_at=err)
    assert not backupplan.due({**p, "enabled": False}, mon, None)
    assert backupplan.max_gap_hours(p) == 6 * 24  # neděle → pondělí 1 den, pondělí → neděle 6 dní
    assert backupplan.max_gap_hours(backupplan.defaults()) == 24


def test_nextcloud_upload_chunked_and_retention():
    db, name, settings, box, app, tid, sid = _env()
    try:
      with small_chunks():
        nc = FakeNextcloud()
        t = cloudbackup.create(db, box, {"kind": "nextcloud", "url": "https://cloud.test/", "user": "mtc",
                                         "password": "app-pass-1234", "folder": "Zálohy/MTC", "keep": 2})
        # heslo v DB jen šifrovaně
        with operator_scope(db) as tx:
            r = tx.fetchone("SELECT config, ciphertext FROM backup_target WHERE id = %(i)s", {"i": t})
        assert b"app-pass" not in bytes(r["ciphertext"]) and "password" not in r["config"]
        assert "cloud.test" in cloudbackup.check(db, box, t, req=nc)
        tmp = Path(tempfile.mkdtemp())
        files = _backups(tmp, 3)
        small = tmp / "mtc-vm-20261001-023000.mtcbak"
        small.write_bytes(b"x" * 100)
        assert cloudbackup.upload_one(db, box, _load(db, box, t), small, nc)["ok"]
        assert not any(u for u in nc.calls if "/uploads/" in u[1])  # malý soubor jedním PUT
        for f in files:
            r = cloudbackup.upload_one(db, box, _load(db, box, t), f, nc)
            assert r["ok"], r
        names = sorted(p.rsplit("/", 1)[1] for p in nc.files)
        assert names == [files[1].name, files[2].name]  # nechá 2 poslední
        assert nc.files["/files/mtc/Zálohy/MTC/" + files[2].name] == files[2].read_bytes()
        assert not nc.uploads  # rozpracované nahrávání uklizené
        st = _load(db, box, t).status
        assert st["last_file"] == files[2].name and st["last_error"] is None
        # špatné heslo → chyba se zapíše, další pokus až po odkladu
        cloudbackup.update(db, box, t, {"password": "spatne-heslo-999"})
        r = cloudbackup.upload_one(db, box, _load(db, box, t), files[2], nc)
        assert not r["ok"] and "heslo aplikace" in r["error"]
        st = _load(db, box, t).status
        assert st["attempts"] == 1 and st["next_try"] > time.time() + 600
        # úprava bez hesla heslo nemění
        cloudbackup.update(db, box, t, {"keep": 5})
        assert _load(db, box, t).secret["password"] == "spatne-heslo-999"
        with operator_scope(db) as tx:
            acts = [x["action"] for x in tx.fetchall("SELECT action FROM audit_log WHERE target LIKE 'backup_target:%'")]
        assert {"system.backup_target_create", "system.backup_upload", "system.backup_upload_failed",
                "system.backup_target_update"} <= set(acts)
    finally:
        drop_db(name)


class small_chunks:
    """1280 KiB = násobek 256 KiB (Google) i 320 KiB (OneDrive) – víc částí i u malého souboru."""

    def __enter__(self):
        self.old = cloudbackup.CHUNK, cloudbackup.SMALL
        cloudbackup.CHUNK = cloudbackup.SMALL = 1280 * 1024

    def __exit__(self, *a):
        cloudbackup.CHUNK, cloudbackup.SMALL = self.old


def _load(db, box, t):
    with operator_scope(db) as tx:
        return cloudbackup.load(tx, box, t)


def test_validation():
    for kind, data, err in (
            ("nextcloud", {"url": "http://cloud.test", "user": "a", "password": "x" * 10}, "https"),
            ("nextcloud", {"url": "https://cloud.test", "user": "a", "password": "x" * 10, "folder": "../etc"}, "složka"),
            ("gdrive", {"client_id": "abc", "client_secret": "s" * 10}, "Client ID"),
            ("onedrive", {"client_id": "nope"}, "GUID"),
            ("onedrive", {"client_id": "0b1c2d3e-0000-4000-8000-00000000abcd", "tenant": "x/../y"}, "tenant")):
        try:
            cloudbackup.validate(kind, data)
            raise AssertionError((kind, data))
        except ServiceError as e:
            assert err in str(e), (err, str(e))


def test_google_drive_device_flow_upload_retention():
    db, name, settings, box, app, tid, sid = _env()
    try:
      with small_chunks():
        g = FakeGoogle()
        t = cloudbackup.create(db, box, {"kind": "gdrive", "client_id": "123-abc.apps.googleusercontent.com",
                                         "client_secret": "GOCSPX-secret", "keep": 2})
        assert cloudbackup.needs_auth(_load(db, box, t))
        dev = cloudbackup.device_start(db, box, t, req=g)
        assert dev["user_code"] == "ABCD-EFGH" and "google.com/device" in dev["url"]
        pub = [x for x in cloudbackup.listing(db) if x["id"] == t][0]
        assert pub["auth"]["user_code"] == "ABCD-EFGH" and "DEV-G" not in json.dumps(pub)
        # dřív než interval se API neptá
        assert cloudbackup.device_poll(db, box, t, req=g)["state"] == "pending" and g.polls == 0
        now = time.time() + 6
        assert cloudbackup.device_poll(db, box, t, req=g, now=now)["state"] == "pending" and g.polls == 1
        assert cloudbackup.device_poll(db, box, t, req=g, now=now + 6)["state"] == "done"
        tt = _load(db, box, t)
        assert tt.secret["refresh_token"] == "rt-google" and "device_code" not in tt.secret and tt.status["authorized"]
        assert "it@firma.cz" in cloudbackup.check(db, box, t, req=g)
        tmp = Path(tempfile.mkdtemp())
        files = _backups(tmp, 3)
        for f in files:
            assert cloudbackup.upload_one(db, box, _load(db, box, t), f, g)["ok"]
        stored = {v["name"]: v["data"] for v in g.files.values() if not v.get("folder")}
        assert sorted(stored) == [files[1].name, files[2].name] and stored[files[2].name] == files[2].read_bytes()
        folder = [k for k, v in g.files.items() if v.get("folder")]
        assert len(folder) == 1 and _load(db, box, t).config["folder_id"] == folder[0]  # složka jen jednou
        # odvolaný přístup → cíl se označí jako nepřihlášený
        g.refresh = "jiny"
        r = cloudbackup.upload_one(db, box, _load(db, box, t), files[2], g)
        assert not r["ok"] and "přihlas" in r["error"]
        assert not _load(db, box, t).status["authorized"] and cloudbackup.needs_auth(_load(db, box, t))
    finally:
        drop_db(name)


def test_onedrive_rotating_token_and_paging():
    db, name, settings, box, app, tid, sid = _env()
    try:
      with small_chunks():
        m = FakeOneDrive()
        t = cloudbackup.create(db, box, {"kind": "onedrive", "client_id": "0b1c2d3e-0000-4000-8000-00000000abcd",
                                         "keep": 3})
        cloudbackup.device_start(db, box, t, req=m)
        now = time.time() + 6
        r = cloudbackup.device_poll(db, box, t, req=m, now=now)
        assert r["state"] == "pending" and r["interval"] == 10  # slow_down → delší interval
        assert cloudbackup.device_poll(db, box, t, req=m, now=now + 11)["state"] == "done"
        tmp = Path(tempfile.mkdtemp())
        files = _backups(tmp, 5)
        for f in files:
            assert cloudbackup.upload_one(db, box, _load(db, box, t), f, m)["ok"]
        # každé obnovení vrátí nový refresh token – musí se uložit, jinak by další běh selhal
        assert _load(db, box, t).secret["refresh_token"] == m.refresh == "rt-ms-6"
        assert sorted(v["name"] for v in m.files.values()) == [f.name for f in files[2:]]
        assert "MikroTik Controller" in cloudbackup.check(db, box, t, req=m)
    finally:
        drop_db(name)


def test_worker_sync_pending_and_force():
    db, name, settings, box, app, tid, sid = _env()
    try:
        nc = FakeNextcloud()
        t = cloudbackup.create(db, box, {"kind": "nextcloud", "url": "https://cloud.test", "user": "mtc",
                                         "password": "app-pass-1234"})
        g = cloudbackup.create(db, box, {"kind": "gdrive", "client_id": "123-abc.apps.googleusercontent.com",
                                         "client_secret": "GOCSPX-secret"})
        tmp = Path(tempfile.mkdtemp())
        assert cloudbackup.sync(db, box, tmp, req=nc, background=False) == 0  # žádná záloha
        files = _backups(tmp, 2, size=1000)
        # Google není přihlášený → přeskočí; Nextcloud dostane nejnovější
        assert [x[0].id for x in cloudbackup.pending(db, box, tmp)] == [t]
        assert cloudbackup.sync(db, box, tmp, req=nc, background=False) == 1
        assert list(nc.files) == ["/files/mtc/MikroTik-Controller/" + files[1].name]
        assert cloudbackup.sync(db, box, tmp, req=nc, background=False) == 0  # už tam je
        cloudbackup.request_upload(db, t)
        assert cloudbackup.sync(db, box, tmp, req=nc, background=False) == 1  # „Odeslat teď“
        assert not _load(db, box, t).status["force"]
        from mtcontroller import doctor
        ch = {c.name: c for c in doctor.check_backup_targets(db)}
        assert ch["cíl Nextcloud"].status == doctor.OK and ch["cíl Google Drive"].status == doctor.FAIL
        assert g
    finally:
        drop_db(name)


def test_system_alerts_notify_once():
    db, name, settings, box, app, tid, sid = _env()
    try:
        tmp = Path(tempfile.mkdtemp())
        settings.paths.data_dir = tmp
        settings.backup.dir = tmp / "bk"
        from mtcontroller.config import NotifyTarget
        (tmp / "s").write_text("secret")
        settings.notify = [NotifyTarget(name="ops", kind="json", url="https://hook.test/x", secret_file=tmp / "s"),
                           NotifyTarget(name="zakaznik", kind="json", url="https://hook.test/y", secret_file=tmp / "s",
                                        tenants=[tid])]
        sent = []

        def post(url, body, headers):
            sent.append((url, json.loads(body)))
            return 200

        old = _backups(settings.backup.dir, 1, size=10)[0]
        t0 = time.time() - 3 * 86400
        os.utime(old, (t0, t0))
        act = sysalerts.reconcile(db, settings, post=post)
        assert "backup_stale" in act and len(sent) == 1 and sent[0][0] == "https://hook.test/x"  # jen cíl provozovatele
        assert "h stará" in sent[0][1]["text"]
        sysalerts.reconcile(db, settings, post=post)
        assert len(sent) == 1  # podruhé už ne
        os.utime(old, None)  # čerstvá záloha
        assert sysalerts.reconcile(db, settings, post=post) == {}
        assert len(sent) == 2 and sent[1][1]["resolved"]
        # cíl, který opakovaně selhává
        t = cloudbackup.create(db, box, {"kind": "nextcloud", "url": "https://cloud.test", "user": "mtc",
                                         "password": "app-pass-1234"})
        with operator_scope(db) as tx:
            cloudbackup.set_status(tx, t, last_error="HTTP 507", attempts=2)
        assert f"cloud_{t}" in sysalerts.reconcile(db, settings, post=post)
        backupplan.save(db, {"enabled": False})
        assert "backup_disabled" in sysalerts.read(settings) or "backup_disabled" in sysalerts.reconcile(db, settings, post=post)
    finally:
        drop_db(name)


def test_backup_endpoints_plan_targets():
    from test_flow import _login

    from mtcontroller.web.app import AppState, create_app

    db, name, settings, box, app, tid, sid = _env()
    try:
        tmp = Path(tempfile.mkdtemp())
        settings.paths.data_dir = tmp
        settings.backup.dir = tmp / "bk"
        c = _login(create_app(AppState(settings, db, box, [])))
        b = c.get("/api/admin/system/backups").json()
        assert b["plan"] == backupplan.defaults(14) and b["targets"] == []
        r = c.put("/api/admin/system/backup-plan", json_body={"enabled": True, "time": "01:05", "days": [1, 3, 5], "keep": 7})
        assert r.status_code == 200 and r.json()["plan"]["days"] == [1, 3, 5]
        assert c.put("/api/admin/system/backup-plan", json_body={"time": "x"}).status_code == 400
        assert c.get("/api/admin/system/backups").json()["keep"] == 7
        r = c.post("/api/admin/system/backup-targets", json_body={"kind": "nextcloud", "url": "https://cloud.test",
                                                                   "user": "mtc", "password": "app-pass-1234"})
        assert r.status_code == 200, r.text
        t = r.json()["id"]
        lst = c.get("/api/admin/system/backups").json()["targets"]
        assert lst[0]["id"] == t and "password" not in json.dumps(lst) and "app-pass" not in json.dumps(lst)
        assert c.patch(f"/api/admin/system/backup-targets/{t}", json_body={"enabled": False}).status_code == 200
        assert c.post(f"/api/admin/system/backup-targets/{t}/upload", json_body={}).status_code == 200
        assert c.post(f"/api/admin/system/backup-targets/{t}/nesmysl", json_body={}).status_code == 404
        assert c.post(f"/api/admin/system/backup-targets/{t}/auth", json_body={}).status_code == 400  # Nextcloud heslem
        assert c.delete(f"/api/admin/system/backup-targets/{t}").status_code == 200
        r = c.post(f"/api/admin/tenants/{tid}/users", json_body={"email": "t@firma.cz", "role": "tenant_admin"})
        u = _login(create_app(AppState(settings, db, box, [])), "t@firma.cz", r.json()["temp_password"])
        assert u.post("/api/admin/system/backup-targets", json_body={"kind": "nextcloud"}).status_code == 404
        assert u.put("/api/admin/system/backup-plan", json_body={}).status_code == 404
        with operator_scope(db) as tx:
            acts = {x["action"] for x in tx.fetchall("SELECT action FROM audit_log")}
        assert {"system.backup_plan", "system.backup_target_create", "system.backup_target_delete",
                "system.backup_target_upload_request"} <= acts
    finally:
        drop_db(name)


def test_http_client_keeps_308_and_errors():
    """Skutečný urllib: 308 (Google „pokračuj“) se nesmí brát jako přesměrování, 4xx vrací tělo."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_PUT(self):
            n = int(self.headers["Content-Length"])
            self.rfile.read(n)
            code = 308 if self.path == "/part" else 403
            self.send_response(code)
            if code == 308:
                self.send_header("Range", f"bytes=0-{n - 1}")
            body = b"" if code == 308 else b'{"error": {"message": "storageQuotaExceeded"}}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        r = cloudbackup.http("PUT", base + "/part", {"Content-Range": "bytes 0-3/8"}, b"abcd")
        assert r.status == 308 and r.headers["range"] == "bytes=0-3"
        r = cloudbackup.http("PUT", base + "/full", {}, b"abcd")
        assert r.status == 403 and "storageQuotaExceeded" in str(cloudbackup._err(r, "x"))
        try:
            cloudbackup.http("GET", "http://cloud.test/")
            raise AssertionError("http prošlo")
        except cloudbackup.CloudError as e:
            assert "HTTPS" in str(e)
    finally:
        srv.shutdown()
