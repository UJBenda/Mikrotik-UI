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
import os
import signal
import threading
import time

from ..auth import purge_expired_sessions
from ..plans import finish_plan
from ..config import Settings, get_settings
from ..db import Database, get_db, operator_scope
from ..rosapi import RosApi, RosApiError
from ..routeros import API_USER
from ..security import SecretBox, get_box
from ..services import audit, get_secret, sstp_username
from ..tunnel import TunnelAccount, TunnelProvider, build_providers

log = logging.getLogger("mtc.worker")

TICK = 30
RELEASES_EVERY = 24 * 3600  # changelogy RouterOS (download.mikrotik.com)
RECONCILE_EVERY = 600
OFFLINE_AFTER_MIN = 5
RESEND_AFTER_MIN = 10
MAX_ATTEMPTS = 3


def _close(api) -> None:
    close = getattr(api, "close", None)
    if close:
        close()


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
        failed = tx.fetchall(
            f"""UPDATE device_command SET status = 'failed', finished_at = now(), result = 'bez potvrzení'
                WHERE status = 'sent' AND sent_at < now() - interval '{RESEND_AFTER_MIN} minutes'
                  AND attempts >= {MAX_ATTEMPTS}
                RETURNING id, kind""")
        for f in failed:
            if f["kind"] == "apply_plan":
                finish_plan(tx, f["id"], False)
        return n + len(failed)


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
        finally:
            _close(api)
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


INVENTORY_EVERY_MIN = 5
BACKUP_EVERY_HOURS = 24
POOL_SIZE = 8
BACKUPS_PER_TICK = 4


def _due(db: Database, box: SecretBox, where: str, limit: int) -> list[tuple[dict, object]]:
    """Zařízení k obsloužení + jejich REST klient (hesla se dešifrují v jedné transakci)."""
    from ..services import device_api

    with operator_scope(db) as tx:
        devs = tx.fetchall(
            f"""SELECT id, tenant_id, serial, host(tunnel_ip) AS tunnel_ip, api_cert_fingerprint AS fp
                FROM device WHERE state = 'adopted' AND tunnel_ip IS NOT NULL
                  AND api_cert_fingerprint IS NOT NULL AND ({where})
                ORDER BY id LIMIT {int(limit)}""")
        return [(d, device_api(tx, box, d)) for d in devs]


def poll_inventory(db: Database, box: SecretBox, limit: int = 200) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from .. import inventory

    due = _due(db, box, f"inventory_at IS NULL OR inventory_at < now() - interval '{INVENTORY_EVERY_MIN} minutes'",
               limit)

    def one(item) -> bool:
        d, api = item
        try:
            data, errors = inventory.collect(api)
            inventory.store(db, d["tenant_id"], d["id"], data, errors)
            return True
        except Exception as e:  # zařízení nedostupné, pin nesedí apod.
            log.info("inventura %s selhala: %s", d["serial"], e)
            try:
                inventory.store_error(db, d["tenant_id"], d["id"], str(e))
            except Exception:
                log.exception("zápis chyby inventury selhal")
            return False

    if not due:
        return 0
    with ThreadPoolExecutor(max_workers=POOL_SIZE) as pool:
        return sum(pool.map(one, due))


def run_backups(db: Database, box: SecretBox, limit: int = BACKUPS_PER_TICK) -> int:
    from .. import backup

    due = _due(db, box, f"backup_at IS NULL OR backup_at < now() - interval '{BACKUP_EVERY_HOURS} hours'", limit)
    done = 0
    for d, api in due:
        api.timeout = 30.0
        try:
            try:
                text = backup.fetch_export(api)
            finally:
                _close(api)
            backup.store(db, box, d["tenant_id"], d["id"], text)
            done += 1
        except Exception as e:
            log.warning("záloha %s selhala: %s", d["serial"], e)
            # zkusit znovu až za hodinu, ne v každém ticku
            with operator_scope(db) as tx:
                tx.execute("UPDATE device SET backup_at = now() - interval '23 hours' WHERE id = %(d)s",
                           {"d": str(d["id"])})
    return done


