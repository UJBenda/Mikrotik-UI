"""Test rychlosti mezi dvěma spravovanými zařízeními (RouterOS bandwidth-test přes REST v tunelu).

Postup (vše s pinningem certifikátu, nic nezůstane):
1. cíl: dočasný uživatel `mtc-btest` (náhodné heslo jen v paměti, povolený jen ze zdrojové adresy)
   a zapnutý bandwidth-server s ověřením – předchozí stav serveru se zapamatuje,
2. zdroj: `/tool/bandwidth-test` na zvolenou adresu cíle po dobu testu,
3. cíl: uživatel pryč, bandwidth-server zpět do původního stavu (i při chybě).

Test vytíží linku i CPU (malé routery CPU limituje víc než linka) a spotřebuje data: zhruba
rychlost × doba (oba směry sčítat) – UI ukáže odhad předem.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from typing import Any

from .db import Database, Tx, tenant_scope
from .security import random_password
from .services import NotFound, ServiceError, _s, audit, device_api

log = logging.getLogger("mtc.bwtest")

USER = "mtc-btest"
DURATIONS = (5, 10, 20, 30)
DIRECTIONS = ("both", "transmit", "receive")
PROTOCOLS = ("tcp", "udp")
_UNITS = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}


def parse_bps(v: Any) -> float | None:
    """'94.2Mbps' | '94215360' | 94215360 → bit/s."""
    if v is None or v == "":
        return None
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)(?:bps|b/s)?\s*", str(v))
    if not m:
        return None
    return float(m.group(1)) * _UNITS[m.group(2).lower()]


def _pct(v: Any) -> int | None:
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def summarize(rows: list[dict]) -> dict:
    """Vzorky ze zdroje → {samples: [{t, tx, rx}], tx_avg, rx_avg, tx_max, rx_max, cpu_local, cpu_remote, lost}."""
    samples = []
    for i, r in enumerate(x for x in rows if isinstance(x, dict)):
        samples.append({"t": i + 1, "tx": parse_bps(r.get("tx-current")), "rx": parse_bps(r.get("rx-current")),
                        "cpu_l": _pct(r.get("local-cpu-load")), "cpu_r": _pct(r.get("remote-cpu-load"))})
    last = next((r for r in reversed(rows) if isinstance(r, dict)), {})
    tx = [s["tx"] for s in samples if s["tx"] is not None]
    rx = [s["rx"] for s in samples if s["rx"] is not None]
    return {
        "samples": samples,
        "tx_avg": parse_bps(last.get("tx-total-average")) or (sum(tx) / len(tx) if tx else None),
        "rx_avg": parse_bps(last.get("rx-total-average")) or (sum(rx) / len(rx) if rx else None),
        "tx_max": max(tx) if tx else None, "rx_max": max(rx) if rx else None,
        "cpu_local": max((s["cpu_l"] for s in samples if s["cpu_l"] is not None), default=None),
        "cpu_remote": max((s["cpu_r"] for s in samples if s["cpu_r"] is not None), default=None),
        "lost": str(last.get("lost-packets") or "") or None,
        "status": str(last.get("status") or "") or None,
    }


def target_addresses(inv: dict) -> list[dict]:
    """Adresy cíle vhodné pro test (bez tunelu controlleru a bez dynamických link-local)."""
    out = []
    for a in inv.get("addresses") or []:
        if not isinstance(a, dict) or str(a.get("disabled")).lower() in ("true", "yes"):
            continue
        iface = str(a.get("interface") or "")
        if iface.startswith("mtc"):
            continue
        try:
            ip = ipaddress.ip_interface(str(a.get("address"))).ip
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        out.append({"address": str(ip), "interface": iface})
    return out


def _dev(tx: Tx, did: Any) -> dict:
    d = tx.fetchone("""SELECT id, identity, serial, state, site_id, host(tunnel_ip) AS tunnel_ip,
                              api_cert_fingerprint AS fp FROM device WHERE id = %(d)s""", {"d": str(did)})
    if not d:
        raise NotFound("zařízení neexistuje")
    if d["state"] != "adopted":
        raise ServiceError(f"{d['identity'] or d['serial']} není online")
    return d


def start(db: Database, box, tenant_id: Any, from_id: Any, to_id: Any, address: str, duration: int = 10,
          direction: str = "both", protocol: str = "tcp", user_id: Any = None, ip: str | None = None,
          run_async: bool = True) -> dict:
    if str(from_id) == str(to_id):
        raise ServiceError("vyber dvě různá zařízení")
    if duration not in DURATIONS or direction not in DIRECTIONS or protocol not in PROTOCOLS:
        raise ServiceError("neplatné parametry testu")
    try:
        addr = str(ipaddress.IPv4Address(str(address)))
    except ValueError:
        raise ServiceError("neplatná adresa cíle") from None
    from . import inventory

    with tenant_scope(db, tenant_id) as tx:
        a, b = _dev(tx, from_id), _dev(tx, to_id)
        if addr not in {x["address"] for x in target_addresses(inventory.load(tx, b["id"], ["addresses"]))}:
            raise ServiceError("adresa nepatří cílovému zařízení (podle inventury)")
        if tx.fetchone("""SELECT 1 FROM bw_test WHERE status = 'running' AND started_at > now() - interval '5 minutes'
                          AND (from_device IN (%(a)s, %(b)s) OR to_device IN (%(a)s, %(b)s))""",
                       {"a": str(a["id"]), "b": str(b["id"])}):
            raise ServiceError("na jednom ze zařízení už test běží")
        params = {"duration": duration, "direction": direction, "protocol": protocol}
        row = tx.fetchone(
            """INSERT INTO bw_test (tenant_id, from_device, to_device, address, params, created_by)
               VALUES (%(t)s, %(a)s, %(b)s, %(ad)s, %(p)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "a": str(a["id"]), "b": str(b["id"]), "ad": addr, "p": params, "u": _s(user_id)})
        audit(tx, "device.bwtest", tenant_id=tenant_id, user_id=user_id, target=f"device:{a['id']}",
              after={"test_id": row["id"], "to": str(b["id"]), "address": addr, **params}, ip=ip)
    job = (db, box, tenant_id, row["id"], a, b, addr, params)
    if run_async:
        threading.Thread(target=_run, args=job, name=f"bwtest-{row['id']}", daemon=True).start()
    else:
        _run(*job)
    return {"id": row["id"], "status": "running"}


