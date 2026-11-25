"""Přehled lokality: souhrn brány a linky, odezva internetu, nejvytíženější AP a klienti, co je pod dohledem.

Jen čte, co už sbírají jiné moduly (inventury, hodinové agregace provozu, klienti, spoje, zdraví portů, alerty,
`wanprobe`). Co se neměří, vrací se jako None / prázdný seznam a rozhraní to nezobrazí.
"""
from __future__ import annotations

from typing import Any

from .db import Tx

WIFI_IFACE_RE = r"^(wifi|wlan|cap)"
_BYTES = "sum((coalesce(h.rx_bps_avg, 0) + coalesce(h.tx_bps_avg, 0)) * 450)::bigint"   # bps × 3600 s / 8


def site_dash(tx: Tx, site_id: Any, hours: int = 24) -> dict:
    from . import infra, inventory, porthealth, wanprobe, wlink

    p = {"s": str(site_id), "h": int(hours)}
    devs = tx.fetchall(
        """SELECT id, coalesce(nullif(identity, ''), serial) AS name, model, ros_version, role::text AS role,
                  state::text AS state, host(tunnel_ip) AS ip
           FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline') ORDER BY created_at""", p)
    names = {str(d["id"]): d["name"] for d in devs}
    counts: dict[str, int] = {}
    for d in devs:
        counts[d["role"]] = counts.get(d["role"], 0) + 1
    counts["links"] = int(tx.fetchone("SELECT count(*) AS n FROM wlink WHERE site_id = %(s)s AND kind <> 'foreign'", p)["n"])
    counts["clients"] = int(tx.fetchone(
        "SELECT count(*) AS n FROM client WHERE site_id = %(s)s AND last_seen > now() - interval '15 minutes'", p)["n"])
    counts["offline"] = sum(1 for d in devs if d["state"] == "offline")

    gw = next((d for d in devs if d["role"] == "gateway"), None)
    gateway = None
    if gw:
        inv = inventory.load(tx, gw["id"], ["resource", "addresses", "dhcp_client", "routes_default"])
        res = (inv.get("resource") or [{}])
        res = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
        wans = [{"interface": w["interface"], "address": w["address"], "gateway": w["gateway"], "active": bool(w["active"]),
                 "comment": w.get("comment")} for w in infra._wans(inv) if w.get("interface")]
        for w in wans:
            w["month_bytes"] = int(tx.fetchone(
                f"""SELECT coalesce({_BYTES}, 0) AS b FROM interface_hourly h
                    WHERE h.device_id = %(d)s AND h.name = %(i)s AND h.hour >= date_trunc('month', now())""",
                {"d": str(gw["id"]), "i": w["interface"]})["b"] or 0)
        gateway = {"id": str(gw["id"]), "name": gw["name"], "model": gw["model"], "version": gw["ros_version"],
                   "state": gw["state"], "ip": gw["ip"], "uptime": res.get("uptime"), "wans": wans}

    aps = tx.fetchall(
        f"""SELECT h.device_id, {_BYTES} AS bytes FROM interface_hourly h JOIN device d ON d.id = h.device_id
            WHERE d.site_id = %(s)s AND h.hour > now() - make_interval(hours => %(h)s) AND h.name ~ %(re)s
            GROUP BY h.device_id ORDER BY 2 DESC LIMIT 5""", dict(p, re=WIFI_IFACE_RE))
    wcl = {str(r["device_id"]): r for r in tx.fetchall(
        """SELECT device_id, count(*) AS n, round(avg(substring(signal from '^-?[0-9]+')::int))::int AS signal
           FROM client WHERE site_id = %(s)s AND ssid IS NOT NULL AND device_id IS NOT NULL
             AND last_seen > now() - interval '15 minutes' GROUP BY device_id""", p)}
    top_aps = [{"device_id": str(r["device_id"]), "name": names.get(str(r["device_id"]), "?"), "bytes": int(r["bytes"] or 0),
                "clients": int(wcl.get(str(r["device_id"]), {}).get("n") or 0)} for r in aps]
    signal = [{"device_id": k, "name": names.get(k, "?"), "signal": v["signal"], "clients": int(v["n"])}
              for k, v in wcl.items() if v["signal"] is not None]

    # drátový klient sám na portu: provoz portu = provoz klienta (u Wi-Fi klientů historii provozu nemáme)
    top_clients = tx.fetchall(
        f"""WITH c AS (SELECT device_id, port, min(mac) AS mac, min(coalesce(nullif(hostname, ''), mac)) AS name
                       FROM client WHERE site_id = %(s)s AND ssid IS NULL AND device_id IS NOT NULL
                         AND port IS NOT NULL AND port <> '?' AND last_seen > now() - make_interval(hours => %(h)s)
                       GROUP BY device_id, port HAVING count(*) = 1)
            SELECT c.mac, c.name, c.device_id, c.port, {_BYTES} AS bytes
            FROM c JOIN interface_hourly h ON h.device_id = c.device_id AND h.name = c.port
            WHERE h.hour > now() - make_interval(hours => %(h)s)
            GROUP BY c.mac, c.name, c.device_id, c.port ORDER BY 5 DESC LIMIT 6""", p)
    top_clients = [{"mac": r["mac"], "name": r["name"], "device": names.get(str(r["device_id"]), "?"), "port": r["port"],
                    "bytes": int(r["bytes"] or 0)} for r in top_clients if int(r["bytes"] or 0) > 0]

    links = [{"id": ln["id"], "name": ln["name"], "kind": ln["kind"], "state": ln["state"], "score": ln["score"]}
             for ln in wlink.site_links(tx, site_id)]
    ports = [{"device_id": did, "device": names.get(did, "?"), "port": port, "level": h["level"], "why": h["why"],
              "top": h["top"], "errors_24h": h["errors_24h"]}
             for (did, port), h in porthealth.site_health(tx, site_id).items() if h["level"] in ("warn", "crit")]
    order = {"down": 0, "bad": 1, "degraded": 2, "unknown": 3, "ok": 4}
    links.sort(key=lambda x: (order.get(x["state"], 3), x["name"]))
    ports.sort(key=lambda x: (x["level"] != "crit", x["device"], x["port"]))

    wf = tx.fetchone(
        """SELECT count(*) FILTER (WHERE kind = 'connected') AS ok, count(*) FILTER (WHERE kind = 'auth_fail') AS fail
           FROM wifi_event WHERE site_id = %(s)s AND at > now() - make_interval(hours => %(h)s)""", p)
    todo = tx.fetchall(
        """SELECT a.id, a.kind, a.severity::text AS severity, a.subject, a.message, a.opened_at, a.device_id,
                  coalesce(nullif(d.identity, ''), d.serial) AS device
           FROM alert a JOIN device d ON d.id = a.device_id
           WHERE d.site_id = %(s)s AND a.resolved_at IS NULL ORDER BY a.severity DESC, a.opened_at DESC LIMIT 6""", p)
    todo_n = int(tx.fetchone(
        """SELECT count(*) AS n FROM alert a JOIN device d ON d.id = a.device_id
           WHERE d.site_id = %(s)s AND a.resolved_at IS NULL""", p)["n"])
    return {"hours": int(hours), "gateway": gateway, "counts": counts, "probe": wanprobe.series(tx, site_id, hours),
            "top_aps": top_aps, "top_clients": top_clients, "links": links[:6], "ports": ports[:6], "signal": signal,
            "wifi_auth": {"ok": int(wf["ok"] or 0), "fail": int(wf["fail"] or 0)},
            "todo": [dict(t, id=str(t["id"]), device_id=str(t["device_id"])) for t in todo], "todo_total": todo_n}
