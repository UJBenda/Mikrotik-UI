"""Zálohy zařízení: výběr zařízení, pravidla zákazníka (interval / podle controlleru), retence verzí, API."""
import datetime as dt


def _exp(name):
    return f"# 2026-11-24 by RouterOS 7.24\n/system identity set name={name}\n/user add name=x password=tajne-{name}\n"


class ExportApi:
    """REST zařízení, které vrací export přes vlastnost contents."""

    def __init__(self, text, fail=False):
        self.text, self.fail, self.timeout = text, fail, 10
        self.made = False

    def get(self, path, **params):
        if self.fail:
            from mtcontroller.rosapi import RosApiError
            raise RosApiError(None, "timeout")
        return [{".id": "*1", "name": "mtc-backup.rsc", "size": str(len(self.text.encode())), "contents": self.text}] if self.made else []

    def post(self, path, body):
        from mtcontroller.rosapi import RosApiError
        if path == "/export":
            self.made = True
            return []
        raise RosApiError(400, "no such command")

    def delete(self, path):
        self.made = False

    def close(self):
        pass


def test_policy_and_cutoff():
    from mtcontroller import devbackup as d
    from mtcontroller.services import ServiceError

    p = d.validate({"mode": "custom", "every_hours": "12", "keep": 14, "monthly": 12, "only_changes": 1})
    assert p == {"mode": "custom", "every_hours": 12, "only_changes": True, "keep": 14, "weekly": 0, "monthly": 12}
    for bad in ({"mode": "x"}, {"every_hours": 0}, {"every_hours": 721}, {"keep": 0}, {"monthly": 200}):
        try:
            d.validate(bad)
            raise AssertionError(bad)
        except ServiceError:
            pass
    plan = {"enabled": True, "time": "02:30", "days": [1, 3, 5], "keep": 14, "weekly": 0, "monthly": 12}
    wed = dt.datetime(2026, 11, 25, 9, 0)                                   # středa dopoledne
    assert d.last_slot(plan, wed) == dt.datetime(2026, 11, 25, 2, 30)
    assert d.last_slot(plan, wed.replace(hour=1)) == dt.datetime(2026, 11, 23, 2, 30)   # ještě nebyl čas → pondělí
    assert d.last_slot({**plan, "enabled": False}, wed) is None
    assert d.cutoff({**p, "mode": "follow"}, plan, wed) == dt.datetime(2026, 11, 25, 2, 30)
    assert d.cutoff({**p, "mode": "follow"}, {**plan, "enabled": False}, wed) == wed - dt.timedelta(hours=24)
    assert d.cutoff(p, plan, wed) == wed - dt.timedelta(hours=12)
    eff = d.effective({**p, "mode": "follow", "keep": 99, "monthly": 0}, plan)
    assert (eff["keep"], eff["monthly"]) == (14, 12) and d.effective(p, plan) == p


