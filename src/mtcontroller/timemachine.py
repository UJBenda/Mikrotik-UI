"""Cestování v čase na mapě lokality a přeskakování MAC mezi porty.

Mapa v minulosti se skládá ze tří zdrojů:
- `infra_snapshot` – strom zařízení (kdo visí na kom a kterým portem), stav zařízení, porty a linky k internetu.
  Ukládá se jen při změně, takže stojí málo místa;
- `client_move` – kam se který klient přesunul (zařízení, port / SSID); poloha v čase T je poslední přesun před T;
- `client_session` – jestli byl klient v čase T vůbec připojený.

Z `client_move` se zároveň hlídá přeskakování MAC: drátový klient, který se opakovaně objevuje střídavě na různých
portech, znamená smyčku, duplicitní MAC nebo vadný switch po cestě.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from .db import Database, Tx, operator_scope, tenant_scope

log = logging.getLogger("mtcontroller.timemachine")
EVERY_S = 300                    # jak často se topologie lokality porovnává s posledním snímkem
KEEP_DAYS = 30
FLAP_WINDOW_MIN = 60
FLAP_MOVES = 4                   # tolik přesunů za okno = přeskakování
LOOP_MACS = 5                    # tolik přeskakujících MAC najednou = podezření na smyčku
_last: dict[str, float] = {}
_last_prune = 0.0
# co z uzlu mapy patří do snímku (bez vytížení a klientů – ti se skládají z přesunů)
_NODE_KEYS = ("id", "kind", "guess", "label", "model", "role", "state", "version", "stp_root", "stp_priority", "poe_w",
              "ports", "wans", "members", "parent", "level", "uplink_port", "parent_port")


def slim(view: dict) -> dict:
    """Mapa → snímek: jen struktura a stav, bez klientů a bez čísel, která se mění pořád (CPU, RAM)."""
    return {"nodes": [{k: n.get(k) for k in _NODE_KEYS if k in n} for n in view.get("nodes") or []],
            "externals": view.get("externals") or [], "wans": view.get("wans") or []}


def signature(snap: dict) -> str:
    stable = {"nodes": [{k: v for k, v in n.items() if k != "poe_w"} for n in snap["nodes"]], "externals": snap["externals"],
              "wans": snap.get("wans")}
    return hashlib.sha1(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def store(tx: Tx, tenant_id: Any, site_id: Any, view: dict) -> bool:
    """Uloží snímek, pokud se topologie od posledního změnila. Vrací, jestli uložil."""
    snap = slim(view)
    if not snap["nodes"]:
        return False
    sig = signature(snap)
    last = tx.fetchone("SELECT sig FROM infra_snapshot WHERE site_id = %(s)s ORDER BY at DESC LIMIT 1", {"s": str(site_id)})
    if last and last["sig"] == sig:
        return False
    tx.execute("INSERT INTO infra_snapshot (tenant_id, site_id, sig, data) VALUES (%(t)s, %(s)s, %(g)s, %(d)s)",
               {"t": str(tenant_id), "s": str(site_id), "g": sig, "d": json.loads(json.dumps(snap, default=str))})
    return True


def snapshot(db: Database, force: bool = False) -> int:
    """Krok workeru: lokalitám, které jsou na řadě, porovná mapu s posledním snímkem. Vrací počet nových snímků."""
    from . import infra

    global _last_prune
    with operator_scope(db) as tx:
        sites = tx.fetchall("""SELECT DISTINCT tenant_id, site_id FROM device
                               WHERE state IN ('adopted', 'offline') AND site_id IS NOT NULL""")
        if time.monotonic() - _last_prune > 3600:
            _last_prune = time.monotonic()
            # nejnovější snímek lokality zůstává vždy (topologie se nemusí měnit měsíce)
            tx.execute("""DELETE FROM infra_snapshot s WHERE s.at < now() - make_interval(days => %(d)s)
                          AND EXISTS (SELECT 1 FROM infra_snapshot n WHERE n.site_id = s.site_id AND n.at > s.at)""",
                       {"d": KEEP_DAYS})
            tx.execute("""DELETE FROM client_move m WHERE m.at < now() - make_interval(days => %(d)s)
                          AND EXISTS (SELECT 1 FROM client_move n WHERE n.site_id = m.site_id AND n.mac = m.mac AND n.at > m.at)""",
                       {"d": KEEP_DAYS})
    n = 0
    for s in sites:
        key = str(s["site_id"])
        if not force and time.monotonic() - _last.get(key, 0) < EVERY_S:
            continue
        _last[key] = time.monotonic()
        try:
            with tenant_scope(db, s["tenant_id"]) as tx:
                n += store(tx, s["tenant_id"], s["site_id"], infra.site_view(tx, s["site_id"]))
        except Exception:
            log.exception("lokalita %s: snímek topologie selhal", key)
    return n


def record_moves(tx: Tx, tenant_id: Any, site_id: Any, rows: list[dict]) -> int:
    """Po inventuře klientů zapíše, kdo je jinde než minule (nové zařízení nebo port; u Wi-Fi jiné AP)."""
    rows = [{"mac": r["mac"], "device_id": str(r["device_id"]), "port": r.get("port"), "ssid": r.get("ssid")}
            for r in rows if r.get("device_id")]
    if not rows:
        return 0
    return tx.execute(
        """INSERT INTO client_move (tenant_id, site_id, mac, device_id, port, ssid)
           SELECT %(t)s, %(s)s, x.mac, x.device_id::uuid, x.port, x.ssid
           FROM jsonb_to_recordset(%(rows)s) AS x(mac text, device_id text, port text, ssid text)
           LEFT JOIN LATERAL (SELECT device_id, port FROM client_move m WHERE m.site_id = %(s)s AND m.mac = x.mac
                              ORDER BY m.at DESC, m.id DESC LIMIT 1) l ON true
           WHERE l.device_id IS NULL OR l.device_id <> x.device_id::uuid OR l.port IS DISTINCT FROM x.port""",
        {"t": str(tenant_id), "s": str(site_id), "rows": rows}) or 0


def at(tx: Tx, site_id: Any, when: Any) -> dict | None:
    """Mapa lokality v čase `when`: poslední snímek topologie před ním + klienti, kteří tehdy byli připojení,
    na místě svého posledního přesunu. None = z té doby ještě žádný snímek není."""
    from . import clientmeta

    p = {"s": str(site_id), "t": when}
    snap = tx.fetchone("SELECT at, data FROM infra_snapshot WHERE site_id = %(s)s AND at <= %(t)s ORDER BY at DESC LIMIT 1", p)
    if not snap:
        return None
    data = snap["data"] if isinstance(snap["data"], dict) else json.loads(snap["data"])
    nodes = data.get("nodes") or []
    by_id = {n["id"]: n for n in nodes}
    by_label = {str(n.get("label")): n for n in nodes if n.get("kind") != "hub"}
    cl = [dict(r) for r in tx.fetchall(
        """SELECT s.mac, s.wifi, s.device AS device_name, s.port AS s_port, s.ssid AS s_ssid, s.vlan,
                  m.device_id, m.port, m.ssid, c.ip, c.hostname
           FROM client_session s
           LEFT JOIN LATERAL (SELECT device_id, port, ssid FROM client_move m WHERE m.site_id = s.site_id AND m.mac = s.mac
                              AND m.at <= %(t)s ORDER BY m.at DESC, m.id DESC LIMIT 1) m ON true
           LEFT JOIN client c ON c.site_id = s.site_id AND c.mac = s.mac
           WHERE s.site_id = %(s)s AND s.started_at <= %(t)s AND (s.ended_at IS NULL OR s.ended_at > %(t)s)
           ORDER BY s.mac""", p)]
    for n in nodes:
        n["clients"] = []
        n.setdefault("cpu", None)
        n.setdefault("mem", None)
    placed = []
    for c in cl:
        # bez záznamu o přesunu (starší data) platí místo, kde relace začala
        node = by_id.get(str(c["device_id"])) if c["device_id"] else by_label.get(str(c["device_name"]))
        if node is None:
            continue
        row = {"mac": c["mac"], "ip": c["ip"], "hostname": c["hostname"], "device_id": node["id"],
               "port": c["port"] if c["device_id"] else c["s_port"], "vlan": c["vlan"],
               "ssid": (c["ssid"] if c["device_id"] else c["s_ssid"]), "signal": None}
        node["clients"].append(row)
        placed.append(row)
    clientmeta.annotate(tx, placed)
    # klienti za odhadnutým nespravovaným switchem patří pod jeho uzel, ne pod zařízení nad ním
    for h in nodes:
        if h.get("kind") == "hub" and h.get("guess") and h.get("parent") in by_id:
            par = by_id[h["parent"]]
            mine = [c for c in par["clients"] if c.get("port") == h.get("parent_port") and not c.get("ssid")]
            h["clients"] = mine
            par["clients"] = [c for c in par["clients"] if c not in mine]
    return {"snapshot_at": snap["at"], "nodes": nodes, "externals": data.get("externals") or [],
            "wans": data.get("wans") or [], "warnings": []}


def alerts(tx: Tx) -> list[dict]:
    """Přeskakování MAC: drátový klient s aspoň FLAP_MOVES přesuny za hodinu, který se vrací na stejná místa.
    Víc takových klientů najednou v jedné lokalitě = podezření na smyčku."""
    rows = tx.fetchall(
        """SELECT m.tenant_id, m.site_id, m.mac, count(*) AS moves,
                  count(DISTINCT (m.device_id, m.port)) AS places,
                  (array_agg(m.device_id ORDER BY m.at DESC))[1] AS device_id,
                  string_agg(DISTINCT coalesce(nullif(d.identity, ''), d.serial) || ' ' || coalesce(m.port, '?'), ', ') AS where_,
                  coalesce(nullif(max(c.hostname), ''), m.mac) AS name
           FROM client_move m JOIN device d ON d.id = m.device_id
           LEFT JOIN client c ON c.site_id = m.site_id AND c.mac = m.mac
           WHERE m.at > now() - make_interval(mins => %(w)s) AND m.ssid IS NULL AND coalesce(m.port, '?') <> '?'
           GROUP BY m.tenant_id, m.site_id, m.mac
           HAVING count(*) >= %(n)s AND count(DISTINCT (m.device_id, m.port)) BETWEEN 2 AND count(*) - 1""",
        {"w": FLAP_WINDOW_MIN, "n": FLAP_MOVES})
    out, by_site = [], {}
    for r in rows:
        by_site.setdefault(str(r["site_id"]), []).append(r)
    for rs in by_site.values():
        if len(rs) >= LOOP_MACS:
            places = sorted({p for r in rs for p in str(r["where_"]).split(", ")})
            out.append({"tenant_id": rs[0]["tenant_id"], "device_id": rs[0]["device_id"], "kind": "loop_suspect",
                        "subject": "smyčka", "severity": "critical",
                        "message": f"podezření na smyčku v síti: {len(rs)} klientů přeskakuje mezi porty "
                                   f"({', '.join(places[:6])}{'…' if len(places) > 6 else ''})"})
            continue
        for r in rs:
            out.append({"tenant_id": r["tenant_id"], "device_id": r["device_id"], "kind": "mac_flap",
                        "subject": r["mac"], "severity": "warning",
                        "message": f"{r['name']} přeskakuje mezi porty ({r['where_']}) – {r['moves']}× za hodinu; "
                                   f"smyčka, dvě zařízení se stejnou MAC nebo vadný switch po cestě"})
    return out
