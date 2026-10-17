"""Notifikace alertů: obecný JSON webhook a bot Nextcloud Talk.

Nextcloud Talk (bot API, Nextcloud 27+): POST
  https://<cloud>/ocs/v2.php/apps/spreed/api/v1/bot/<token konverzace>/message
s hlavičkami X-Nextcloud-Talk-Bot-Random a X-Nextcloud-Talk-Bot-Signature
(HMAC-SHA256 tajemstvím bota nad random + text zprávy). Bot se v Nextcloudu
registruje příkazem `occ talk:bot:install` a povolí v konverzaci.

JSON webhook: tělo = alert v JSON, hlavička X-MTC-Signature: sha256=<HMAC těla>.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import ssl
import urllib.request

from .config import NotifyTarget, Settings
from .db import Database, operator_scope

log = logging.getLogger("mtc.notify")

_SEV = {"info": 0, "warning": 1, "critical": 2}
MAX_ATTEMPTS = 5
_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float = 10.0) -> int:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 (URL z konfigurace)
        return r.status


def format_text(alert: dict, resolved: bool) -> str:
    if resolved:
        return f"✅ Vyřešeno: {alert['message']}"
    return f"{_ICON.get(alert['severity'], '')} {alert['message']}"


def send(target: NotifyTarget, alert: dict, resolved: bool, post=_post) -> None:
    secret = target.secret().encode()
    if target.kind == "nextcloud_talk":
        text = format_text(alert, resolved)
        rnd = secrets.token_hex(32)
        sig = hmac.new(secret, (rnd + text).encode(), hashlib.sha256).hexdigest()
        body = json.dumps({"message": text, "referenceId": hashlib.sha256(
            f"mtc-{alert['id']}-{resolved}".encode()).hexdigest()}).encode()
        post(target.url, body, {"OCS-APIRequest": "true", "X-Nextcloud-Talk-Bot-Random": rnd,
                                "X-Nextcloud-Talk-Bot-Signature": sig})
    else:
        body = json.dumps({"alert": alert, "resolved": resolved, "text": format_text(alert, resolved)},
                          default=str, ensure_ascii=False).encode()
        sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
        post(target.url, body, {"X-MTC-Signature": f"sha256={sig}"})


def _targets_for(settings: Settings, alert: dict) -> list[NotifyTarget]:
    return [t for t in settings.notify
            if _SEV[alert["severity"]] >= _SEV[t.min_severity]
            and (not t.tenants or str(alert["tenant_id"]) in t.tenants)]


def deliver(db: Database, settings: Settings, post=_post) -> int:
    """Pošle čekající notifikace (otevření i uzavření). Vrací počet odeslaných."""
    if not settings.notify:
        return 0
    with operator_scope(db) as tx:
        pending = tx.fetchall(
            f"""SELECT a.id, a.tenant_id, a.device_id, a.kind, a.subject, a.severity::text AS severity,
                       a.message, a.opened_at, a.resolved_at, a.notified_open, a.notified_close
                FROM alert a
                WHERE a.notify_attempts < {MAX_ATTEMPTS}
                  AND ((NOT a.notified_open AND a.resolved_at IS NULL)
                       OR (a.notified_open AND NOT a.notified_close AND a.resolved_at IS NOT NULL))
                ORDER BY a.id LIMIT 50""")
    sent = 0
    for a in pending:
        resolved = a["resolved_at"] is not None
        targets = _targets_for(settings, a)
        ok = True
        for t in targets:
            try:
                send(t, a, resolved, post)
            except Exception as e:  # síť, 4xx/5xx
                ok = False
                log.warning("notifikace %s přes %s selhala: %s", a["id"], t.name, e)
        with operator_scope(db) as tx:
            if ok:
                col = "notified_close" if resolved else "notified_open"
                tx.execute(f"UPDATE alert SET {col} = true WHERE id = %(i)s", {"i": a["id"]})
                sent += 1 if targets else 0
            else:
                tx.execute("UPDATE alert SET notify_attempts = notify_attempts + 1 WHERE id = %(i)s", {"i": a["id"]})
    return sent


def test_message(settings: Settings, name: str) -> None:
    """Zkušební zpráva (mtc-admin notify-test NAME)."""
    t = next((x for x in settings.notify if x.name == name), None)
    if not t:
        raise ValueError(f"cíl notifikací {name!r} není v konfiguraci")
    send(t, {"id": 0, "tenant_id": "", "severity": "info", "message": "MikroTik Controller: zkušební notifikace"},
         False)