def test_worker_selection_retention_and_api(monkeypatch):
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import backup, devbackup, services
    from mtcontroller.db import operator_scope, tenant_scope
    from mtcontroller.worker import __main__ as worker

    db, name, settings, box, app, tid, sid = _env()
    try:
        a = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        b = _adopted(db, box, tid, sid, "AP1", "10.200.1.3", "ap-sklad")
        state = {"a": "v1", "fail_b": False}
        monkeypatch.setattr(services, "device_api",
                            lambda tx, box_, dev, timeout=10.0: ExportApi(_exp(state["a"])) if str(dev["id"]) == a
                            else ExportApi(_exp("ap"), fail=state["fail_b"]))
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/device-backups"
        r = c.get(url + f"?site={sid}").json()
        assert r["policy"]["mode"] == "custom" and r["policy"]["keep"] == 365           # výchozí = dosavadní chování
        assert [x["name"] for x in r["devices"]] == ["ap-sklad", "sw-hala"] and all(x["enabled"] and x["versions"] == 0 for x in r["devices"])
        # AP ze zálohování vyřadit, pravidla: jen změny, 2 poslední + 2 měsíční
        r = c.post(url, json_body={"device_ids": [b], "enabled": False}).json()
        assert [x["enabled"] for x in r["devices"]] == [False, True]
        r = c.put(url, json_body={"mode": "custom", "every_hours": 6, "only_changes": True, "keep": 2, "monthly": 2}).json()
        assert r["policy"]["every_hours"] == 6 and r["effective"]["keep"] == 2
        assert c.put(url, json_body={"every_hours": 0}).status_code == 400
        assert worker.run_backups(db, box) == 1                                         # jen vybrané zařízení
        assert worker.run_backups(db, box) == 0                                         # interval ještě neuplynul
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM device_backup")["n"] == 1
            tx.execute("UPDATE device SET backup_at = now() - interval '7 hours'")
        assert worker.run_backups(db, box) == 1                                         # beze změny → nová verze nevznikne
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM device_backup")["n"] == 1
            assert tx.fetchone("SELECT backup_at > now() - interval '1 minute' AS ok FROM device WHERE id = %(d)s", {"d": a})["ok"]
            # starší verze: 3 měsíce zpět po dvou, ať je co promazávat
            for i, days in enumerate((95, 80, 65, 50, 35, 20)):
                sealed = box.seal(_exp(f"old{i}").encode(), backup._ctx(a))
                tx.execute("""INSERT INTO device_backup (tenant_id, device_id, taken_at, size_bytes, sha256, content_sha, changed,
                                  wrapped_key, ciphertext, key_version)
                              VALUES (%(t)s, %(d)s, now() - make_interval(days => %(n)s), 10, %(s)s, %(s)s, true, %(w)s, %(c)s, %(k)s)""",
                           {"t": tid, "d": a, "n": days, "s": f"{i}" * 64, "w": sealed.wrapped_key, "c": sealed.ciphertext,
                            "k": sealed.key_version})
            tx.execute("UPDATE device SET backup_at = now() - interval '7 hours'")
        state["a"] = "v2"
        assert worker.run_backups(db, box) == 1                                         # změna → nová verze + promazání
        with tenant_scope(db, tid) as tx:
            vs = devbackup.versions(tx, a)
            assert vs[0]["latest"] and vs[0]["changed"] and len(vs) <= 2 + 2 and len(vs) >= 3
        r = c.get(f"/api/admin/tenants/{tid}/devices/{a}/backups").json()["versions"]
        assert len(r) == len(vs) and r[0]["id"] == vs[0]["id"]
        # porovnání starší verze s aktuální: co by návrat změnil (hesla jsou zamaskovaná)
        old = r[1]["id"]
        dfr = c.get(f"/api/admin/tenants/{tid}/devices/{a}/backups/{old}/diff?against=latest").json()
        assert "--- aktuální" in dfr["diff"] and "+++ vybraná verze" in dfr["diff"] and "-/system identity set name=v2" in dfr["diff"]
        assert "tajne" not in dfr["diff"] and dfr["latest_id"] == r[0]["id"]
        assert c.get(f"/api/admin/tenants/{tid}/devices/{a}/backups/{r[0]['id']}/diff?against=latest").json()["diff"] == ""
        # chyba zálohy: zapíše se k zařízení a další pokus je až za hodinu
        c.post(url, json_body={"device_ids": [b], "enabled": True})
        state["fail_b"] = True
        assert worker.run_backups(db, box) == 0
        ov = {x["name"]: x for x in c.get(url).json()["devices"]}
        assert "timeout" in ov["ap-sklad"]["error"] and ov["sw-hala"]["error"] is None and ov["sw-hala"]["changes"] >= 1
        state["fail_b"] = False
        assert worker.run_backups(db, box) == 0                                         # čeká se hodinu
        assert c.post(f"/api/admin/tenants/{tid}/devices/{b}/backup", json_body={}).status_code == 200   # „Zálohovat teď“
        assert worker.run_backups(db, box) == 1
        assert {x["name"]: x for x in c.get(url).json()["devices"]}["ap-sklad"]["error"] is None
        # režim „podle zálohy controlleru“ přebírá jeho retenci
        r = c.put(url, json_body={"mode": "follow"}).json()
        assert r["policy"]["mode"] == "follow" and r["effective"]["keep"] == r["plan"]["keep"]
        with operator_scope(db) as tx:
            acts = {x["action"] for x in tx.fetchall("SELECT action FROM audit_log")}
        assert {"tenant.device_backup_policy", "device.backup_disabled", "device.backup_enabled"} <= acts
    finally:
        drop_db(name)