def _source_ip(api_a, addr: str) -> str | None:
    """Adresa, ze které zdroj na cíl půjde (pro omezení dočasného uživatele) – route lookup na zdroji."""
    try:
        r = api_a.post("/ip/route/check", {"dst-ip": addr, "once": "yes"})
        r = r[0] if isinstance(r, list) and r else r
        src = (r or {}).get("src") or (r or {}).get("pref-src")
        return str(ipaddress.IPv4Address(str(src))) if src else None
    except Exception:
        return None


def _run(db: Database, box, tenant_id: Any, test_id: int, a: dict, b: dict, addr: str, params: dict) -> None:
    api_a = api_b = None
    prev: dict | None = None
    user_added = False
    status, result, error = "failed", None, None
    try:
        with tenant_scope(db, tenant_id) as tx:
            api_a = device_api(tx, box, a, timeout=10.0)
            api_b = device_api(tx, box, b, timeout=10.0)
        pw = random_password()
        src = _source_ip(api_a, addr)
        prev = api_b.get("/tool/bandwidth-server") or {}
        if isinstance(prev, list):
            prev = prev[0] if prev else {}
        if any(u.get("name") == USER for u in (api_b.get("/user") or []) if isinstance(u, dict)):
            api_b.post("/user/remove", {"numbers": USER})  # zbytek po přerušeném testu
        body = {"name": USER, "group": "mtc", "password": pw, "comment": "mtc: docasny ucet testu rychlosti"}
        if src:
            body["address"] = src + "/32"
        api_b.put("/user", body)
        user_added = True
        api_b.post("/tool/bandwidth-server/set", {"enabled": "yes", "authenticate": "yes"})
        api_a.timeout = params["duration"] + 25
        rows = api_a.post("/tool/bandwidth-test", {
            "address": addr, "user": USER, "password": pw, "duration": f"{params['duration']}s",
            "direction": params["direction"], "protocol": params["protocol"]})
        result = summarize(rows if isinstance(rows, list) else [rows])
        if not result["samples"] or (result["tx_avg"] is None and result["rx_avg"] is None):
            error = f"test nedal výsledek ({result.get('status') or 'bez dat'}) – firewall cíle (TCP 2000) nebo CPU"
        else:
            status = "done"
    except Exception as e:
        error = str(e)[:400]
        if "2000" in error or "connection" in error.lower() or "refused" in error.lower():
            error += " – zkontroluj firewall na cíli (bandwidth-test TCP 2000, UDP 2001+)"
    finally:
        # úklid na cíli vždy
        if api_b is not None:
            try:
                if user_added:
                    api_b.post("/user/remove", {"numbers": USER})
                if prev is not None:
                    yes = lambda k, d: "yes" if str(prev.get(k, d)).lower() in ("true", "yes") else "no"  # noqa: E731
                    api_b.post("/tool/bandwidth-server/set", {"enabled": yes("enabled", "false"),
                                                              "authenticate": yes("authenticate", "true")})
            except Exception as e:
                log.warning("bwtest %s: úklid na cíli selhal: %s", test_id, e)
                error = (error + "; " if error else "") + f"úklid na cíli selhal ({e}) – zkontroluj /user mtc-btest"
        for api in (api_a, api_b):
            close = getattr(api, "close", None)
            if close:
                close()
        try:
            with tenant_scope(db, tenant_id) as tx:
                tx.execute("""UPDATE bw_test SET status = %(s)s, result = %(r)s, error = %(e)s, finished_at = now()
                              WHERE id = %(i)s""", {"s": status, "r": result, "e": error, "i": test_id})
        except Exception:
            log.exception("bwtest %s: zápis výsledku selhal", test_id)


def list_tests(tx: Tx, device_id: Any, limit: int = 20) -> list[dict]:
    # nedokončený test po 5 minutách = selhal (web se mezitím restartoval)
    tx.execute("""UPDATE bw_test SET status = 'failed', error = 'test nedoběhl (restart controlleru?)', finished_at = now()
                  WHERE status = 'running' AND started_at < now() - interval '5 minutes'""")
    return tx.fetchall(
        """SELECT t.id, t.from_device, t.to_device, t.address, t.params, t.status, t.result, t.error, t.started_at,
                  t.finished_at, fa.identity AS from_name, fb.identity AS to_name
           FROM bw_test t JOIN device fa ON fa.id = t.from_device JOIN device fb ON fb.id = t.to_device
           WHERE t.from_device = %(d)s OR t.to_device = %(d)s ORDER BY t.id DESC LIMIT %(n)s""",
        {"d": str(device_id), "n": int(limit)})
