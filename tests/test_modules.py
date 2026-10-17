"""End-to-end: inventura, metriky, zálohy, NTP plány, detail a topologie."""

import copy
import re
import tempfile
from pathlib import Path

from fixtures import legacy_per_vlan_bridges, modern
from helpers import AsgiClient, FakeProvider, drop_db, fresh_db, make_settings, new_box
from test_flow import PASSWORD, _login

from mtcontroller import auth, backup, inventory, services
from mtcontroller.db import operator_scope, tenant_scope
from mtcontroller.web.app import AppState, create_app
from mtcontroller.worker import __main__ as worker


def _env():
    db, name = fresh_db()
    tmp = Path(tempfile.mkdtemp())
    settings = make_settings(tmp)
    box = new_box()
    app = create_app(AppState(settings, db, box, [FakeProvider()]))
    from mtcontroller.web import admin as _admin
    _admin._attempts.clear()
    with operator_scope(db) as tx:
        uid = auth.create_user(tx, "admin@example.cz", PASSWORD)
        tx.execute("INSERT INTO role_binding (user_id, role) VALUES (%(u)s, 'superadmin')", {"u": uid})
    t = services.create_tenant(db, settings, "Firma")
    s = services.create_site(db, t["id"], "Praha")
    return db, name, settings, box, app, str(t["id"]), str(s["id"])


def _adopted(db, box, tid, sid, serial, ip, identity):
    with tenant_scope(db, tid) as tx:
        d = tx.fetchone(
            """INSERT INTO device (tenant_id, site_id, serial, state, tunnel_ip, identity, ros_version,
                                   api_cert_fingerprint, checkin_key_hash, last_checkin_at)
               VALUES (%(t)s, %(s)s, %(se)s, 'adopted', %(ip)s, %(i)s, '7.20.1 (stable)', %(fp)s, %(k)s, now())
               RETURNING id""",
            {"t": tid, "s": sid, "se": serial, "ip": ip, "i": identity, "fp": "ab" * 32,
             "k": services.token_hash("key-" + serial)})
        services.put_secret(tx, box, tid, d["id"], "api_password", "pw-" + serial)
        services.put_secret(tx, box, tid, d["id"], "checkin_key", "key-" + serial)
    return str(d["id"])


