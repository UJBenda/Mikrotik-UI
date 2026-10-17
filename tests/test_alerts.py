import hashlib
import hmac
import json
import tempfile
from pathlib import Path

from helpers import AsgiClient, drop_db
from test_flow import _login
from test_modules import _adopted, _env

from mtcontroller import alerts, backup, inventory, notify, services
from mtcontroller.config import NotifyTarget, Settings
from mtcontroller.db import operator_scope, tenant_scope


def _kinds(db):
    with operator_scope(db) as tx:
        return {(r["kind"], r["subject"]) for r in tx.fetchall("SELECT kind, subject FROM alert WHERE resolved_at IS NULL")}


def test_alert_lifecycle_and_api():
    db, name, settings, box, app, tid, sid = _env()
    try:
        a = _adopted(db, box, tid, sid, "A1", "10.200.1.1", "gw")
        b = _adopted(db, box, tid, sid, "B1", "10.200.1.2", "sw")
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'offline' WHERE id = %(d)s", {"d": b})
            tx.execute("""UPDATE device SET last_status = '{"sstp": "down", "sstp_down": 4}', backup_at = now(),
                          adopted_at = now() WHERE id = %(d)s""", {"d": a})
            tx.execute(
                """INSERT INTO interface_hourly (tenant_id, device_id, hour, name, link_downs, rx_errors, samples)
                   VALUES (%(t)s, %(d)s, date_trunc('hour', now()), 'ether3', 7, 500, 12)""", {"t": tid, "d": a})
        inventory.store(db, tid, a, {"resource": {"cpu-load": "97", "free-memory": "50", "total-memory": "1000",
                                                  "version": "7.24.2 (stable)"}}, [])
        backup.store(db, box, tid, a, "# x by RouterOS 7.24\n/system identity set name=a\n")
        backup.store(db, box, tid, a, "# y by RouterOS 7.24\n/system identity set name=hacked\n")

        opened, closed = alerts.reconcile(db)
        k = _kinds(db)
        assert ("offline", "") in k and ("tunnel_down", "") in k and ("cpu_high", "") in k
        assert ("memory_high", "") in k and ("port_flapping", "ether3") in k and ("port_errors", "ether3") in k
        assert ("config_drift", "") in k and ("ros_issue", "7.24") in k
        assert len(opened) == len(k) and not closed

        # druhé vyhodnocení nic nového neotevře
        opened2, closed2 = alerts.reconcile(db)
        assert not opened2 and not closed2

        # podmínka zmizí → alert se uzavře
        inventory.store(db, tid, a, {"resource": {"cpu-load": "5", "free-memory": "900", "total-memory": "1000",
                                                  "version": "7.24.5 (stable)"}}, [])
        _, closed3 = alerts.reconcile(db)
        k = _kinds(db)
        assert ("cpu_high", "") not in k and ("memory_high", "") not in k and ("ros_issue", "7.24") not in k
        assert len(closed3) == 3

        # API: seznam a potvrzení; tenant B nic nevidí
        c = _login(app)
        lst = c.get(f"/api/admin/tenants/{tid}/alerts").json()
        assert lst[0]["severity"] == "critical" and lst[0]["kind"] == "offline"
        assert c.post(f"/api/admin/tenants/{tid}/alerts/{lst[0]['id']}/ack").status_code == 200
        assert c.post(f"/api/admin/tenants/{tid}/alerts/{lst[0]['id']}/ack").status_code == 404
        other = services.create_tenant(db, settings, "Jiná firma")
        assert c.get(f"/api/admin/tenants/{other['id']}/alerts").json() == []
        det = c.get(f"/api/admin/tenants/{tid}/devices/{a}").json()
        assert {x["kind"] for x in det["alerts"]} >= {"tunnel_down", "port_flapping"}
    finally:
        drop_db(name)


def _talk_target(tmp: Path) -> NotifyTarget:
    sec = tmp / "talk.secret"
    sec.write_text("bot-secret-1234567890\n")
    return NotifyTarget(name="talk", kind="nextcloud_talk",
                        url="https://cloud.test/ocs/v2.php/apps/spreed/api/v1/bot/abc/message",
                        secret_file=sec, min_severity="warning")


def test_talk_signature():
    t = _talk_target(Path(tempfile.mkdtemp()))
    sent = []
    notify.send(t, {"id": 1, "tenant_id": "x", "severity": "critical", "message": "gw: offline"}, False,
                post=lambda url, body, headers: sent.append((url, body, headers)))
    url, body, headers = sent[0]
    msg = json.loads(body)["message"]
    assert "gw: offline" in msg and headers["OCS-APIRequest"] == "true"
    expect = hmac.new(b"bot-secret-1234567890", (headers["X-Nextcloud-Talk-Bot-Random"] + msg).encode(),
                      hashlib.sha256).hexdigest()
    assert headers["X-Nextcloud-Talk-Bot-Signature"] == expect


def test_json_webhook_signature():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "s").write_text("tajne")
    t = NotifyTarget(name="w", kind="json", url="https://hook.test/x", secret_file=tmp / "s")
    sent = []
    notify.send(t, {"id": 2, "tenant_id": "x", "severity": "warning", "message": "m"}, True,
                post=lambda url, body, headers: sent.append((body, headers)))
    body, headers = sent[0]
    assert headers["X-MTC-Signature"] == "sha256=" + hmac.new(b"tajne", body, hashlib.sha256).hexdigest()
    assert json.loads(body)["resolved"] is True


def test_deliver_retries_and_filters():
    db, name, settings, box, app, tid, sid = _env()
    try:
        a = _adopted(db, box, tid, sid, "A1", "10.200.1.1", "gw")
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'offline' WHERE id = %(d)s", {"d": a})
        alerts.reconcile(db)
        st = Settings.model_validate({**settings.model_dump(), "notify": [_talk_target(Path(tempfile.mkdtemp())).model_dump()]})
        calls = []

        def fail(url, body, headers):
            calls.append(1)
            raise ConnectionError("down")

        assert notify.deliver(db, st, post=fail) == 0
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT notify_attempts FROM alert")["notify_attempts"] == 1
        ok = []
        assert notify.deliver(db, st, post=lambda u, b, h: ok.append(json.loads(b)["message"])) == 1
        assert notify.deliver(db, st, post=lambda u, b, h: ok.append("dup")) == 0  # už odesláno
        # uzavření → notifikace o vyřešení
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET state = 'adopted' WHERE id = %(d)s", {"d": a})
        alerts.reconcile(db)
        notify.deliver(db, st, post=lambda u, b, h: ok.append(json.loads(b)["message"]))
        assert ok[0].startswith("🔴") and ok[-1].startswith("✅")
    finally:
        drop_db(name)


def test_half_duplex_alert():
    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        inv = {"ethernet_monitor": [{"name": "ether5", "status": "link-ok", "rate": "100Mbps", "full-duplex": "false"},
                                    {"name": "ether6", "status": "no-link", "full-duplex": "false"},
                                    {"name": "ether7", "status": "link-ok", "rate": "1Gbps", "full-duplex": "true"}]}
        inventory.store(db, tid, sw, inv, [])
        with operator_scope(db) as tx:
            found = [a for a in alerts.conditions(tx) if a.kind == "port_half_duplex"]
        assert [a.subject for a in found] == ["ether5"] and "100Mbps" in found[0].message
    finally:
        drop_db(name)
