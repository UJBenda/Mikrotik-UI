"""Časová osa lokality: všechno, co se stalo, na jedné ose – pro hledání příčiny problému.

Skládá události, které controller už sbírá (nic nového se neukládá):

- `alert`   – alert vznikl / pominul (závažnost podle alertu),
- `port`    – změna na portu (linka, rychlost, PoE, soused, smyčka…) z `port_event`,
- `client`  – klient se připojil / odpojil (`client_session`),
- `wifi`    – roaming a neúspěšné přihlášení k Wi-Fi (`wifi_event`, ze syslogu zařízení),
- `change`  – změna konfigurace controllerem (`config_plan`: provedeno, selhalo, vráceno),
- `audit`   – zásah člověka (audit: restart, aktualizace, adopce, nastavení lokality…).

Každá událost: {at, cat, kind, sev, device_id, device, port, mac, client, text, a, b, user}.
`buckets` = počty po úsecích času (pro graf nad seznamem), počítané ze všech událostí okna,
i když se seznam ořízne limitem.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import porthistory
from .db import Tx

CATS = ("alert", "port", "client", "wifi", "change", "audit")
MAX_HOURS = 24 * 30


def state_at(tx: Any, site_id: Any, at: Any) -> dict:
    """Stav lokality v čase `at` pro přehrávání na mapě: která zařízení byla offline, jaké alerty byly otevřené
    a v jakém stavu byly měřené spoje. Topologii v čase neukládáme – přehrává se jen stav nad dnešní mapou."""
    from . import wlink

    p = {"s": str(site_id), "t": at}
    alerts = tx.fetchall(
        """SELECT a.device_id, coalesce(nullif(d.identity, ''), d.serial) AS device, a.kind, a.subject,
                  a.severity::text AS severity, a.message, a.opened_at
           FROM alert a JOIN device d ON d.id = a.device_id
           WHERE d.site_id = %(s)s AND a.opened_at <= %(t)s AND (a.resolved_at IS NULL OR a.resolved_at > %(t)s)
           ORDER BY a.severity DESC, a.opened_at DESC LIMIT 200""", p)
    links = {}
    for r in tx.fetchall(
            """SELECT DISTINCT ON (s.link_id) s.link_id, s.up, s.loss FROM wlink_sample s JOIN wlink l ON l.id = s.link_id
               WHERE l.site_id = %(s)s AND s.at <= %(t)s AND s.at > %(t)s::timestamptz - interval '20 minutes'
               ORDER BY s.link_id, s.at DESC""", p):
        loss = r["loss"]
        links[str(r["link_id"])] = ("down" if not r["up"] else "bad" if loss is not None and loss >= wlink.LOSS_CRIT
                                    else "degraded" if loss is not None and loss >= wlink.LOSS_WARN else "ok")
    return {"at": _iso(at), "offline": sorted({str(a["device_id"]) for a in alerts if a["kind"] == "offline"}),
            "alerts": [{"device_id": str(a["device_id"]), "device": a["device"], "kind": a["kind"], "subject": a["subject"],
                        "severity": a["severity"], "message": a["message"], "opened_at": a["opened_at"]} for a in alerts],
            "links": links}
LIMIT = 500
BUCKETS = 48
SEV_RANK = {"critical": 3, "warning": 2, "ok": 1, "info": 0}
# audit: jen zásahy do lokality a zařízení (ne přihlášení, čtení tajemství apod.)
AUDIT_SKIP = ("plan.", "auth.", "session.", "user.", "secret.")


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, dt.datetime) else str(v)


def events(tx: Tx, site_id: Any, *, hours: int = 24, minutes: int | None = None, until: dt.datetime | None = None, cats: set[str] | None = None,
           device_id: str | None = None, mac: str | None = None, limit: int = LIMIT) -> dict:
    # okno: hodiny, nebo minuty (úzké okno kolem jedné události – „co se dělo kolem“)
    span = max(2, min(int(minutes), MAX_HOURS * 60)) if minutes else max(1, min(int(hours), MAX_HOURS)) * 60
    until = until or dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(minutes=span)
    cats = {c for c in (cats or CATS) if c in CATS}
    mac = mac.upper() if mac else None
    p: dict[str, Any] = {"s": str(site_id), "a": since, "b": until, "d": device_id, "m": mac}
    devs = {str(r["id"]): (r["identity"] or r["serial"]) for r in tx.fetchall(
        "SELECT id, identity, serial FROM device WHERE site_id = %(s)s", p)}
    names = {r["mac"]: (r["note"] or r["hostname"] or r["ip"] or r["mac"]) for r in tx.fetchall(
        """SELECT c.mac, c.hostname, c.ip, n.note FROM client c LEFT JOIN client_note n ON n.mac = c.mac
           WHERE c.site_id = %(s)s""", p)}
    out: list[dict] = []

    def add(at: Any, cat: str, kind: str, sev: str = "info", **kw: Any) -> None:
        e = {"at": _iso(at), "cat": cat, "kind": kind, "sev": sev}
        e.update({k: v for k, v in kw.items() if v not in (None, "")})
        if e.get("device_id"):
            e["device"] = devs.get(str(e["device_id"]), "?")
        if e.get("mac"):
            e["client"] = names.get(e["mac"], e["mac"])
        out.append(e)

    dev_f = " AND x.device_id = %(d)s" if device_id else ""
    if "alert" in cats and not mac:
        for r in tx.fetchall(
                f"""SELECT x.id, x.device_id, x.kind, x.subject, x.severity::text AS sev, x.message, x.opened_at, x.resolved_at
                    FROM alert x JOIN device d ON d.id = x.device_id
                    WHERE d.site_id = %(s)s{dev_f} AND ((x.opened_at >= %(a)s AND x.opened_at < %(b)s)
                       OR (x.resolved_at >= %(a)s AND x.resolved_at < %(b)s))""", p):
            sev = r["sev"] if r["sev"] in ("critical", "warning") else "info"
            if since <= r["opened_at"] < until:
                add(r["opened_at"], "alert", "opened", sev, device_id=str(r["device_id"]), text=r["message"],
                    a=r["kind"], port=r["subject"] or None)
            if r["resolved_at"] and since <= r["resolved_at"] < until:
                add(r["resolved_at"], "alert", "resolved", "ok", device_id=str(r["device_id"]), text=r["message"],
                    a=r["kind"], port=r["subject"] or None)
    if "port" in cats and not mac:
        for r in tx.fetchall(
                f"""SELECT x.at, x.device_id, x.port, x.field, x.old, x.new FROM port_event x JOIN device d ON d.id = x.device_id
                    WHERE d.site_id = %(s)s{dev_f} AND x.at >= %(a)s AND x.at < %(b)s""", p):
            bad = (r["field"] == "link" and r["new"] != "up") or r["field"] == "flaps" or \
                  (r["field"] == "loop" and r["new"] == "ano")
            add(r["at"], "port", r["field"], "warning" if bad else "info", device_id=str(r["device_id"]), port=r["port"],
                text=porthistory.describe(r), a=r["old"], b=r["new"])
    if "client" in cats:
        mac_f = " AND mac = %(m)s" if mac else ""
        for r in tx.fetchall(
                f"""SELECT mac, started_at, ended_at, wifi, device, port, ssid FROM client_session
                    WHERE site_id = %(s)s{mac_f} AND ((started_at >= %(a)s AND started_at < %(b)s)
                       OR (ended_at >= %(a)s AND ended_at < %(b)s))""", p):
            if device_id and r["device"] != devs.get(device_id):
                continue
            where = {"via": r["device"], "port": r["ssid"] if r["wifi"] else r["port"], "wifi": bool(r["wifi"])}
            if since <= r["started_at"] < until:
                add(r["started_at"], "client", "connected", mac=r["mac"], **where)
            if r["ended_at"] and since <= r["ended_at"] < until:
                add(r["ended_at"], "client", "disconnected", mac=r["mac"], **where)
    if "wifi" in cats:
        mac_f = " AND x.mac = %(m)s" if mac else ""
        for r in tx.fetchall(
                f"""SELECT x.at, x.device_id, x.mac, x.kind, x.iface, x.signal, x.ft, x.reason FROM wifi_event x
                    WHERE x.site_id = %(s)s{dev_f}{mac_f} AND x.at >= %(a)s AND x.at < %(b)s
                      AND x.kind IN ('roamed', 'auth_fail')""", p):
            add(r["at"], "wifi", r["kind"], "warning" if r["kind"] == "auth_fail" else "info", device_id=str(r["device_id"]),
                mac=r["mac"], port=r["iface"], text=r["reason"], a=r["signal"], b=bool(r["ft"]))
    if "change" in cats and not mac:
        for r in tx.fetchall(
                f"""SELECT x.id, x.device_id, x.module, x.summary, x.status::text AS status, x.finished_at, u.email
                    FROM config_plan x JOIN device d ON d.id = x.device_id
                    LEFT JOIN app_user u ON u.id = COALESCE(x.approved_by, x.created_by)
                    WHERE d.site_id = %(s)s{dev_f} AND x.finished_at >= %(a)s AND x.finished_at < %(b)s
                      AND x.status::text IN ('applied', 'failed', 'rolled_back')""", p):
            add(r["finished_at"], "change", r["status"], "info" if r["status"] == "applied" else "warning",
                device_id=str(r["device_id"]), text=r["summary"], a=r["module"], b=r["id"], user=r["email"])
    if "audit" in cats and not mac:
        targets = [f"site:{site_id}"] + ([f"device:{device_id}"] if device_id else [f"device:{d}" for d in devs])
        if device_id:
            targets = targets[1:]
        if targets:
            for r in tx.fetchall(
                    """SELECT x.at, x.action, x.target, u.email FROM audit_log x LEFT JOIN app_user u ON u.id = x.user_id
                       WHERE x.target = ANY(string_to_array(%(t)s, ',')) AND x.at >= %(a)s AND x.at < %(b)s
                         AND x.user_id IS NOT NULL""", {**p, "t": ",".join(targets)}):
                if r["action"].startswith(AUDIT_SKIP):
                    continue
                did = r["target"].split(":", 1)[1] if r["target"].startswith("device:") else None
                add(r["at"], "audit", r["action"], device_id=did, user=r["email"])
    out.sort(key=lambda e: e["at"], reverse=True)
    # graf: počty po úsecích, nejhorší závažnost v úseku
    step = (until - since) / BUCKETS
    buckets = [{"at": _iso(since + step * i), "n": 0, "sev": "info"} for i in range(BUCKETS)]
    for e in out:
        t = dt.datetime.fromisoformat(e["at"])
        i = min(BUCKETS - 1, max(0, int((t - since) / step)))
        b = buckets[i]
        b["n"] += 1
        if SEV_RANK[e["sev"]] > SEV_RANK[b["sev"]] and e["sev"] != "ok":
            b["sev"] = e["sev"]
    return {"since": _iso(since), "until": _iso(until), "minutes": span, "total": len(out), "events": out[:limit],
            "buckets": buckets, "step_seconds": int(step.total_seconds()),
            "devices": [{"id": k, "name": v} for k, v in sorted(devs.items(), key=lambda x: x[1].lower())]}
