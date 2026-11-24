"""Nespravované switche a cizí úseky: evidence portů, za kterými něco větví, a upozornění na nové.

Port spravovaného zařízení je „nespravovaný“, když za ním visí aspoň `MIN_CLIENTS` drátových klientů, nebo
drátový klient spolu se spravovaným zařízením (`shared`). Dva klienti na portu se nepočítají – bývá to
IP telefon s počítačem za ním.

První průchod lokality zapíše nalezené porty jako výchozí stav (`baseline`) a nic nehlásí. Co přibude později
a vydrží aspoň `SETTLE_MIN` minut, hlásí alert `unmanaged_switch`, dokud to správce neschválí nebo to nezmizí.
"""

from __future__ import annotations

from typing import Any

from .db import Tx
from .services import audit

MIN_CLIENTS = 3
SETTLE_MIN = 10        # jak dlouho musí nález vydržet, než se ohlásí (stěhování klienta, restart switche)
FORGET_HOURS = 24      # neschválený nález, který zmizel, se po této době zapomene
BASELINE_HOURS = 24    # jak dlouho po prvním průchodu lokality se nálezy berou jako výchozí stav


def detect(clients: list[dict]) -> dict[tuple[str, str], dict]:
    """Z výstupu clients.correlate: {(zařízení, port): {clients, shared}}."""
    out: dict[tuple[str, str], dict] = {}
    for c in clients:
        if not c.get("device_id") or c.get("wifi") or not c.get("port") or c.get("via_link"):
            continue
        e = out.setdefault((str(c["device_id"]), str(c["port"])), {"clients": 0, "shared": False})
        e["clients"] += 1
        e["shared"] = e["shared"] or bool(c.get("shared"))
    return {k: v for k, v in out.items() if v["shared"] or v["clients"] >= MIN_CLIENTS}


def record(tx: Tx, tenant_id: Any, site_id: Any, found: dict[tuple[str, str], dict], have_clients: bool = True) -> int:
    """Zapíše nález po inventuře klientů lokality. Vrací počet nově založených řádků.
    Výchozí stav se sbírá prvních `BASELINE_HOURS` hodin od prvního průchodu s daty (inventury nabíhají postupně)."""
    if not have_clients:
        return 0
    first = tx.fetchone("""SELECT unmanaged_scanned_at IS NULL AS first,
                                  unmanaged_scanned_at > now() - make_interval(hours => %(h)s) AS fresh
                           FROM site WHERE id = %(s)s""", {"s": str(site_id), "h": BASELINE_HOURS})
    baseline = bool(first and (first["first"] or first["fresh"]))
    new = 0
    for (dev, port), e in found.items():
        r = tx.fetchone(
            """INSERT INTO unmanaged_port (tenant_id, site_id, device_id, port, clients, shared, baseline)
               VALUES (%(t)s, %(s)s, %(d)s, %(p)s, %(n)s, %(sh)s, %(b)s)
               ON CONFLICT (device_id, port) DO UPDATE SET last_seen = now(), clients = EXCLUDED.clients,
                   shared = EXCLUDED.shared, site_id = EXCLUDED.site_id
               RETURNING (xmax = 0) AS inserted""",
            {"t": str(tenant_id), "s": str(site_id), "d": dev, "p": port[:100], "n": e["clients"], "sh": e["shared"],
             "b": baseline})
        new += 1 if r and r["inserted"] else 0
    tx.execute("""DELETE FROM unmanaged_port WHERE site_id = %(s)s AND NOT approved
                  AND last_seen < now() - make_interval(hours => %(h)s)""", {"s": str(site_id), "h": FORGET_HOURS})
    if first and first["first"]:
        tx.execute("UPDATE site SET unmanaged_scanned_at = now() WHERE id = %(s)s", {"s": str(site_id)})
    return new


_SQL = """SELECT u.id, u.device_id, u.port, u.clients, u.shared, u.baseline, u.approved, u.note, u.first_seen, u.last_seen,
                 u.tenant_id, coalesce(nullif(d.identity, ''), d.serial) AS device,
                 u.last_seen > now() - interval '30 minutes' AS present
          FROM unmanaged_port u JOIN device d ON d.id = u.device_id"""


def what(r: dict) -> str:
    n = r["clients"]
    kl = f"{n} klient" + ("" if n == 1 else "i" if 2 <= n <= 4 else "ů")
    return kl + (" a spravované zařízení" if r["shared"] else "")


def alerts(tx: Tx) -> list[dict]:
    """Nové, neschválené nálezy, které vydržely aspoň SETTLE_MIN minut a pořád trvají."""
    rows = tx.fetchall(_SQL + """ WHERE NOT u.approved AND NOT u.baseline AND d.state = 'adopted'
                                   AND u.last_seen > now() - interval '30 minutes'
                                   AND u.last_seen - u.first_seen >= make_interval(mins => %(m)s)""", {"m": SETTLE_MIN})
    return [{"tenant_id": r["tenant_id"], "device_id": r["device_id"], "kind": "unmanaged_switch", "subject": r["port"],
             "severity": "warning",
             "message": f"{r['device']}: na portu {r['port']} přibylo nespravované zařízení, které větví síť "
                        f"(switch, AP nebo spoj) – za portem: {what(r)}"} for r in rows]


def site_list(tx: Tx, site_id: Any) -> list[dict]:
    rows = tx.fetchall(_SQL + " WHERE u.site_id = %(s)s ORDER BY u.approved, u.baseline, device, u.port", {"s": str(site_id)})
    return [{"id": str(r["id"]), "device_id": str(r["device_id"]), "device": r["device"], "port": r["port"],
             "clients": r["clients"], "shared": r["shared"], "baseline": r["baseline"], "approved": r["approved"],
             "note": r["note"], "first_seen": r["first_seen"], "last_seen": r["last_seen"], "present": r["present"],
             "state": "approved" if r["approved"] else "known" if r["baseline"] else "new"} for r in rows]


def approve(tx: Tx, tenant_id: Any, uid: Any, approved: bool, note: str | None = None, user_id: Any = None,
            ip: str | None = None) -> bool:
    r = tx.fetchone("SELECT id, device_id, port, approved, note FROM unmanaged_port WHERE id = %(i)s", {"i": str(uid)})
    if not r:
        return False
    note = (str(note).strip()[:200] or None) if note is not None else r["note"]
    tx.execute("""UPDATE unmanaged_port SET approved = %(a)s, note = %(n)s,
                      approved_by = CASE WHEN %(a)s THEN %(u)s::uuid ELSE NULL END,
                      approved_at = CASE WHEN %(a)s THEN now() ELSE NULL END
                  WHERE id = %(i)s""", {"a": bool(approved), "n": note, "u": str(user_id) if user_id else None, "i": str(uid)})
    audit(tx, "site.unmanaged_approve" if approved else "site.unmanaged_unapprove", tenant_id=tenant_id, user_id=user_id,
          target=f"device:{r['device_id']}", before={"approved": r["approved"], "note": r["note"]},
          after={"port": r["port"], "approved": bool(approved), "note": note}, ip=ip)
    return True
