"""Chybovost portů: přírůstky čítačů, hodnocení poměrem k provozu, alerty a pokles výkonu SFP."""


def _inv(rxp, fcs, late=0, rxerr=None, sfp=None, reset=False):
    data = {
        "interfaces": [{"name": "ether2", "type": "ether", "running": "true", "rx-byte": "1", "tx-byte": "1",
                        "rx-packet": str(rxp), "tx-packet": str(rxp), "rx-error": str(fcs if rxerr is None else rxerr),
                        "tx-error": "0", "rx-drop": "0", "tx-drop": "0", "link-downs": "0"},
                       {"name": "bridge", "type": "bridge", "rx-packet": "5", "rx-error": "900"}],
        "ethernet_stats": [{"name": "ether2", "rx-fcs-error": str(fcs), "tx-late-collision": str(late), "rx-pause": "3"}],
    }
    if sfp is not None:
        data["interfaces"].append({"name": "sfp-sfpplus1", "type": "ether", "rx-packet": "10", "tx-packet": "10"})
        data["ethernet_monitor"] = [{"name": "sfp-sfpplus1", "status": "link-ok", "sfp-rx-power": str(sfp)}]
    return data


def test_counters_deltas_and_evaluate():
    from mtcontroller import porthealth as ph

    a, b = ph.counters(_inv(1000, 5)), ph.counters(_inv(51000, 45, late=2))
    assert "bridge" not in a and a["ether2"]["fcs"] == 5 and a["ether2"]["rx_packets"] == 1000
    d = ph.deltas(a, b)["ether2"]
    assert d["fcs"] == 40 and d["late_collision"] == 2 and d["rx_packets"] == 50000 and "pause" not in d
    # vynulování čítačů (restart) → port se přeskočí, žádné záporné ani nesmyslné přírůstky
    assert ph.deltas(b, ph.counters(_inv(10, 0))) == {}
    assert ph.deltas({}, b) == {}

    ev = ph.evaluate({"rx_packets": 50000, "tx_packets": 50000, "fcs": 40, "late_collision": 2, "generic": 40})
    assert ev["errors"] == 42 and ev["level"] == "warning" and ev["top"] == "fcs"
    # stejný počet chyb na vytíženém uplinku je pod prahem
    assert ph.evaluate({"rx_packets": 5_000_000, "tx_packets": 5_000_000, "fcs": 42})["level"] == "ok"
    assert ph.evaluate({"rx_packets": 4000, "tx_packets": 4000, "fcs": 300})["level"] == "critical"
    # bez podrobných čítačů zůstává souhrn; bez počtu rámců absolutní práh
    g = ph.evaluate({"generic": 150})
    assert g["level"] == "warning" and g["top"] == "generic" and g["ratio"] is None
    assert ph.evaluate({"generic": 50})["level"] == "ok"
    assert "FCS" in ph.message("ether2", ev) and "0,04 % rámců" in ph.message("ether2", ev)


def test_record_alert_site_and_api():
    from helpers import drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import alerts, inventory, porthealth as ph, porttools
    from mtcontroller.db import operator_scope, tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        inventory.store(db, tid, sw, _inv(1000, 5, sfp=-4.0), [])
        with operator_scope(db) as tx:
            # první inventura nemá s čím porovnat – jen výkon SFP
            rows = tx.fetchall("SELECT name, fcs, sfp_rx FROM port_error_hourly ORDER BY name")
            assert [(r["name"], r["fcs"]) for r in rows] == [("sfp-sfpplus1", 0)] and rows[0]["sfp_rx"] == -4.0
        inventory.store(db, tid, sw, _inv(51000, 45, late=2, sfp=-8.5), [])
        inventory.store(db, tid, sw, _inv(101000, 85, late=2, sfp=-8.5), [])
        with operator_scope(db) as tx:
            r = tx.fetchone("SELECT * FROM port_error_hourly WHERE name = 'ether2'")
            assert r["fcs"] == 80 and r["late_collision"] == 2 and r["rx_packets"] == 100000 and r["generic"] == 80
            assert r["pause"] == 0
            # týden historie SFP kolem −4 dBm → poslední hodnota −8,5 je pokles
            tx.execute("""INSERT INTO port_error_hourly (tenant_id, device_id, name, hour, sfp_rx)
                          SELECT %(t)s, %(d)s, 'sfp-sfpplus1', date_trunc('hour', now()) - make_interval(hours => h), -4.0
                          FROM generate_series(3, 40) h""", {"t": tid, "d": sw})
            found = {(a["kind"], a["port"]): a for a in ph.alerts(tx)}
        assert found[("port_errors", "ether2")]["severity"] == "warning" and "FCS" in found[("port_errors", "ether2")]["message"]
        assert "-8.5 dBm" in found[("sfp_power", "sfp-sfpplus1")]["message"]
        alerts.reconcile(db)
        with operator_scope(db) as tx:
            kinds = {(r["kind"], r["subject"]) for r in tx.fetchall("SELECT kind, subject FROM alert WHERE resolved_at IS NULL")}
            msg = tx.fetchone("SELECT message FROM alert WHERE kind = 'port_errors'")["message"]
        assert ("port_errors", "ether2") in kinds and ("sfp_power", "sfp-sfpplus1") in kinds and msg.startswith("sw-hala: port ether2")

        with tenant_scope(db, tid) as tx:
            h = ph.site_health(tx, sid)
            assert h[(sw, "ether2")]["level"] == "warning" and h[(sw, "ether2")]["errors_24h"] == 82
            assert h[(sw, "ether2")]["why"] == ["errors"] and h[(sw, "sfp-sfpplus1")]["why"] == ["sfp"]
            ports = {p["port"]: p for d in porttools.site_ports(tx, sid) for p in d["ports"]}
        assert ports["ether2"]["health"]["level"] == "warning" and ports["ether2"]["health"]["kinds"]["fcs"] == 80
        c = _login(app)
        r = c.get(f"/api/admin/tenants/{tid}/devices/{sw}/port-errors?interface=ether2").json()
        assert len(r["points"]) == 1 and r["points"][0]["errors"] == 82 and r["points"][0]["kinds"]["late_collision"] == 2
        # starý předchozí stav (zařízení bylo dlouho pryč) se nezapočítá do aktuální hodiny
        with operator_scope(db) as tx:
            tx.execute("UPDATE device_inventory SET collected_at = now() - interval '5 hours'")
        inventory.store(db, tid, sw, _inv(900000, 5000), [])
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT fcs FROM port_error_hourly WHERE name = 'ether2'")["fcs"] == 80
    finally:
        drop_db(name)