def test_inventory_metrics_and_detail():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw")
        inv_gw, inv_sw = modern(), legacy_per_vlan_bridges()
        inv_gw["neighbors"] = [{"interface": "ether1", "mac-address": "AA:00:00:00:00:01", "identity": "sw"}]
        inv_gw["resource"] = {"version": "7.24.1 (stable)", "board-name": "RB5009UG+S+", "uptime": "1d",
                              "free-memory": "500", "total-memory": "1000"}

        # worker: inventura přes podvržené REST API
        from test_inventory_backup import FakeApi
        apis = {"10.200.1.1": inv_gw, "10.200.1.2": inv_sw}

        def fake_device_api(tx, box_, dev, timeout=10.0):
            inv = apis[dev["tunnel_ip"]]
            resp = {}
            for s in inventory.SECTIONS:  # více sekcí může číst stejnou cestu (bridge_ports/stp_ports)
                if s.name in inv or s.path not in resp:
                    resp[s.path] = inv.get(s.name, [])
            return FakeApi(resp)

        orig = services.device_api
        services.device_api = fake_device_api
        try:
            assert worker.poll_inventory(db, box) == 2
            assert worker.poll_inventory(db, box) == 0  # do 5 minut znovu ne
            # druhý vzorek o 5 minut později pro výpočet rychlosti
            with operator_scope(db) as tx:
                tx.execute("UPDATE interface_sample SET at = at - interval '5 minutes'")
                tx.execute("UPDATE device SET inventory_at = NULL")
            inv_gw["interfaces"][0]["rx-byte"] = str(1000 + 300 * 1000 * 1000 // 8)  # +1 Mbit/s * 300 s
            assert worker.poll_inventory(db, box) == 2
        finally:
            services.device_api = orig
        assert inventory.rollup(db) > 0
        with tenant_scope(db, tid) as tx:
            h = tx.fetchone("SELECT rx_bps_avg FROM interface_hourly WHERE device_id = %(d)s AND name = 'ether1'",
                            {"d": gw})
        assert 990_000 <= h["rx_bps_avg"] <= 1_010_000, h

        c = _login(app)
        r = c.get(f"/api/admin/tenants/{tid}/devices/{gw}")
        assert r.status_code == 200, r.text
        det = r.json()
        assert det["device"]["model"] == "RB5009UG+S+" and det["summary"]["memory_used_pct"] == 50
        assert det["vlan"]["status"] == "conforming"
        assert any(w["severity"] == "critical" for w in det["warnings"])  # 7.24.1
        assert det["modules"]["ntp"] == "observe"
        r = c.get(f"/api/admin/tenants/{tid}/devices/{sw}/vlan-migration")
        assert r.json()["analysis"]["status"] == "legacy" and r.json()["plan"]["ok"]
        topo = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/topology").json()
        assert any({e["a"], e["b"]} == {gw, sw} for e in topo["edges"])
        m = c.get(f"/api/admin/tenants/{tid}/devices/{gw}/metrics?interface=ether1").json()
        assert m and m[0]["name"] == "ether1"
    finally:
        drop_db(name)


def test_ntp_plan_flow():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        cl = _adopted(db, box, tid, sid, "AP1", "10.200.1.2", "ap")
        inv_gw = modern()
        inv_cl = copy.deepcopy(modern())
        inv_cl["addresses"] = [{"address": "10.0.10.30/24", "interface": "vlan10", "disabled": "false", "dynamic": "false"}]
        inventory.store(db, tid, gw, inv_gw, [])
        inventory.store(db, tid, cl, inv_cl, [])

        c = _login(app)
        base = f"/api/admin/tenants/{tid}"
        # politika: primární musí být z lokality
        r = c.post(f"{base}/sites/{sid}/policy/ntp", json_body={"primary_device_id": gw, "timezone": "Europe/Prague"})
        assert r.status_code == 200, r.text
        r = c.post(f"{base}/sites/{sid}/plans/ntp")
        res = {x["device_id"]: x for x in r.json()}
        assert res[gw]["result"] == "plan" and res[cl]["result"] == "plan"
        plans = c.get(f"{base}/plans").json()
        p_gw = next(p for p in plans if p["device_id"] == gw)
        p_cl = next(p for p in plans if p["device_id"] == cl)
        assert "ntp server set enabled=yes" in p_gw["script"]
        assert 'servers="10.0.10.2,tik.cesnet.cz"' in p_cl["script"]

        # režim observe → schválení odmítnuto
        r = c.post(f"{base}/plans/{p_gw['id']}/approve")
        assert r.status_code == 400 and "observe" in r.text
        assert c.post(f"{base}/devices/{gw}/modules", json_body={"module": "ntp", "mode": "manage"}).status_code == 200
        # VLAN zatím jen observe
        assert c.post(f"{base}/devices/{gw}/modules", json_body={"module": "vlan", "mode": "manage"}).status_code == 400
        r = c.post(f"{base}/plans/{p_gw['id']}/approve")
        assert r.status_code == 200, r.text

        # check-in doručí plán, ack ho označí jako aplikovaný
        dev = AsgiClient(app, "checkin.test")
        r = dev.post("/api/checkin", json_body={"sstp": "up"}, headers={"authorization": "Bearer key-GW1"})
        assert "/system ntp server set enabled=yes" in r.text
        cid = int(re.search(r"ack\\?\?id=(\d+)", r.text).group(1))
        dev.post(f"/api/checkin/ack?id={cid}&status=done", headers={"authorization": "Bearer key-GW1"})
        with tenant_scope(db, tid) as tx:
            st = tx.fetchone("SELECT status FROM config_plan WHERE id = %(i)s", {"i": p_gw["id"]})["status"]
            inv_at = tx.fetchone("SELECT inventory_at FROM device WHERE id = %(d)s", {"d": gw})["inventory_at"]
        assert st == "applied" and inv_at is None  # inventura se načte znovu

        # změna zařízení mezi vytvořením a schválením → plán propadne
        c.post(f"{base}/devices/{cl}/modules", json_body={"module": "ntp", "mode": "manage"})
        inv_cl["ntp_client"] = {"enabled": "true", "servers": "1.2.3.4"}
        inventory.store(db, tid, cl, inv_cl, [])
        r = c.post(f"{base}/plans/{p_cl['id']}/approve")
        assert r.status_code == 400 and "změnila" in r.text
        assert c.post(f"{base}/plans/{p_cl['id']}/approve").status_code == 400  # už není draft
    finally:
        drop_db(name)


def test_backups_store_diff_and_rbac():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        e1 = "# 2026-10-17 20:00:00 by RouterOS 7.20.1\n/system identity set name=gw\n/user add name=x password=tajne1\n"
        e2 = e1.replace("2026-10-17", "2026-10-18")  # jen hlavička → beze změny
        e3 = e2.replace("name=gw", "name=gw2")
        b1 = backup.store(db, box, tid, gw, e1)
        b2 = backup.store(db, box, tid, gw, e2)
        b3 = backup.store(db, box, tid, gw, e3)
        assert b1["changed"] and not b2["changed"] and b3["changed"]
        with operator_scope(db) as tx:
            raw = [bytes(r["ciphertext"]) for r in tx.fetchall("SELECT ciphertext FROM device_backup")]
            actions = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log")]
        assert all(b"tajne1" not in x for x in raw)  # zálohy jen šifrovaně
        assert actions.count("device.config_changed") == 1  # drift jen u skutečné změny

        admin = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{gw}/backups"
        d = admin.get(f"{base}/{b3['id']}/diff").json()
        assert "+/system identity set name=gw2" in d["diff"] and "tajne1" not in d["diff"]
        dl = admin.get(f"{base}/{b3['id']}/download")
        assert dl.status_code == 200 and "tajne1" in dl.text and "attachment" in dl.headers["content-disposition"]

        # technik smí diff, ale ne stáhnout celý export s hesly
        with operator_scope(db) as tx:
            u = auth.create_user(tx, "tech@firma.cz", PASSWORD)
            tx.execute("INSERT INTO role_binding (user_id, role, tenant_id) VALUES (%(u)s, 'technician', %(t)s)",
                       {"u": u, "t": tid})
        tech = _login(app, "tech@firma.cz")
        assert tech.get(f"{base}/{b3['id']}/diff").status_code == 200
        assert tech.get(f"{base}/{b3['id']}/download").status_code == 404
        # vyžádání zálohy
        assert tech.post(f"/api/admin/tenants/{tid}/devices/{gw}/backup").status_code == 200
    finally:
        drop_db(name)


def test_update_check_and_indicator():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        from test_inventory_backup import FakeApi
        calls = []

        def fake_device_api(tx, box_, dev, timeout=10.0):
            api = FakeApi({("POST", "/system/package/update/check-for-updates"): [{"status": "New version is available"}]})
            calls.append(api)
            return api

        orig = services.device_api
        services.device_api = fake_device_api
        try:
            assert worker.check_updates(db, box) == 1
            assert worker.check_updates(db, box) == 0  # do 24 h znovu ne
        finally:
            services.device_api = orig
        inventory.store(db, tid, gw, {"update": {"channel": "stable", "installed-version": "7.20.1",
                                                 "latest-version": "7.24.5"}}, [])
        c = _login(app)
        d = c.get(f"/api/admin/tenants/{tid}/devices").json()[0]
        assert d["update_available"] and d["latest_version"] == "7.24.5" and d["update_channel"] == "stable"
    finally:
        drop_db(name)


def test_site_clients_endpoint():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        inventory.store(db, tid, gw, {
            "interfaces": [{"name": "ether1", "type": "ether", "mac-address": "AA:00:00:00:00:01"}],
            "bridge_hosts": [{"mac-address": "11:11:11:11:11:11", "on-interface": "ether3", "vid": "10", "local": "false"}],
            "dhcp_leases": [{"mac-address": "11:11:11:11:11:11", "address": "10.0.10.5", "host-name": "pc-ucetni"}]}, [])
        c = _login(app)
        r = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/clients")
        assert r.status_code == 200
        assert r.json() == [{"mac": "11:11:11:11:11:11", "ip": "10.0.10.5", "hostname": "pc-ucetni", "device_id": gw,
                             "device": "gw", "port": "ether3", "vlan": "10", "wifi": None, "dhcp_status": None,
                             "candidates": 1}]
    finally:
        drop_db(name)


def test_upgrade_and_firmware_commands():
    from test_flow import _device_post

    from mtcontroller import alerts
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        c = _login(app)
        url = f"/api/admin/tenants/{tid}/devices/{gw}/commands"
        # dostupná verze zatím neznámá
        r = c.post(url, json_body={"kind": "upgrade", "version": "7.20.5"})
        assert r.status_code == 400 and "není známá" in r.json()["detail"]
        inv = modern()
        inv["update"] = {"channel": "stable", "installed-version": "7.20.1", "latest-version": "7.20.5"}
        inventory.store(db, tid, gw, inv, [])
        devs = c.get(f"/api/admin/tenants/{tid}/devices").json()
        assert devs[0]["update_available"] and devs[0]["latest_version"] == "7.20.5"
        # uživatel musí potvrdit přesně tu verzi, kterou zařízení nabízí
        r = c.post(url, json_body={"kind": "upgrade", "version": "7.20.4"})
        assert r.status_code == 400 and "nabízí verzi 7.20.5" in r.json()["detail"]
        r = c.post(url, json_body={"kind": "upgrade", "version": "7.20.5"})
        assert r.status_code == 201, r.text
        cid = r.json()["command_id"]
        # druhá aktualizace se nezařadí, dokud první neproběhne
        assert c.post(url, json_body={"kind": "firmware"}).status_code == 400

        r = _device_post(app, "/api/checkin", "key-GW1", {"version": "7.20.1 (stable)", "sstp": "up"})
        s = r.text
        assert "check-for-updates once" in s and '"7.20.5"' in s
        # ack odchází před instalací; instalace jen když verze ještě není nainstalovaná
        assert s.index("/tool fetch") < s.index("/system package update install")
        assert 'installed-version] != "7.20.5"' in s
        s.encode("ascii")
        _device_post(app, f"/api/checkin/ack?id={cid}&status=done", "key-GW1")

        def upgrade_alerts():
            with operator_scope(db) as tx:
                return [a for a in alerts.conditions(tx) if a.kind == "upgrade_failed"]

        assert upgrade_alerts() == []  # lhůta na stažení a restart
        with operator_scope(db) as tx:
            tx.execute("UPDATE device_command SET finished_at = now() - interval '20 minutes' WHERE id = %(c)s",
                       {"c": cid})
        found = upgrade_alerts()
        assert len(found) == 1 and "7.20.5" in found[0].message
        _device_post(app, "/api/checkin", "key-GW1", {"version": "7.20.5 (stable)", "sstp": "up"})
        assert upgrade_alerts() == []

        # verze s kritickým známým problémem se odmítne
        inv["update"] = {"channel": "stable", "installed-version": "7.20.5", "latest-version": "7.24.2"}
        inventory.store(db, tid, gw, inv, [])
        r = c.post(url, json_body={"kind": "upgrade", "version": "7.24.2"})
        assert r.status_code == 400 and "kritický" in r.json()["detail"]

        # RouterBOOT: restart jen při rozdílné verzi, po ack
        r = c.post(url, json_body={"kind": "firmware"})
        assert r.status_code == 201
        s = _device_post(app, "/api/checkin", "key-GW1", {"sstp": "up"}).text
        assert "/system routerboard upgrade" in s
        assert s.index("/tool fetch") < s.index("/system reboot")
        with operator_scope(db) as tx:
            acts = [r["action"] for r in tx.fetchall("SELECT action FROM audit_log ORDER BY id")]
        assert "device.command.upgrade" in acts and "device.command.firmware" in acts
    finally:
        drop_db(name)


def test_site_wifi_endpoint():
    from test_wifi import INVS
    db, name, settings, box, app, tid, sid = _env()
    try:
        ap = _adopted(db, box, tid, sid, "AP1", "10.200.1.1", "ap-hala")
        inv = modern()
        inv.update(INVS["a1"])
        inventory.store(db, tid, ap, inv, [])
        c = _login(app)
        w = c.get(f"/api/admin/tenants/{tid}/sites/{sid}/wifi").json()
        assert w["summary"]["radios"] == 3 and w["summary"]["clients"] == 3
        assert w["caps"][0]["identity"] == "cap-kancl"
        # cizí tenant nevidí nic
        other = services.create_tenant(db, settings, "Jina")
        assert c.get(f"/api/admin/tenants/{other['id']}/sites/{sid}/wifi").json()["radios"] == []
    finally:
        drop_db(name)


def test_traffic_endpoint():
    db, name, settings, box, app, tid, sid = _env()
    try:
        gw = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "gw")
        with tenant_scope(db, tid) as tx:
            for k in range(4):  # 4 vzorky po 5 min, 1 MB/5 min navíc RX, 2 MB TX
                tx.execute(
                    """INSERT INTO interface_sample (tenant_id, device_id, at, name, rx_bytes, tx_bytes)
                       VALUES (%(t)s, %(d)s, now() - make_interval(mins => %(m)s), 'ether1', %(rx)s, %(tx)s)""",
                    {"t": tid, "d": gw, "m": 140 - 5 * k, "rx": 1_000_000 * k, "tx": 2_000_000 * k})
            # restart čítačů (záporný rozdíl) se nesmí ukázat jako špička
            # (vzorky před 2 h – agregace zpracovává jen dokončené hodiny)
            tx.execute("""INSERT INTO interface_sample (tenant_id, device_id, at, name, rx_bytes, tx_bytes)
                          VALUES (%(t)s, %(d)s, now() - interval '120 minutes', 'ether1', 5, 5)""",
                       {"t": tid, "d": gw})
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/devices/{gw}/traffic"
        r = c.get(base + "?interface=ether1&hours=6").json()
        assert r["resolution"] == "5m" and len(r["points"]) == 3
        assert all(abs(p["rx"] - 1_000_000 * 8 / 300) < 100 and abs(p["tx"] - 2 * 1_000_000 * 8 / 300) < 100
                   for p in r["points"])
        inventory.rollup(db)
        r = c.get(base + "?interface=ether1&hours=168").json()
        assert r["resolution"] == "1h" and r["points"] and r["points"][-1]["rx_max"] > 0
        assert c.get(base).status_code == 400
    finally:
        drop_db(name)