UPDATE_CHECK_HOURS = 24


def check_updates(db: Database, box: SecretBox, limit: int = 20) -> int:
    """Nechá zařízení zjistit dostupnou verzi v jeho kanálu (zařízení se ptá
    upgrade.mikrotik.com samo). Výsledek přinese další inventura."""
    due = _due(db, box, f"update_checked_at IS NULL OR update_checked_at < now() - interval '{UPDATE_CHECK_HOURS} hours'",
               limit)
    n = 0
    for d, api in due:
        api.timeout = 45.0
        try:
            api.post("/system/package/update/check-for-updates", {})
            n += 1
        except Exception as e:
            log.info("kontrola aktualizací %s selhala: %s", d["serial"], e)
        finally:
            _close(api)
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET update_checked_at = now(), inventory_at = NULL WHERE id = %(d)s",
                       {"d": str(d["id"])})
    return n


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
    from .. import alerts, clients, heartbeat, inventory, notify, releases, routes, upgrades

    # -inf: monotonic() počítá od startu systému, 0.0 by po čerstvém bootu oddálilo první běh
    last_reconcile = last_rollup = last_releases = float("-inf")
    steps: dict[str, dict] = {}
    tick, started = 0, time.time()
    log.info("worker běží (%d koncentrátorů)", len(providers))
    while not stop.is_set():
        # každý krok zvlášť: chyba jednoho (např. nedostupný koncentrátor) nesmí zastavit ostatní
        for name, step in (("offline", lambda: mark_offline(db)),
                           ("requeue", lambda: requeue_stale(db)),
                           ("verify", lambda: verify_provisioning(db, box)),
                           ("inventory", lambda: poll_inventory(db, box)),
                           ("backup", lambda: run_backups(db, box)),
                           ("updates", lambda: check_updates(db, box)),
                           ("upgrades", lambda: upgrades.advance(db)),
                           ("alerts", lambda: alerts.reconcile(db)),
                           ("notify", lambda: notify.deliver(db, settings)),
                           ("routes", lambda: settings.tunnel.manage_routes and routes.sync(settings, providers))):
            t0 = time.monotonic()
            try:
                res = step()
                steps[name] = {"ms": round((time.monotonic() - t0) * 1000), "result": res if isinstance(res, (int, bool)) else None,
                               "error": None}
            except Exception as e:
                log.exception("krok %s selhal", name)
                steps[name] = {"ms": round((time.monotonic() - t0) * 1000), "result": None,
                               "error": f"{type(e).__name__}: {e}"[:300], "error_at": time.time()}
        tick += 1
        try:
            heartbeat.write(settings.paths.data_dir, {
                "at_epoch": time.time(), "started_epoch": started, "tick": tick, "pid": os.getpid(),
                "concentrators": [p.name for p in providers], "manage_routes": settings.tunnel.manage_routes,
                "steps": steps})
        except OSError as e:
            log.warning("stav workeru nejde zapsat: %s", e)
        now = time.monotonic()
        if now - last_rollup > 300:
            try:
                inventory.rollup(db)
                last_rollup = now
            except Exception:
                log.exception("agregace metrik selhala")
            try:
                clients.record(db)
            except Exception:
                log.exception("historie klientů selhala")
        if now - last_releases > RELEASES_EVERY:
            last_releases = now
            try:
                n = releases.sync(db)
                if n:
                    log.info("staženo %d changelogů RouterOS", n)
            except Exception:
                log.exception("stažení changelogů selhalo")
        if now - last_reconcile > RECONCILE_EVERY:
            try:
                reconcile_tunnels(db, box, providers)
                with operator_scope(db) as tx:
                    purge_expired_sessions(tx)
                inventory.retention(db)
                clients.purge(db)
            except Exception:
                log.exception("údržba selhala")
            last_reconcile = now
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
