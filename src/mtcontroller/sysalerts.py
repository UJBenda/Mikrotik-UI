"""Alerty provozovatele (ne tenanta): zálohy controlleru a jejich kopie v cloudu.

Alerty v tabulce `alert` patří zařízením tenantů; tyhle jsou o controlleru samotném, proto
jen stav v souboru workeru (`system-alerts.json`) a notifikace cílům bez omezení na tenanty
(`[[notify]]` s prázdným `tenants`). Otevření i vyřešení se pošle jednou.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import backupplan, notify, sysbackup
from .db import Database, operator_scope

log = logging.getLogger("mtcontroller.sysalerts")
FILE = "system-alerts.json"
GRACE_HOURS = 6       # rezerva nad plánovanou pauzou mezi zálohami
TARGET_FAILS = 2      # cíl hlásit až po druhém neúspěšném pokusu


def conditions(db: Database, settings, now: dt.datetime | None = None) -> dict[str, dict]:
    """{klíč: {severity, message}} aktuálně platných problémů."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out: dict[str, dict] = {}
    plan = backupplan.load(db, settings.backup.keep)
    files = sysbackup.listing(settings.backup.dir)
    st = sysbackup.read_status(settings.paths.data_dir)
    if not plan["enabled"]:
        out["backup_disabled"] = {"severity": "warning",
                                  "message": "Plánované zálohy controlleru jsou vypnuté (Systém → Zálohy)"}
    else:
        limit = backupplan.max_gap_hours(plan) + GRACE_HOURS
        last = dt.datetime.fromisoformat(files[0]["at"]) if files else None
        if last is None:
            # čerstvá instalace: hlásit až když měla první záloha dávno proběhnout
            st_at = dt.datetime.fromisoformat(st["at"]) if st.get("at") else None
            if st_at and now - st_at > dt.timedelta(hours=limit):
                out["backup_stale"] = {"severity": "critical", "message": "Controller nemá žádnou zálohu"}
        elif now - last > dt.timedelta(hours=limit):
            h = int((now - last).total_seconds() // 3600)
            out["backup_stale"] = {"severity": "critical",
                                   "message": f"Poslední záloha controlleru je {h} h stará ({files[0]['name']})"}
    if st.get("state") == "error":
        out["backup_failed"] = {"severity": "critical",
                                "message": f"Záloha controlleru selhala: {st.get('error') or '?'}"}
    with operator_scope(db) as tx:
        targets = tx.fetchall("SELECT id, kind, name, status FROM backup_target WHERE enabled")
    for t in targets:
        s = t["status"] or {}
        key = f"cloud_{t['id']}"
        if t["kind"] != "nextcloud" and not s.get("authorized"):
            out[key] = {"severity": "warning",
                        "message": f"Cíl záloh {t['name']}: není přihlášený (Systém → Zálohy → Přihlásit)"}
        elif s.get("last_error") and int(s.get("attempts") or 0) >= TARGET_FAILS:
            out[key] = {"severity": "warning",
                        "message": f"Cíl záloh {t['name']}: kopie se nedaří ({s['last_error']})"}
    return out


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _send(settings, key: str, a: dict, resolved: bool, post, smtp) -> bool:
    alert = {"id": int(hashlib.sha256(key.encode()).hexdigest()[:8], 16), "tenant_id": "", "device_id": "",
             "kind": key.split("_")[0] + "_system", "severity": a["severity"], "message": a["message"],
             "opened_at": a.get("opened_at"), "resolved_at": a.get("resolved_at")}
    ok = True
    for t in settings.notify:
        if t.tenants or notify._SEV[a["severity"]] < notify._SEV[t.min_severity]:
            continue
        try:
            notify.send(t, alert, resolved, post, smtp)
        except Exception as e:  # noqa: BLE001
            ok = False
            log.warning("notifikace %s přes %s selhala: %s", key, t.name, e)
    return ok


def reconcile(db: Database, settings, now: dt.datetime | None = None, post=notify._post,
              smtp=notify._smtp_send) -> dict[str, Any]:
    """Krok workeru: otevře/uzavře alerty provozovatele a pošle notifikace. Vrací aktivní."""
    now = now or dt.datetime.now(dt.timezone.utc)
    path = settings.paths.data_dir / FILE
    state = _load(path)
    active = conditions(db, settings, now)
    for key, a in active.items():
        cur = state.get(key)
        if cur and not cur.get("resolved_at"):
            cur.update(message=a["message"], severity=a["severity"])
        else:
            state[key] = cur = {**a, "opened_at": now.isoformat(), "notified": False}
        if not cur["notified"] and _send(settings, key, cur, False, post, smtp):
            cur["notified"] = True
    for key, cur in list(state.items()):
        if key in active or cur.get("resolved_at"):
            continue
        cur["resolved_at"] = now.isoformat()
        if cur.get("notified"):
            _send(settings, key, cur, True, post, smtp)
    # vyřešené držet den (UI), pak zahodit
    state = {k: v for k, v in state.items()
             if not v.get("resolved_at") or now - dt.datetime.fromisoformat(v["resolved_at"]) < dt.timedelta(days=1)}
    try:
        _save(path, state)
    except OSError as e:
        log.warning("stav alertů provozovatele nejde zapsat: %s", e)
    return {k: v for k, v in state.items() if not v.get("resolved_at")}


def read(settings) -> dict:
    return {k: v for k, v in _load(settings.paths.data_dir / FILE).items() if not v.get("resolved_at")}
