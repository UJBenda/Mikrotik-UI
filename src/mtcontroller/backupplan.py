"""Plán záloh controlleru nastavitelný v administraci (Systém → Zálohy).

Uložený v `system_setting` (klíč `backup`), jen provozovatel. Timer `mtc-backup.timer` běží
každých 15 minut a `mtc-admin backup --scheduled` podle plánu sám pozná, jestli je čas –
root tak při změně plánu nemusí přepisovat systemd. Čas je místní čas serveru (průvodce
nastaví časovou zónu). Zmeškaný termín (server byl vypnutý) se dožene týž den.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .db import Database, operator_scope
from .services import ServiceError, audit

KEY = "backup"
DAYS = (1, 2, 3, 4, 5, 6, 7)  # ISO: 1 = pondělí
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
RETRY_AFTER = dt.timedelta(hours=1)


def defaults(keep: int = 14) -> dict:
    return {"enabled": True, "time": "02:30", "days": list(DAYS), "keep": keep}


def validate(data: dict, keep_default: int = 14) -> dict:
    out = defaults(keep_default)
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    if "time" in data:
        t = str(data["time"]).strip()
        if not _TIME.match(t):
            raise ServiceError("čas zálohy ve tvaru HH:MM")
        out["time"] = t
    if "days" in data:
        try:
            days = sorted({int(d) for d in data["days"]})
        except (TypeError, ValueError):
            raise ServiceError("dny v týdnu 1–7") from None
        if not days or any(d not in DAYS for d in days):
            raise ServiceError("vyber aspoň jeden den v týdnu")
        out["days"] = days
    if "keep" in data:
        try:
            keep = int(data["keep"])
        except (TypeError, ValueError):
            raise ServiceError("počet záloh 1–365") from None
        if not 1 <= keep <= 365:
            raise ServiceError("počet záloh 1–365")
        out["keep"] = keep
    return out


def load(db: Database, keep_default: int = 14) -> dict:
    with operator_scope(db) as tx:
        r = tx.fetchone("SELECT data FROM system_setting WHERE key = %(k)s", {"k": KEY})
    try:
        return validate(r["data"] if r else {}, keep_default)
    except ServiceError:
        return defaults(keep_default)


def save(db: Database, data: dict, user_id: Any = None, ip: str | None = None, keep_default: int = 14) -> dict:
    plan = validate(data, keep_default)
    with operator_scope(db) as tx:
        before = tx.fetchone("SELECT data FROM system_setting WHERE key = %(k)s", {"k": KEY})
        tx.execute("""INSERT INTO system_setting (key, data) VALUES (%(k)s, %(d)s)
                      ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                   {"k": KEY, "d": plan})
        audit(tx, "system.backup_plan", user_id=user_id, target="system:backup",
              before=before["data"] if before else None, after=plan, ip=ip)
    return plan


def slot(plan: dict, now: dt.datetime) -> dt.datetime | None:
    """Dnešní termín zálohy (místní čas `now`), pokud je dnes den zálohy a už nastal."""
    if not plan["enabled"] or now.isoweekday() not in plan["days"]:
        return None
    h, m = (int(x) for x in plan["time"].split(":"))
    s = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return s if now >= s else None


def due(plan: dict, now: dt.datetime, last_backup: dt.datetime | None, last_error_at: dt.datetime | None = None) -> bool:
    """Je čas na plánovanou zálohu? Hotová záloha po termínu (i ruční) ho splní;
    po chybě se zkouší znovu nejdřív za hodinu."""
    s = slot(plan, now)
    if s is None:
        return False
    if last_backup and last_backup >= s:
        return False
    if last_error_at and last_error_at >= s and now - last_error_at < RETRY_AFTER:
        return False
    return True


def max_gap_hours(plan: dict) -> int:
    """Nejdelší očekávaná pauza mezi zálohami (pro hlídání stáří poslední zálohy)."""
    if not plan["enabled"]:
        return 0
    days = plan["days"]
    gaps = [((days[(i + 1) % len(days)] - d) % 7) or 7 for i, d in enumerate(days)]
    return max(gaps) * 24
