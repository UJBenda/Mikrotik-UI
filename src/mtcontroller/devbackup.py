"""Zálohy konfigurace zařízení: které zařízení zálohovat, společná pravidla zákazníka, verze a porovnání.

Záloha je textový export RouterOS (`/export show-sensitive`), uložený šifrovaně v databázi (viz backup.py) –
je tedy součástí každé zálohy controlleru i jejích kopií v cloudu. Zálohovat jde řízená i jen sledovaná
zařízení; u každého se dá zálohování vypnout (`device.backup_enabled`).

Pravidla jsou jedna pro zákazníka (`device_backup_policy`):
- `follow` – řídit se plánem zálohy controlleru: zařízení se zazálohují v jeho termínu a drží se stejná retence,
- `custom` – vlastní interval (hodiny) a vlastní retence (posledních N + týdenní + měsíční verze),
- `only_changes` – novou verzi uložit jen tehdy, když se konfigurace od minulé zálohy změnila.

Obnova na zařízení se odsud zatím nespouští (export se nedá nahrát zpět bez resetu zařízení a ztráty
certifikátů tunelu); k dispozici je porovnání verze s aktuálním stavem a stažení exportu.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import backupplan, retention
from .db import Database, Tx, operator_scope
from .services import ServiceError, audit

MODES = ("follow", "custom")
DEFAULT = {"mode": "custom", "every_hours": 24, "only_changes": False, "keep": 365, "weekly": 0, "monthly": 0}


def _row(r: dict | None) -> dict:
    if not r:
        return dict(DEFAULT)
    return {"mode": r["mode"], "every_hours": r["every_hours"], "only_changes": r["only_changes"], "keep": r["keep"],
            "weekly": r["keep_weekly"], "monthly": r["keep_monthly"]}


def policy(tx: Tx, tenant_id: Any) -> dict:
    return _row(tx.fetchone("SELECT * FROM device_backup_policy WHERE tenant_id = %(t)s", {"t": str(tenant_id)}))


def validate(data: dict, old: dict | None = None) -> dict:
    out = dict(old or DEFAULT)
    if "mode" in data:
        if data["mode"] not in MODES:
            raise ServiceError("režim: follow (podle zálohy controlleru) nebo custom (vlastní)")
        out["mode"] = data["mode"]
    if "every_hours" in data:
        try:
            h = int(data["every_hours"])
        except (TypeError, ValueError):
            raise ServiceError("interval záloh 1–720 hodin") from None
        if not 1 <= h <= 720:
            raise ServiceError("interval záloh 1–720 hodin")
        out["every_hours"] = h
    if "only_changes" in data:
        out["only_changes"] = bool(data["only_changes"])
    for k in ("keep", "weekly", "monthly"):
        if k in data:
            out[k] = retention.number(k, data[k])
    return out


def set_policy(tx: Tx, tenant_id: Any, data: dict, user_id: Any = None, ip: str | None = None) -> dict:
    old = policy(tx, tenant_id)
    new = validate(data, old)
    tx.execute(
        """INSERT INTO device_backup_policy (tenant_id, mode, every_hours, only_changes, keep, keep_weekly, keep_monthly)
           VALUES (%(t)s, %(mode)s, %(every_hours)s, %(only_changes)s, %(keep)s, %(weekly)s, %(monthly)s)
           ON CONFLICT (tenant_id) DO UPDATE SET mode = EXCLUDED.mode, every_hours = EXCLUDED.every_hours,
               only_changes = EXCLUDED.only_changes, keep = EXCLUDED.keep, keep_weekly = EXCLUDED.keep_weekly,
               keep_monthly = EXCLUDED.keep_monthly, updated_at = now()""", {"t": str(tenant_id), **new})
    audit(tx, "tenant.device_backup_policy", tenant_id=tenant_id, user_id=user_id, target=f"tenant:{tenant_id}",
          before=old, after=new, ip=ip)
    return new


def effective(pol: dict, plan: dict) -> dict:
    """Pravidla po dosazení plánu controlleru (režim follow přebírá jeho retenci)."""
    if pol["mode"] == "follow":
        return {**pol, "keep": plan.get("keep", 14), "weekly": plan.get("weekly", 0), "monthly": plan.get("monthly", 0)}
    return pol


def last_slot(plan: dict, now: dt.datetime) -> dt.datetime | None:
    """Poslední termín plánu controlleru, který už nastal (nejdál týden zpět)."""
    if not plan.get("enabled"):
        return None
    h, m = (int(x) for x in plan["time"].split(":"))
    for back in range(0, 8):
        day = now - dt.timedelta(days=back)
        s = day.replace(hour=h, minute=m, second=0, microsecond=0)
        if day.isoweekday() in plan["days"] and s <= now:
            return s
    return None


def cutoff(pol: dict, plan: dict, now: dt.datetime) -> dt.datetime:
    """Zařízení se zálohou starší než tento okamžik je na řadě."""
    if pol["mode"] == "follow":
        s = last_slot(plan, now)
        if s is not None:
            return s
        return now - dt.timedelta(hours=24)      # plán controlleru je vypnutý → aspoň denně
    return now - dt.timedelta(hours=pol["every_hours"])


def cutoffs(db: Database, keep_default: int = 14, now: dt.datetime | None = None) -> tuple[dict[str, dt.datetime], dict[str, dict]]:
    """({tenant: hranice}, {tenant: účinná pravidla}) pro worker; zákazníci bez řádku mají výchozí pravidla."""
    now = now or dt.datetime.now().astimezone()
    plan = backupplan.load(db, keep_default)
    cuts, pols = {}, {}
    with operator_scope(db) as tx:
        rows = {str(r["tenant_id"]): r for r in tx.fetchall("SELECT * FROM device_backup_policy")}
        for t in tx.fetchall("SELECT id FROM tenant"):
            pol = _row(rows.get(str(t["id"])))
            cuts[str(t["id"])] = cutoff(pol, plan, now)
            pols[str(t["id"])] = effective(pol, plan)
    return cuts, pols


def prune(tx: Tx, device_id: Any, pol: dict) -> int:
    """Promaže verze zařízení podle retence. Nejnovější verze zůstává vždy."""
    rows = tx.fetchall("SELECT id, taken_at FROM device_backup WHERE device_id = %(d)s", {"d": str(device_id)})
    kept = retention.select_dated([(r["taken_at"], r["id"]) for r in rows], pol["keep"], pol["weekly"], pol["monthly"])
    drop = [r["id"] for r in rows if r["id"] not in kept]
    if drop:
        tx.execute("DELETE FROM device_backup WHERE device_id = %(d)s AND id = ANY(string_to_array(%(ids)s, ',')::bigint[])",
                   {"d": str(device_id), "ids": ",".join(str(i) for i in drop)})
    return len(drop)


def overview(tx: Tx, db: Database, tenant_id: Any, site_id: Any = None, keep_default: int = 14) -> dict:
    """Stránka Zálohy zařízení: pravidla, plán controlleru (pro režim follow) a zařízení se stavem záloh."""
    plan = backupplan.load(db, keep_default)
    pol = policy(tx, tenant_id)
    devs = tx.fetchall(
        """SELECT d.id, coalesce(nullif(d.identity, ''), d.serial) AS name, d.serial, d.model, d.state, d.site_id,
                  s.name AS site, d.backup_enabled, d.backup_at, d.backup_error, d.managed_at IS NOT NULL AS managed,
                  (d.tunnel_ip IS NOT NULL AND d.api_cert_fingerprint IS NOT NULL) AS reachable,
                  b.n AS versions, b.changes, b.last_at, b.last_change, b.bytes
           FROM device d LEFT JOIN site s ON s.id = d.site_id
           LEFT JOIN LATERAL (SELECT count(*) AS n, count(*) FILTER (WHERE changed) AS changes, max(taken_at) AS last_at,
                                     max(taken_at) FILTER (WHERE changed) AS last_change, sum(size_bytes) AS bytes
                              FROM device_backup x WHERE x.device_id = d.id) b ON true
           WHERE d.tenant_id = %(t)s AND d.state IN ('adopted', 'offline')
             AND (%(s)s::uuid IS NULL OR d.site_id = %(s)s::uuid)
           ORDER BY s.name NULLS LAST, name""", {"t": str(tenant_id), "s": str(site_id) if site_id else None})
    out = []
    for d in devs:
        out.append({"id": str(d["id"]), "name": d["name"], "serial": d["serial"], "model": d["model"], "state": d["state"],
                    "site": d["site"], "managed": bool(d["managed"]), "enabled": d["backup_enabled"],
                    "reachable": bool(d["reachable"]), "backup_at": d["last_at"], "error": d["backup_error"],
                    "versions": int(d["versions"] or 0), "changes": int(d["changes"] or 0),
                    "last_change": d["last_change"], "bytes": int(d["bytes"] or 0)})
    return {"policy": pol, "effective": effective(pol, plan),
            "plan": {k: plan.get(k) for k in ("enabled", "time", "days", "keep", "weekly", "monthly")}, "devices": out}


def select(tx: Tx, tenant_id: Any, device_ids: list, enabled: bool, user_id: Any = None, ip: str | None = None) -> int:
    ids = [str(i) for i in device_ids][:2000]
    if not ids:
        return 0
    n = tx.execute(
        """UPDATE device SET backup_enabled = %(e)s, backup_error = CASE WHEN %(e)s THEN backup_error ELSE NULL END
           WHERE tenant_id = %(t)s AND id = ANY(string_to_array(%(ids)s, ',')::uuid[]) AND backup_enabled <> %(e)s""",
        {"e": bool(enabled), "t": str(tenant_id), "ids": ",".join(ids)})
    if n:
        audit(tx, "device.backup_enabled" if enabled else "device.backup_disabled", tenant_id=tenant_id, user_id=user_id,
              target=f"tenant:{tenant_id}", after={"devices": ids, "enabled": bool(enabled)}, ip=ip)
    return int(n or 0)


def versions(tx: Tx, device_id: Any, limit: int = 400) -> list[dict]:
    rows = tx.fetchall(
        """SELECT id, taken_at, reason, size_bytes, changed FROM device_backup WHERE device_id = %(d)s
           ORDER BY id DESC LIMIT %(n)s""", {"d": str(device_id), "n": int(limit)})
    return [{"id": r["id"], "taken_at": r["taken_at"], "reason": r["reason"], "size": r["size_bytes"],
             "changed": r["changed"], "latest": i == 0} for i, r in enumerate(rows)]
