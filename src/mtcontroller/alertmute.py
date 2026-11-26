"""Ztlumení upozornění na čas.

Ztlumené upozornění zůstává vidět (ve skupině „Ztlumené“), ale nepočítá se do zvonku ani do „K řešení“
a neposílá se (e-mail, webhook, Talk). Když po skončení ztlumení ještě trvá, odešle se – stejně jako
u servisního okna. Ztlumení je pravidlo (druh + volitelně zařízení a předmět), takže platí i pro alert,
který se mezitím zavřel a znovu otevřel.
"""

from __future__ import annotations

from typing import Any

from .db import Tx
from .services import NotFound, ServiceError, audit

HOURS = (1, 8, 24, 168, 720)
SCOPES = ("alert", "device", "kind")

# podmínka „alert a je právě ztlumený“ / čas, do kdy (pro SELECT nad alert a)
MATCH = """m.tenant_id = a.tenant_id AND m.until > now() AND m.kind = a.kind
           AND (m.device_id IS NULL OR m.device_id = a.device_id) AND (m.subject IS NULL OR m.subject = a.subject)"""
IS_MUTED = f"EXISTS (SELECT 1 FROM alert_mute m WHERE {MATCH})"
MUTED_UNTIL = f"(SELECT max(m.until) FROM alert_mute m WHERE {MATCH})"
MUTE_ID = f"(SELECT m.id FROM alert_mute m WHERE {MATCH} ORDER BY m.until DESC LIMIT 1)"


def create(tx: Tx, tenant_id: str, alert_id: int, scope: str, hours: int, *, user_id: Any, ip: str | None) -> dict:
    """Ztlumí podle existujícího alertu: jen tenhle (zařízení + druh + předmět), druh na zařízení, nebo druh všude."""
    if scope not in SCOPES:
        raise ServiceError("rozsah ztlumení musí být alert, device nebo kind")
    if hours not in HOURS:
        raise ServiceError("délka ztlumení musí být 1, 8, 24, 168 nebo 720 hodin")
    a = tx.fetchone("SELECT device_id, kind, subject FROM alert WHERE id = %(i)s", {"i": alert_id})
    if not a:
        raise NotFound("alert neexistuje")
    tx.execute("DELETE FROM alert_mute WHERE until < now()")
    r = tx.fetchone(
        """INSERT INTO alert_mute (tenant_id, device_id, kind, subject, until, created_by)
           VALUES (%(t)s, %(d)s, %(k)s, %(s)s, now() + make_interval(hours => %(h)s), %(u)s) RETURNING id, until""",
        {"t": tenant_id, "d": None if scope == "kind" else str(a["device_id"]), "k": a["kind"],
         "s": a["subject"] if scope == "alert" else None, "h": hours, "u": user_id})
    audit(tx, "alert.mute", tenant_id=tenant_id, user_id=user_id, target=f"alert:{alert_id}",
          after={"scope": scope, "hours": hours, "kind": a["kind"]}, ip=ip)
    return {"id": r["id"], "until": r["until"]}


def delete(tx: Tx, tenant_id: str, mute_id: int, *, user_id: Any, ip: str | None) -> None:
    r = tx.fetchone("DELETE FROM alert_mute WHERE id = %(i)s RETURNING kind", {"i": mute_id})
    if not r:
        raise NotFound("ztlumení neexistuje")
    audit(tx, "alert.unmute", tenant_id=tenant_id, user_id=user_id, target=f"mute:{mute_id}", before={"kind": r["kind"]}, ip=ip)
