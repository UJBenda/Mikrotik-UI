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
