"""Kontrola zabezpečení lokality: nová pravidla auditu, nálezy k verzi a zálohám, skóre."""

from helpers import drop_db
from test_flow import _login
from test_hardening import DEFCONF
from test_modules import _adopted, _env

from mtcontroller import hardening, inventory, seccheck
from mtcontroller.db import operator_scope


def test_new_hardening_rules():
    inv = dict(DEFCONF, upnp={"enabled": "true"}, smb={"enabled": "yes"},
               ssh={"strong-crypto": "false", "forwarding-enabled": "both"})
    codes = {f["code"]: f["severity"] for f in hardening.audit(inv)}
    assert codes["upnp_enabled"] == "warning" and codes["smb_enabled"] == "warning"
    assert codes["ssh_weak_crypto"] == "info" and codes["ssh_forwarding"] == "info"
    quiet = dict(DEFCONF, upnp={"enabled": "false"}, smb={"enabled": "no"},
                 ssh={"strong-crypto": "true", "forwarding-enabled": "no"})
    assert not {"upnp_enabled", "smb_enabled", "ssh_weak_crypto", "ssh_forwarding"} & {f["code"] for f in hardening.audit(quiet)}
    # zařízení bez těchto menu (sekce chybí) a s vypnutým SSH: žádný nález
    off = dict(DEFCONF, ssh={"strong-crypto": "false"},
               ip_services=[{"name": "ssh", "disabled": "true"}, {"name": "www-ssl", "disabled": "false", "address": "10.0.0.1/32"}])
    assert "ssh_weak_crypto" not in {f["code"] for f in hardening.audit(off)}


def test_version_findings_and_score():
    f = seccheck.version_findings("7.8 (stable)", {"latest-version": "7.20.4", "channel": "stable"})
    codes = {x["code"]: x["severity"] for x in f}
    assert codes["ros_unsupported"] == "warning" and codes["ros_outdated"] == "warning"
    near = seccheck.version_findings("7.20.2 (stable)", {"latest-version": "7.20.4", "channel": "stable"})
    assert [x["severity"] for x in near if x["code"] == "ros_outdated"] == ["info"]
    assert not [x for x in seccheck.version_findings("7.20.4", {"latest-version": "7.20.4"}) if x["code"] == "ros_outdated"]
    assert seccheck.version_findings("", {}) == []
    assert seccheck.score([]) == 100 and seccheck.level(100) == "ok" and seccheck.level(None) == "none"
    assert seccheck.score([{"severity": "warning"}, {"severity": "info"}]) == 88
    assert seccheck.score([{"severity": "critical"}]) == 40                 # kritický nález: nejvýš 40
    assert seccheck.score([{"severity": "warning"}] * 20) == 0
    assert seccheck.level(84) == "warning" and seccheck.level(59) == "critical"
    assert seccheck.category("open_resolver") == "exposure" and seccheck.category("service_telnet") == "access"


def test_site_security_endpoint():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/sites/{sid}/security"
        r = c.get(url).json()                                   # bez inventury: nic nevíme → bez skóre
        assert r["score"] is None and r["level"] == "none" and r["unchecked"] == 2
        assert all(d["score"] is None and not d["checked"] for d in r["devices"])

        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET role = 'gateway', ros_version = '7.20.2 (stable)' WHERE id = %(d)s", {"d": gw})
            tx.execute("UPDATE device SET ros_version = '7.20.4 (stable)', backup_enabled = false WHERE id = %(d)s", {"d": sw})
        # brána: výchozí konfigurace + otevřená z internetu (input nic nezahazuje) + UPnP
        open_gw = dict(DEFCONF, upnp={"enabled": "true"}, ntp_client={"enabled": "false"},
                       update={"latest-version": "7.20.4", "channel": "stable"},
                       fw_filter=[{"chain": "input", "action": "accept", "protocol": "icmp"}])
        inventory.store(db, tid, gw, open_gw, [])
        clean = {"ip_services": [{"name": "www-ssl", "disabled": "false", "address": "10.200.0.1/32"}],
                 "users": [{"name": "spravce", "group": "full"}], "ntp_client": {"enabled": "true", "status": "synchronized"},
                 "resource": {"uptime": "2d"}, "update": {"latest-version": "7.20.4", "channel": "stable"}}
        inventory.store(db, tid, sw, clean, [])
        r = c.get(url).json()
        g, s = r["devices"]
        assert g["id"] == gw and g["role"] == "gateway"            # brána první
        codes = {f["code"]: f for f in g["findings"]}
        assert codes["no_input_firewall"]["severity"] == "critical" and codes["no_input_firewall"]["category"] == "exposure"
        assert codes["upnp_enabled"]["category"] == "exposure" and codes["service_telnet"]["category"] == "access"
        assert codes["service_telnet"]["fixable"] and not codes["upnp_enabled"]["fixable"]
        assert codes["ros_outdated"]["severity"] == "info" and codes["ntp_off"]["category"] == "time"
        assert codes["backup_none"]["category"] == "backup"
        assert g["findings"][0]["severity"] == "critical" and g["score"] <= 40 and g["level"] == "critical"
        assert {f["code"] for f in s["findings"]} == {"backup_off"} and s["score"] == 90 and s["level"] == "ok"
        assert r["counts"]["critical"] >= 1 and r["score"] <= 49 and r["level"] == "critical" and r["unchecked"] == 0
        assert r["categories"]["backup"]["warning"] == 2 and r["categories"]["exposure"]["critical"] >= 1
        # účet bez dvoufázového ověření je nález lokality (controller)
        mfa = [f for f in r["site"] if f["code"] == "mfa_missing"]
        if mfa:
            assert mfa[0]["category"] == "controller" and mfa[0]["users"]
        with operator_scope(db) as tx:
            n = tx.fetchone("""SELECT count(*) AS n FROM role_binding rb JOIN app_user u ON u.id = rb.user_id
                               WHERE rb.tenant_id = %(t)s AND rb.site_id IS NULL AND u.is_active AND NOT u.totp_confirmed""",
                            {"t": tid})["n"]
        assert bool(mfa) == bool(n)
        assert c.get(f"/api/admin/tenants/{tid}/sites/00000000-0000-0000-0000-000000000000/security").status_code == 404
    finally:
        drop_db(name)
