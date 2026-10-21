"""Stav systému: heartbeat workeru, endpoint pro superadmina, kontrola v doctor."""

import json
import tempfile
import threading
import time
from pathlib import Path

from helpers import drop_db
from test_flow import _login
from test_modules import _env

from mtcontroller import doctor, heartbeat
from mtcontroller.web.app import AppState, create_app
from mtcontroller.worker import __main__ as worker


def test_worker_writes_heartbeat_and_system_endpoint():
    db, name, settings, box, app, tid, sid = _env()
    try:
        tmp = Path(tempfile.mkdtemp())
        settings.paths.data_dir = tmp
        stop = threading.Event()
        th = threading.Thread(target=worker.run, args=(settings, db, box, [], stop), daemon=True)
        th.start()
        for _ in range(200):
            if (tmp / heartbeat.FILE).exists():
                break
            time.sleep(0.1)
        stop.set()
        th.join(10)
        st = heartbeat.read(tmp)
        assert st and st["tick"] >= 1 and not st["stale"]
        assert {"offline", "inventory", "alerts", "upgrades"} <= set(st["steps"])
        assert all(v["error"] is None for v in st["steps"].values()), st["steps"]

        app2 = create_app(AppState(settings, db, box, []))
        c = _login(app2)
        s = c.get("/api/admin/system").json()
        assert s["worker"]["tick"] >= 1 and s["tenants"] == 1 and s["db_bytes"] > 0
        # ne-superadmin systém nevidí
        r = c.post(f"/api/admin/tenants/{tid}/users", json_body={"email": "t@firma.cz", "role": "tenant_admin"})
        t = _login(app2, "t@firma.cz", r.json()["temp_password"])
        assert t.get("/api/admin/system").status_code == 404
    finally:
        drop_db(name)


def test_doctor_worker_check(tmp_path):
    from helpers import make_settings

    s = make_settings(tmp_path)
    s.paths.data_dir = tmp_path
    assert doctor.check_worker(s)[0].status == doctor.WARN  # ještě nezapsal
    heartbeat.write(tmp_path, {"at_epoch": time.time(), "tick": 5, "steps": {"inventory": {"ms": 3, "error": None}}})
    assert doctor.check_worker(s)[0].status == doctor.OK
    heartbeat.write(tmp_path, {"at_epoch": time.time(), "tick": 6, "steps": {"backup": {"ms": 3, "error": "Timeout"}}})
    assert doctor.check_worker(s)[0].status == doctor.WARN
    (tmp_path / heartbeat.FILE).write_text(json.dumps({"at_epoch": time.time() - 600, "tick": 7}))
    assert doctor.check_worker(s)[0].status == doctor.FAIL


def test_backup_endpoints_and_regional_defaults():
    """Zálohy controlleru v UI (jen superadmin) a výchozí region z config.toml v /me."""
    from mtcontroller import regional, sysbackup
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        tmp = Path(tempfile.mkdtemp())
        settings.paths.data_dir = tmp
        settings.backup.dir = tmp / "bk"
        settings.backup.dir.mkdir()
        settings.site_defaults.country = "Slovakia"
        settings.site_defaults.timezone = "Europe/Bratislava"
        good = "mtc-vm-20261021-023000.mtcbak"
        (settings.backup.dir / good).write_bytes(sysbackup.MAGIC + b"x")
        (settings.backup.dir / "jiny.txt").write_text("ne")
        app2 = create_app(AppState(settings, db, box, []))
        assert regional.country() == "Slovakia" and regional.timezone() == "Europe/Bratislava"
        c = _login(app2)
        assert c.get("/api/admin/me").json()["defaults"]["country"] == "Slovakia"
        b = c.get("/api/admin/system/backups").json()
        assert [x["name"] for x in b["backups"]] == [good] and b["keep"] == 14 and not b["pending"]
        r = c.get("/api/admin/system/backups/" + good)
        assert r.status_code == 200 and r.content == sysbackup.MAGIC + b"x"
        assert c.get("/api/admin/system/backups/jiny.txt").status_code == 404
        assert c.get("/api/admin/system/backups/..%2Fjiny.txt").status_code == 404
        assert c.post("/api/admin/system/backups", json_body={}).status_code == 200
        assert json.loads(sysbackup.request_path(tmp).read_text())["user"] == "admin@example.cz"
        assert c.post("/api/admin/system/backups", json_body={}).status_code == 409
        assert c.get("/api/admin/system/backups").json()["pending"] is True
        with operator_scope(db) as tx:
            acts = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log")]
        assert "system.backup_request" in acts and "system.backup_download" in acts
        # výchozí země pro Wi-Fi politiku lokality
        from mtcontroller import intent
        assert intent.validate_wifi({})["country"] == "Slovakia"
    finally:
        from mtcontroller.config import SiteDefaults
        regional._current = SiteDefaults()
        drop_db(name)
