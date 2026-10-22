"""Detekce podvržených AP (jako Air Marshal v Meraki) a přehled sousedních Wi-Fi sítí.

Zdroj: sken rádií AP (`/interface/wifi/scan`, starší `/interface/wireless/scan`) přes REST.
Sken na chvíli přeruší klienty na rádiu, proto ho spouští správce ručně, nebo noční sken
(politika Wi-Fi lokality `rogue_scan: night`, mezi 2. a 5. hodinou místního času).

Třídění BSSID:
- own: rádio nebo virtuální AP spravovaného zařízení lokality (shoda MAC, nebo stejné
  poslední tři bajty – virtuální AP mají lokálně spravovanou MAC odvozenou od rádia),
- rogue: cizí BSSID, který vysílá SSID naší lokality (evil twin / podvržené AP) → alert,
- neighbor: cizí síť (soused) – jen pro přehled a plán kanálů.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from . import inventory
from .db import Database, Tx, operator_scope, tenant_scope
from .services import NotFound, ServiceError, audit, device_api

log = logging.getLogger("mtc.rogue")
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
SCAN_SECONDS = 8


def _mac(v: Any) -> str | None:
    m = str(v or "").strip().upper().replace("-", ":")
    return m if _MAC.match(m) else None


def _int(v: Any) -> int | None:
    m = re.search(r"-?\d+", str(v or ""))
    return int(m.group(0)) if m else None


def parse_scan(rows: Any) -> list[dict]:
    """Výstup skenu (wifi i wireless) → [{bssid, ssid, frequency, signal, security}]."""
    out = {}
    for r in rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []:
        if not isinstance(r, dict):
            continue
        bssid = _mac(r.get("address") or r.get("bssid") or r.get("mac-address"))
        if not bssid:
            continue
        ssid = str(r.get("ssid") or "")[:64]
        freq = _int(str(r.get("channel") or r.get("frequency") or "").split("/")[0])
        sig = _int(r.get("signal") or r.get("sig") or r.get("signal-strength"))
        sec = str(r.get("security") or r.get("auth") or "")[:80]
        prev = out.get(bssid)
        if prev is None or (sig is not None and (prev["signal"] is None or sig > prev["signal"])):
            out[bssid] = {"bssid": bssid, "ssid": ssid, "frequency": freq, "signal": sig, "security": sec}
    return list(out.values())


def own_macs(invs: list[dict]) -> set[str]:
    out = set()
    for inv in invs:
        for key, field in (("wifi", "mac-address"), ("wifi", "radio-mac"), ("wifi_radios", "radio-mac"),
                           ("wireless", "mac-address"), ("interfaces", "mac-address")):
            for r in inv.get(key) or []:
                m = _mac(r.get(field)) if isinstance(r, dict) else None
                if m:
                    out.add(m)
    return out


def own_ssids(invs: list[dict], extra: list[str]) -> set[str]:
    out = {s for s in extra if s}
    for inv in invs:
        for r in inv.get("wifi") or []:
            if isinstance(r, dict) and r.get("configuration.ssid"):
                out.add(str(r["configuration.ssid"]))
        for r in inv.get("wireless") or []:
            if isinstance(r, dict) and r.get("ssid"):
                out.add(str(r["ssid"]))
    return out


def classify(bss: dict, macs: set[str], ssids: set[str]) -> str:
    b = bss["bssid"]
    if b in macs or any(b[9:] == m[9:] for m in macs):
        return "own"
    if bss["ssid"] and bss["ssid"] in ssids:
        return "rogue"
    return "neighbor"


def _site_context(tx: Tx, site_id: Any) -> tuple[set[str], set[str]]:
    devs = tx.fetchall("SELECT id FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline')", {"s": str(site_id)})
    invs = [inventory.load(tx, d["id"], ["wifi", "wifi_radios", "wireless", "interfaces"]) for d in devs]
    names = [r["ssid"] for r in tx.fetchall("SELECT ssid FROM site_ssid WHERE site_id = %(s)s", {"s": str(site_id)})]
    return own_macs(invs), own_ssids(invs, names)


def radios_of(inv: dict) -> list[tuple[str, str]]:
    """[(menu, rozhraní)] – fyzická rádia zařízení (ne virtuální AP)."""
    out = []
    for r in inv.get("wifi") or []:
        if isinstance(r, dict) and r.get("name") and not r.get("master-interface") and str(r.get("disabled")) != "true":
            out.append(("wifi", str(r["name"])))
    for r in inv.get("wireless") or []:
        if isinstance(r, dict) and r.get("name") and str(r.get("interface-type") or "").lower() != "virtual" \
                and not r.get("master-interface") and str(r.get("disabled")) != "true":
            out.append(("wireless", str(r["name"])))
    return out


def scan_device(db: Database, box, tenant_id: Any, device_id: Any, user_id: Any = None, ip: str | None = None,
                reason: str = "manual") -> dict:
    """Sken rádií jednoho AP a uložení výsledku. Vrací souhrn."""
    with tenant_scope(db, tenant_id) as tx:
        d = tx.fetchone("""SELECT id, identity, serial, state, site_id, host(tunnel_ip) AS tunnel_ip,
                                  api_cert_fingerprint AS fp FROM device WHERE id = %(d)s""", {"d": str(device_id)})
        if not d:
            raise NotFound("zařízení neexistuje")
        if d["state"] != "adopted":
            raise ServiceError("zařízení není online")
        if not d["site_id"]:
            raise ServiceError("zařízení nemá lokalitu")
        inv = inventory.load(tx, device_id, ["wifi", "wireless"])
        radios = radios_of(inv)
        if not radios:
            raise ServiceError("zařízení nemá Wi-Fi rádia")
        audit(tx, "wifi.rogue_scan", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"radios": [r for _, r in radios], "reason": reason}, ip=ip)
        api = device_api(tx, box, d, timeout=SCAN_SECONDS + 15)
    found: list[dict] = []
    errors = []
    try:
        for menu, iface in radios:
            try:
                rows = api.post(f"/interface/{menu}/scan", {"numbers": iface, "duration": str(SCAN_SECONDS)})
                found += parse_scan(rows)
            except Exception as e:  # noqa: BLE001 – další rádio zkusit
                errors.append(f"{iface}: {str(e)[:150]}")
    finally:
        close = getattr(api, "close", None)
        if close:
            close()
    if not found and errors:
        raise ServiceError("sken selhal: " + "; ".join(errors))
    return store(db, tenant_id, d["site_id"], device_id, found) | {"errors": errors}


def store(db: Database, tenant_id: Any, site_id: Any, device_id: Any, found: list[dict]) -> dict:
    with tenant_scope(db, tenant_id) as tx:
        macs, ssids = _site_context(tx, site_id)
        counts = {"own": 0, "rogue": 0, "neighbor": 0}
        for b in found:
            kind = classify(b, macs, ssids)
            counts[kind] += 1
            tx.execute(
                """INSERT INTO wifi_bss (tenant_id, site_id, bssid, ssid, frequency, signal, security, kind, seen_by)
                   VALUES (%(t)s, %(s)s, %(b)s, %(ssid)s, %(f)s, %(sig)s, %(sec)s, %(k)s, %(d)s)
                   ON CONFLICT (site_id, bssid) DO UPDATE SET ssid = EXCLUDED.ssid, frequency = EXCLUDED.frequency,
                       signal = EXCLUDED.signal, security = EXCLUDED.security, kind = EXCLUDED.kind,
                       seen_by = EXCLUDED.seen_by, last_seen = now()""",
                {"t": str(tenant_id), "s": str(site_id), "b": b["bssid"], "ssid": b["ssid"], "f": b["frequency"],
                 "sig": b["signal"], "sec": b["security"], "k": kind, "d": str(device_id)})
    return {"found": len(found), **counts}


def site_list(tx: Tx, site_id: Any, days: int = 14) -> list[dict]:
    return tx.fetchall(
        """SELECT b.bssid, b.ssid, b.frequency, b.signal, b.security, b.kind, b.acknowledged,
                  b.first_seen, b.last_seen, b.seen_by::text AS seen_by, coalesce(d.identity, d.serial) AS seen_by_name
           FROM wifi_bss b LEFT JOIN device d ON d.id = b.seen_by
           WHERE b.site_id = %(s)s AND b.last_seen >= now() - make_interval(days => %(n)s)
           ORDER BY (b.kind = 'rogue' AND NOT b.acknowledged) DESC, b.kind DESC, b.signal DESC NULLS LAST""",
        {"s": str(site_id), "n": int(days)})


def acknowledge(db: Database, tenant_id: Any, site_id: Any, bssid: str, ack: bool, user_id: Any = None,
                ip: str | None = None) -> None:
    b = _mac(bssid)
    if not b:
        raise ServiceError("neplatné BSSID")
    with tenant_scope(db, tenant_id) as tx:
        n = tx.execute("UPDATE wifi_bss SET acknowledged = %(a)s WHERE site_id = %(s)s AND bssid = %(b)s",
                       {"a": bool(ack), "s": str(site_id), "b": b})
        if not n:
            raise NotFound("BSSID v lokalitě není")
        audit(tx, "wifi.rogue_ack", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"bssid": b, "acknowledged": bool(ack)}, ip=ip)


def nightly(db: Database, box, now: dt.datetime | None = None, tz: str | None = None) -> int:
    """Noční sken AP lokalit s `rogue_scan: night` (jednou za ~20 h na zařízení, 2–5 h místního času)."""
    from zoneinfo import ZoneInfo

    from . import regional

    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(ZoneInfo(tz or regional.timezone()))
    if not 2 <= local.hour < 5:
        return 0
    with operator_scope(db) as tx:
        rows = tx.fetchall(
            """SELECT d.id, d.tenant_id FROM device d
               JOIN site_policy p ON p.site_id = d.site_id AND p.module = 'intent'
               WHERE d.state = 'adopted' AND p.data->'wifi'->>'rogue_scan' = 'night'
                 AND NOT EXISTS (SELECT 1 FROM wifi_bss b WHERE b.seen_by = d.id AND b.last_seen > now() - interval '20 hours')
               ORDER BY d.id LIMIT 5""")
    n = 0
    for r in rows:
        try:
            with tenant_scope(db, r["tenant_id"]) as tx:
                inv = inventory.load(tx, r["id"], ["wifi", "wireless"])
            if not radios_of(inv):
                continue
            scan_device(db, box, r["tenant_id"], r["id"], reason="night")
            n += 1
        except Exception as e:  # noqa: BLE001
            log.info("noční sken %s selhal: %s", r["id"], e)
    return n
