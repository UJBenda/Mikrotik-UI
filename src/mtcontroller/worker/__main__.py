"""Worker: `python3 -m mtcontroller.worker` (systemd: mtc-worker.service).

Každých 30 s:
- zařízení bez check-inu > 5 min → offline,
- odeslané příkazy bez potvrzení > 10 min → znovu do fronty (max 3 pokusy),
- zařízení v provisioningu se známým otiskem certifikátu → ověření REST přes
  tunel (s pinningem) → adopted,
Každých 10 min:
- dorovnání účtů na koncentrátorech (založit chybějící, zakázat odebrané),
- úklid prošlých relací.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from ..auth import purge_expired_sessions
from ..config import Settings, get_settings
from ..db import Database, get_db, operator_scope
from ..rosapi import RosApi, RosApiError
from ..routeros import API_USER
from ..security import SecretBox, get_box
from ..services import audit, get_secret, sstp_username
from ..tunnel import TunnelAccount, TunnelProvider, build_providers

log = logging.getLogger("mtc.worker")

TICK = 30
RECONCILE_EVERY = 600
OFFLINE_AFTER_MIN = 5
RESEND_AFTER_MIN = 10
MAX_ATTEMPTS = 3


def mark_offline(db: Database) -> int:
    with operator_scope(db) as tx:
        rows = tx.fetchall(
            f"""UPDATE device SET state = 'offline'
                WHERE state = 'adopted' AND last_checkin_at < now() - interval '{OFFLINE_AFTER_MIN} minutes'
                RETURNING id, tenant_id""")
        for r in rows:
            audit(tx, "device.offline", tenant_id=r["tenant_id"], target=f"device:{r['id']}")
        return len(rows)


def requeue_stale(db: Database) -> int:
    with operator_scope(db) as tx:
        n = tx.execute(
            f"""UPDATE device_command SET status = 'queued'
                WHERE status = 'sent' AND sent_at < now() - interval '{RESEND_AFTER_MIN} minutes'
                  AND attempts < {MAX_ATTEMPTS}""")
        n += tx.execute(
            f"""UPDATE device_command SET status = 'failed', finished_at = now(), result = 'bez potvrzení'
                WHERE status = 'sent' AND sent_at < now() - interval '{RESEND_AFTER_MIN} minutes'
                  AND attempts >= {MAX_ATTEMPTS}""")
        return n


def verify_provisioning(db: Database, box: SecretBox, timeout: float = 8.0) -> int:
    """Zařízení v provisioningu: ověř, že REST přes tunel odpovídá s pinnutým
    certifikátem a servisním účtem. Teprve pak je zařízení adopted."""
    with operator_scope(db) as tx:
        devs = tx.fetchall(
            """SELECT id, tenant_id, serial, host(tunnel_ip) AS ip, api_cert_fingerprint AS fp
               FROM device WHERE state = 'provisioning' AND tunnel_ip IS NOT NULL
                 AND api_cert_fingerprint IS NOT NULL""")
        creds = {str(d["id"]): get_secret(tx, box, d["id"], "api_password") for d in devs}
    done = 0
    for d in devs:
        api = RosApi(f"https://{d['ip']}", API_USER, creds[str(d["id"])] or "", d["fp"], timeout=timeout)
        try:
            ident = api.get("/system/identity")
            res = api.get("/system/resource")
        except RosApiError as e:
            log.info("zařízení %s zatím přes tunel neodpovídá: %s", d["serial"], e)
            continue
        with operator_scope(db) as tx:
            tx.execute(
                """UPDATE device SET state = 'adopted', adopted_at = now(), identity = %(i)s,
                       model = coalesce(nullif(%(m)s, ''), model)
                   WHERE id = %(d)s AND state = 'provisioning'""",
                {"i": str((ident or {}).get("name", ""))[:200], "m": str((res or {}).get("board-name", ""))[:100],
                 "d": str(d["id"])})
            audit(tx, "device.adopted", tenant_id=d["tenant_id"], target=f"device:{d['id']}",
                  after={"identity": (ident or {}).get("name"), "tunnel_ip": d["ip"]})
        log.info("zařízení %s adoptováno (%s)", d["serial"], d["ip"])
        done += 1
    return done


def reconcile_tunnels(db: Database, box: SecretBox, providers: list[TunnelProvider]) -> None:
    if not providers:
        return
    with operator_scope(db) as tx:
        active = tx.fetchall(
            """SELECT id, tenant_id, site_id, serial, host(tunnel_ip) AS ip FROM device
               WHERE state IN ('provisioning', 'adopted', 'offline') AND tunnel_ip IS NOT NULL""")
        removed = [r["serial"] for r in tx.fetchall("SELECT serial FROM device WHERE state = 'removed'")]
        accounts = [TunnelAccount(sstp_username(d["serial"]), get_secret(tx, box, d["id"], "sstp_password") or "",
                                  d["ip"], f"{d['tenant_id']}/{d['site_id']}") for d in active]
    for p in providers:
        try:
            for a in accounts:
                if a.password:
                    p.ensure_account(a)
            for serial in removed:
                p.disable_account(sstp_username(serial))
        except Exception as e:
            log.warning("reconcile koncentrátoru %s selhal: %s", p.name, e)


def run(settings: Settings, db: Database, box: SecretBox, providers: list[TunnelProvider],
        stop: threading.Event) -> None:
    last_reconcile = 0.0
    log.info("worker běží (%d koncentrátorů)", len(providers))
    while not stop.is_set():
        try:
            mark_offline(db)
            requeue_stale(db)
            verify_provisioning(db, box)
            if time.monotonic() - last_reconcile > RECONCILE_EVERY:
                reconcile_tunnels(db, box, providers)
                with operator_scope(db) as tx:
                    purge_expired_sessions(tx)
                last_reconcile = time.monotonic()
        except Exception:
            log.exception("tick selhal")
        stop.wait(TICK)
    log.info("worker zastaven")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    s = get_settings()
    run(s, get_db(), get_box(), build_providers(s), stop)


if __name__ == "__main__":
    main()
