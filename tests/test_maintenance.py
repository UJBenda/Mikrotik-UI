"""Servisní okna lokality: během okna se notifikace nových alertů neposílají."""

import datetime as dt
import json
import tempfile
from pathlib import Path

import pytest
from helpers import drop_db
from test_alerts import _talk_target
from test_flow import _login
from test_modules import _adopted, _env

from mtcontroller import alerts, maintenance, notify, services
from mtcontroller.config import Settings
from mtcontroller.db import operator_scope, tenant_scope


def test_window_holds_notifications():
    db, name, settings, box, app, tid, sid = _env()
    try:
        a = _adopted(db, box, tid, sid, "A1", "10.200.1.1", "gw")
        b = _adopted(db, box, tid, sid, "B1", "10.200.1.2", "sw")
        st = Settings.model_validate({**settings.model_dump(), "notify": [_talk_target(Path(tempfile.mkdtemp())).model_dump()]})
        out = []
        post = lambda u, body, h: out.append(json.loads(body)["message"])  # noqa: E731
        now = dt.datetime.now(dt.timezone.utc)

        s = maintenance.add(db, tid, sid, {"minutes": 60, "note": "výměna switche"})
        assert s["active"]["note"] == "výměna switche" and len(s["windows"]) == 1
        with operator_scope(db) as tx:
            assert maintenance.quiet_sites(tx) == {str(sid)}
            tx.execute("UPDATE device SET state = 'offline' WHERE id IN (%(a)s, %(b)s)", {"a": a, "b": b})
        alerts.reconcile(db)
        assert notify.deliver(db, st, post=post) == 0 and out == []        # drží se
        # gw se během okna vrátí → o něm se nepošle nic (vznik ani vyřešení)
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'adopted' WHERE id = %(a)s", {"a": a})
        alerts.reconcile(db)
        assert notify.deliver(db, st, post=post) == 0 and out == []
        # okno skončí, sw je pořád offline → teď se pošle
        s = maintenance.end(db, tid, sid, s["active"]["id"])
        assert s["active"] is None and s["windows"] == []
        assert notify.deliver(db, st, post=post) == 1 and len(out) == 1 and "sw" in out[0]
        with operator_scope(db) as tx:
            acts = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log ORDER BY id")]
        assert "site.maintenance_add" in acts and "site.maintenance_end" in acts

        # naplánované okno zatím neplatí; platnost podle času
        s = maintenance.add(db, tid, sid, {"from": (now + dt.timedelta(hours=2)).isoformat(),
                                          "to": (now + dt.timedelta(hours=3)).isoformat()})
        assert s["active"] is None and s["windows"][0]["active"] is False
        with operator_scope(db) as tx:
            assert maintenance.quiet_sites(tx) == set()
            assert maintenance.quiet_sites(tx, now + dt.timedelta(hours=2, minutes=30)) == {str(sid)}
            assert maintenance.quiet_sites(tx, now + dt.timedelta(hours=4)) == set()
        # běžící postupná aktualizace = servisní okno
        with operator_scope(db) as tx:
            tx.execute("INSERT INTO upgrade_job (tenant_id, site_id, steps) VALUES (%(t)s, %(s)s, '[]')", {"t": tid, "s": sid})
            assert maintenance.quiet_sites(tx) == {str(sid)}
        with tenant_scope(db, tid) as tx:
            assert maintenance.status(tx, sid)["upgrade"] is True
        for bad in ({"minutes": 2}, {"minutes": "x"}, {}, {"minutes": 60 * 24 * 8},
                    {"from": now.isoformat(), "to": (now - dt.timedelta(hours=1)).isoformat()}):
            with pytest.raises(services.ServiceError):
                maintenance.add(db, tid, sid, bad)
    finally:
        drop_db(name)


def test_api():
    db, name, settings, box, app, tid, sid = _env()
    try:
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/sites/{sid}/maintenance"
        assert c.get(base).json() == {"active": None, "windows": [], "upgrade": False}
        r = c.post(base, json_body={"minutes": 30, "note": "test"})
        assert r.status_code == 201 and r.json()["active"]["note"] == "test", r.text
        wid = r.json()["active"]["id"]
        assert c.post(base, json_body={"minutes": 1}).status_code == 400
        assert c.delete(f"{base}/nic").status_code == 404
        r = c.delete(f"{base}/{wid}")
        assert r.status_code == 200 and r.json()["active"] is None
    finally:
        drop_db(name)
