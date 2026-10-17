"""Vyhodnocení alertů (worker, každý tick) a jejich životní cyklus.

Každá podmínka vrací množinu aktivních klíčů (zařízení, druh, předmět).
Nový klíč = otevřený alert, trvající = aktualizace last_seen, zmizelý =
automatické uzavření. Notifikace se posílají při otevření a uzavření
(viz notify.py); neúspěšné odeslání se zkusí znovu v dalším ticku.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import health, ros_versions
from .db import Database, Tx, operator_scope

log = logging.getLogger("mtc.alerts")

FLAP_LINK_DOWNS = 5        # výpadků linky za 2 hodiny
PORT_ERRORS = 100          # chyb RX+TX za poslední hodinu
CPU_HIGH = 90
MEM_HIGH = 90
BACKUP_STALE_HOURS = 48


@dataclass(frozen=True)
class Active:
    tenant_id: Any
    device_id: Any
    kind: str
    subject: str
    severity: str
    message: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (str(self.device_id), self.kind, self.subject)


def _label(d: dict) -> str:
    return d.get("identity") or d.get("serial") or str(d["id"])


def conditions(tx: Tx) -> list[Active]:
    out: list[Active] = []
    devs = tx.fetchall(
        """SELECT d.id, d.tenant_id, d.serial, d.identity, d.state, d.ros_version, d.last_checkin_at,
                  d.last_status, d.inventory_error, d.inventory_at, d.backup_at, d.adopted_at,
                  (SELECT data FROM device_inventory i WHERE i.device_id = d.id AND i.section = 'resource') AS res
           FROM device d WHERE d.state IN ('adopted', 'offline', 'provisioning')""")
    for d in devs:
        name = _label(d)
        t, i = d["tenant_id"], d["id"]
        if d["state"] == "offline":
            seen = f"{d['last_checkin_at']:%Y-%m-%d %H:%M} UTC" if d["last_checkin_at"] else "nikdy"
            out.append(Active(t, i, "offline", "", "critical",
                              f"{name}: zařízení je offline (poslední check-in {seen})"))
            continue
        st = d["last_status"] or {}
        if d["state"] == "adopted" and st.get("sstp") != "up" and int(st.get("sstp_down") or 0) >= 3:
            out.append(Active(t, i, "tunnel_down", "", "warning",
                              f"{name}: SSTP tunel neběží (zařízení se hlásí jen přes check-in)"))
        if d["state"] == "adopted" and d["inventory_error"] and "otisk certifikátu" not in d["inventory_error"]:
            out.append(Active(t, i, "rest_unreachable", "", "warning",
                              f"{name}: inventura přes tunel selhává: {d['inventory_error'][:200]}"))
        if d["state"] == "adopted" and d["inventory_error"] and "otisk certifikátu" in d["inventory_error"]:
            out.append(Active(t, i, "rest_pin_mismatch", "", "critical",
                              f"{name}: certifikát REST na zařízení nesouhlasí s uloženým otiskem – možné podvržení"))
        res = d["res"] or {}
        try:
            cpu = int(res.get("cpu-load"))
            if cpu >= CPU_HIGH:
                out.append(Active(t, i, "cpu_high", "", "warning", f"{name}: vytížení CPU {cpu} %"))
        except (TypeError, ValueError):
            pass
        try:
            used = 100 - 100 * int(res.get("free-memory")) / int(res.get("total-memory"))
            if used >= MEM_HIGH:
                out.append(Active(t, i, "memory_high", "", "warning", f"{name}: obsazená paměť {used:.0f} %"))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        if d["state"] == "adopted" and d["adopted_at"]:
            stale = tx.fetchone(
                f"""SELECT (%(b)s::timestamptz IS NULL AND %(a)s::timestamptz < now() - interval '{BACKUP_STALE_HOURS} hours')
                       OR %(b)s::timestamptz < now() - interval '{BACKUP_STALE_HOURS} hours' AS stale""",
                {"b": d["backup_at"], "a": d["adopted_at"]})["stale"]
            if stale:
                out.append(Active(t, i, "backup_stale", "", "warning",
                                  f"{name}: záloha konfigurace starší než {BACKUP_STALE_HOURS} h"))
        for issue in ros_versions.issues_for(d["ros_version"] or ""):
            if issue.severity == "critical":
                out.append(Active(t, i, "ros_issue", issue.first, "warning",
                                  f"{name}: RouterOS {d['ros_version']} – {issue.text}"))

    for r in tx.fetchall(
            f"""SELECT h.tenant_id, h.device_id, h.name, sum(h.link_downs) AS downs,
                       d.identity, d.serial, d.id
                FROM interface_hourly h JOIN device d ON d.id = h.device_id
                WHERE h.hour > now() - interval '2 hours' AND d.state = 'adopted'
                GROUP BY h.tenant_id, h.device_id, h.name, d.identity, d.serial, d.id
                HAVING sum(h.link_downs) >= {FLAP_LINK_DOWNS}"""):
        out.append(Active(r["tenant_id"], r["device_id"], "port_flapping", r["name"], "warning",
                          f"{_label(r)}: port {r['name']} – {r['downs']} výpadků linky za 2 h"))
    for r in tx.fetchall(
            f"""SELECT h.tenant_id, h.device_id, h.name, sum(coalesce(h.rx_errors, 0) + coalesce(h.tx_errors, 0)) AS errs,
                       d.identity, d.serial, d.id
                FROM interface_hourly h JOIN device d ON d.id = h.device_id
                WHERE h.hour > now() - interval '1 hour' AND d.state = 'adopted'
                GROUP BY h.tenant_id, h.device_id, h.name, d.identity, d.serial, d.id
                HAVING sum(coalesce(h.rx_errors, 0) + coalesce(h.tx_errors, 0)) >= {PORT_ERRORS}"""):
        out.append(Active(r["tenant_id"], r["device_id"], "port_errors", r["name"], "warning",
                          f"{_label(r)}: port {r['name']} – {r['errs']} chyb za poslední hodinu (kabel, duplex, SFP?)"))
    for r in tx.fetchall(
            """SELECT DISTINCT ON (a.target) a.tenant_id, substr(a.target, 8)::uuid AS device_id, a.action, a.after,
                      d.identity, d.serial, d.id
               FROM audit_log a JOIN device d ON d.id = substr(a.target, 8)::uuid
               WHERE a.action IN ('device.config_changed', 'device.cert_changed')
                 AND a.target LIKE 'device:%' AND a.at > now() - interval '24 hours'
               ORDER BY a.target, a.id DESC"""):
        if r["action"] == "device.cert_changed":
            out.append(Active(r["tenant_id"], r["device_id"], "cert_changed", "", "critical",
                              f"{_label(r)}: zařízení hlásí jiný certifikát REST než při adopci"))
        elif not (r["after"] or {}).get("by_controller"):
            out.append(Active(r["tenant_id"], r["device_id"], "config_drift", "", "info",
                              f"{_label(r)}: konfigurace změněna mimo controller (viz zálohy)"))
    # DHCP pooly a NTP: jen zařízení, která DHCP server / NTP klienta mají
    for r in tx.fetchall(
            """SELECT d.id, d.tenant_id, d.identity, d.serial,
                      jsonb_object_agg(i.section, i.data) AS inv
               FROM device d JOIN device_inventory i ON i.device_id = d.id
               WHERE d.state = 'adopted'
                 AND i.section IN ('dhcp_usage', 'ntp_client', 'resource', 'hardening')
               GROUP BY d.id, d.tenant_id, d.identity, d.serial"""):
        inv = r["inv"] or {}
        for p in inv.get("dhcp_usage") or []:
            if p["pct"] >= health.POOL_FULL_PCT:
                out.append(Active(r["tenant_id"], r["id"], "dhcp_pool_full", str(p["server"]), "warning",
                                  f"{_label(r)}: DHCP {p['server']} – pool {p['pool']} zaplněn na {p['pct']} % "
                                  f"({p['used']}/{p['size']})"))
        for f in inv.get("hardening") or []:
            if isinstance(f, dict) and f.get("severity") == "critical":
                out.append(Active(r["tenant_id"], r["id"], "security_exposed", str(f.get("code") or ""), "critical",
                                  f"{_label(r)}: {f.get('text')}"))
        st = health.ntp_unsynced(inv)
        if st:
            out.append(Active(r["tenant_id"], r["id"], "ntp_unsynced", "", "warning",
                              f"{_label(r)}: hodiny nejsou synchronizované přes NTP (stav {st})"))
    for r in tx.fetchall(
            """SELECT i.tenant_id, i.device_id, e->>'name' AS name, e->>'rate' AS rate, d.identity, d.serial, d.id
               FROM device_inventory i JOIN device d ON d.id = i.device_id,
                    jsonb_array_elements(CASE WHEN jsonb_typeof(i.data) = 'array' THEN i.data ELSE '[]'::jsonb END) e
               WHERE i.section = 'ethernet_monitor' AND d.state = 'adopted'
                 AND e->>'status' = 'link-ok' AND e->>'full-duplex' = 'false'"""):
        out.append(Active(r["tenant_id"], r["device_id"], "port_half_duplex", r["name"], "warning",
                          f"{_label(r)}: port {r['name']} běží v half-duplexu ({r['rate'] or '?'}) – "
                          "nesoulad autonegociace s protistranou?"))
    for r in tx.fetchall(
            """SELECT DISTINCT ON (c.device_id) c.tenant_id, c.device_id, c.status::text AS status,
                      c.params->>'version' AS target, c.finished_at < now() - interval '15 minutes' AS grace_over,
                      d.ros_version, d.identity, d.serial, d.id
               FROM device_command c JOIN device d ON d.id = c.device_id
               WHERE c.kind = 'upgrade' AND c.status IN ('done', 'failed')
                 AND c.finished_at > now() - interval '7 days'
               ORDER BY c.device_id, c.id DESC"""):
        if r["status"] == "failed":
            out.append(Active(r["tenant_id"], r["device_id"], "upgrade_failed", r["target"] or "", "warning",
                              f"{_label(r)}: aktualizace na {r['target']} se nespustila (verze není v kanálu zařízení?)"))
        elif (ros_versions.parse(r["ros_version"] or "") != ros_versions.parse(r["target"] or "")
              and r["grace_over"]):
            out.append(Active(r["tenant_id"], r["device_id"], "upgrade_failed", r["target"] or "", "warning",
                              f"{_label(r)}: po aktualizaci na {r['target']} hlásí verzi {r['ros_version']}"
                              " – stažení nebo instalace selhaly (místo na disku? /log)"))
    return out


def reconcile(db: Database) -> tuple[list[dict], list[dict]]:
    """Otevře nové, aktualizuje trvající a uzavře zmizelé alerty.
    Vrací (otevřené, uzavřené) pro notifikace."""
    opened, closed = [], []
    with operator_scope(db) as tx:
        active = {a.key: a for a in conditions(tx)}
        existing = {(str(r["device_id"]), r["kind"], r["subject"]): r for r in tx.fetchall(
            "SELECT id, device_id, kind, subject FROM alert WHERE resolved_at IS NULL")}
        for key, a in active.items():
            if key in existing:
                tx.execute("UPDATE alert SET last_seen_at = now(), message = %(m)s, severity = %(s)s WHERE id = %(i)s",
                           {"m": a.message, "s": a.severity, "i": existing[key]["id"]})
            else:
                row = tx.fetchone(
                    """INSERT INTO alert (tenant_id, device_id, kind, subject, severity, message)
                       VALUES (%(t)s, %(d)s, %(k)s, %(sub)s, %(s)s, %(m)s) RETURNING id""",
                    {"t": str(a.tenant_id), "d": str(a.device_id), "k": a.kind, "sub": a.subject,
                     "s": a.severity, "m": a.message})
                opened.append({"id": row["id"], **a.__dict__})
        for key, r in existing.items():
            if key not in active:
                tx.execute("UPDATE alert SET resolved_at = now() WHERE id = %(i)s", {"i": r["id"]})
                closed.append({"id": r["id"]})
    return opened, closed
