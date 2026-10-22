"""Nástroje na portu a zařízení (jako Live Tools v Meraki) a hromadné operace s porty.

Živé nástroje jdou přímo přes REST v tunelu (výsledek hned): ping a traceroute ze zařízení,
Wake-on-LAN, test kabelu, blikání LED portu. Restart PoE a restart portu jdou frontou příkazů
(běží na zařízení celé, i na uplinku). Vše se zapisuje do auditu.

Hromadné operace: profil nebo nastavení na víc portů víc switchů – jeden plán s návratem
na každé zařízení; LACP a MLAG přes bondcfg.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from . import inventory, plans, portcfg, profiles
from .db import Database, Tx, tenant_scope
from .services import NotFound, ServiceError, audit, device_api

_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_PORT = re.compile(r"^[A-Za-z0-9_./-]{1,64}$")


def _target(v: Any) -> str:
    t = str(v or "").strip()
    try:
        return str(ipaddress.ip_address(t))
    except ValueError:
        pass
    if _HOST.match(t):
        return t
    raise ServiceError("zadej IP adresu nebo jméno")


def _dev(tx: Tx, did: Any) -> dict:
    d = tx.fetchone("""SELECT id, identity, serial, state, host(tunnel_ip) AS tunnel_ip, api_cert_fingerprint AS fp
                       FROM device WHERE id = %(d)s""", {"d": str(did)})
    if not d:
        raise NotFound("zařízení neexistuje")
    if d["state"] != "adopted":
        raise ServiceError(f"{d['identity'] or d['serial']} není online")
    return d


def _rows(v: Any) -> list[dict]:
    if isinstance(v, dict):
        return [v]
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _ms(v: Any) -> float | None:
    """'12ms', '1ms540us', '850us' → ms."""
    t = str(v or "")
    if not t:
        return None
    total, ok = 0.0, False
    for n, u in re.findall(r"([\d.]+)(ms|us|s)", t):
        total += float(n) * {"s": 1000, "ms": 1, "us": 0.001}[u]
        ok = True
    return round(total, 2) if ok else None


def run_tool(db: Database, box, tenant_id: Any, device_id: Any, tool: str, params: dict,
             user_id: Any = None, ip: str | None = None) -> dict:
    """Spustí živý nástroj na zařízení a vrátí výsledek."""
    if tool not in TOOLS:
        raise ServiceError("neznámý nástroj")
    with tenant_scope(db, tenant_id) as tx:
        d = _dev(tx, device_id)
        inv = inventory.load(tx, device_id, ["interfaces", "ethernet", "bridge_hosts", "bridge_ports", "bridges"])
        body, timeout, after = TOOLS[tool][0](inv, params)
        audit(tx, f"device.tool.{tool}", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after=after, ip=ip)
        api = device_api(tx, box, d, timeout=timeout)
    try:
        try:
            raw = api.post(*body)
        except Exception as e:  # noqa: BLE001 – chybu zařízení předat uživateli
            alt = TOOLS[tool][2]
            if alt is None:
                raise
            raw = api.post(*alt(params, e))
        return {"tool": tool, **TOOLS[tool][1](raw)}
    except ServiceError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"{TOOL_LABEL[tool]} selhal: {str(e)[:300]}") from e
    finally:
        close = getattr(api, "close", None)
        if close:
            close()


# --- jednotlivé nástroje: (příprava → (path, body), timeout, audit) / zpracování / záložní volání ---


def _prep_ping(inv: dict, p: dict):
    addr = _target(p.get("address"))
    n = int(p.get("count") or 4)
    if not 1 <= n <= 20:
        raise ServiceError("počet 1–20")
    return ("/ping", {"address": addr, "count": str(n)}), n * 1.5 + 8, {"address": addr, "count": n}


def _res_ping(raw: Any) -> dict:
    rows = _rows(raw)
    replies = [{"seq": r.get("seq"), "host": r.get("host"), "time_ms": _ms(r.get("time")), "ttl": r.get("ttl"),
                "status": r.get("status") or ("" if r.get("time") else "timeout")} for r in rows]
    last = rows[-1] if rows else {}
    sent, recv = int(last.get("sent") or len(rows) or 0), int(last.get("received") or sum(1 for r in replies if r["time_ms"] is not None))
    times = [r["time_ms"] for r in replies if r["time_ms"] is not None]
    return {"replies": replies, "sent": sent, "received": recv,
            "loss_pct": round(100 * (sent - recv) / sent) if sent else None,
            "avg_ms": _ms(last.get("avg-rtt")) or (round(sum(times) / len(times), 2) if times else None),
            "min_ms": _ms(last.get("min-rtt")) or (min(times) if times else None),
            "max_ms": _ms(last.get("max-rtt")) or (max(times) if times else None)}


def _prep_trace(inv: dict, p: dict):
    addr = _target(p.get("address"))
    return ("/tool/traceroute", {"address": addr, "count": "1", "max-hops": "20"}), 45, {"address": addr}


def _res_trace(raw: Any) -> dict:
    hops = []
    for i, r in enumerate(_rows(raw), 1):
        hops.append({"hop": i, "address": r.get("address") or "", "loss": r.get("loss"),
                     "last_ms": _ms(r.get("last")), "avg_ms": _ms(r.get("avg")), "status": r.get("status") or ""})
    return {"hops": hops}


def _bridge_for_mac(inv: dict, mac: str) -> str | None:
    for h in inv.get("bridge_hosts") or []:
        if isinstance(h, dict) and str(h.get("mac-address") or "").upper() == mac:
            return h.get("bridge") or None
    br = [b.get("name") for b in inv.get("bridges") or [] if isinstance(b, dict) and b.get("name")]
    return br[0] if br else None


def _prep_wol(inv: dict, p: dict):
    mac = str(p.get("mac") or "").strip().upper().replace("-", ":")
    if not _MAC.match(mac):
        raise ServiceError("neplatná MAC")
    iface = str(p.get("interface") or "") or _bridge_for_mac(inv, mac)
    names = {i.get("name") for key in ("interfaces", "bridges") for i in inv.get(key) or [] if isinstance(i, dict)}
    if not iface or iface not in names:
        raise ServiceError("rozhraní pro Wake-on-LAN na zařízení není")
    return ("/tool/wol", {"mac": mac, "interface": iface}), 10, {"mac": mac, "interface": iface}


def _res_simple(raw: Any) -> dict:
    return {"ok": True}


def _port(inv: dict, p: dict) -> str:
    port = str(p.get("port") or "")
    if not _PORT.match(port) or not any(i.get("name") == port and i.get("type") == "ether"
                                         for i in inv.get("interfaces") or [] if isinstance(i, dict)):
        raise ServiceError("port na zařízení není")
    return port


def _prep_cable(inv: dict, p: dict):
    port = _port(inv, p)
    return ("/interface/ethernet/cable-test", {"numbers": port, "duration": "5s"}), 20, {"port": port}


def _alt_cable(p: dict, err: Exception):
    # starší RouterOS: bez "duration" – jednorázově
    return ("/interface/ethernet/cable-test", {"numbers": str(p.get("port")), "once": ""})


def _res_cable(raw: Any) -> dict:
    rows = _rows(raw)
    last = rows[-1] if rows else {}
    pairs = str(last.get("cable-pairs") or "")
    out = []
    for part in [x for x in pairs.split(",") if x]:
        st, _, length = part.partition(":")
        out.append({"status": st.strip(), "length_m": int(length) if length.strip().isdigit() else None})
    return {"status": str(last.get("status") or "") or None, "pairs": out, "raw": {k: str(v) for k, v in last.items()}}


def _prep_blink(inv: dict, p: dict):
    port = _port(inv, p)
    sec = int(p.get("seconds") or 15)
    if not 3 <= sec <= 60:
        raise ServiceError("blikání 3–60 s")
    return ("/interface/ethernet/blink", {"numbers": port, "duration": f"{sec}s"}), sec + 10, {"port": port, "seconds": sec}


TOOLS = {
    "ping": (_prep_ping, _res_ping, None),
    "traceroute": (_prep_trace, _res_trace, None),
    "wol": (_prep_wol, _res_simple, None),
    "cable_test": (_prep_cable, _res_cable, _alt_cable),
    "blink": (_prep_blink, _res_simple, None),
}
TOOL_LABEL = {"ping": "Ping", "traceroute": "Traceroute", "wol": "Wake-on-LAN", "cable_test": "Test kabelu",
              "blink": "Blikání LED"}


# --- cíl PoE hlídače ------------------------------------------------------------------------


def watchdog_target(tx: Tx, inv: dict, device_id: Any, port: str) -> str | None:
    """Co na portu hlídat pingem: IP souseda (MNDP/LLDP), jinak IP klienta z korelace,
    jinak MAC MikroTiku za portem (MAC-ping funguje jen u MikroTiku)."""
    neigh = [n for n in inv.get("neighbors") or [] if isinstance(n, dict)
             and str(n.get("interface") or "").split(",")[0] == port]
    for n in neigh:
        for a in str(n.get("address") or "").split(","):
            try:
                ip = ipaddress.ip_address(a.strip())
                if ip.version == 4 and not ip.is_link_local:
                    return str(ip)
            except ValueError:
                continue
    c = tx.fetchone("""SELECT ip::text AS ip FROM client WHERE device_id = %(d)s AND port = %(p)s AND ip IS NOT NULL
                       AND last_seen >= now() - interval '1 day' ORDER BY last_seen DESC LIMIT 1""",
                    {"d": str(device_id), "p": port})
    if c and c["ip"]:
        return str(c["ip"]).split("/")[0]
    for n in neigh:
        mac = str(n.get("mac-address") or "").upper()
        if _MAC.match(mac) and "mikrotik" in str(n.get("platform") or "").lower():
            return mac
    return None


# --- hromadné operace -------------------------------------------------------------------------


def _uplinks(tx: Tx, inv: dict) -> set[str]:
    from .web.devices import _uplinks as up

    return up(tx, inv)


def bulk(db: Database, settings, tenant_id: Any, targets: list[dict], *, desired: dict | None = None,
         profile_id: int | None = None, apply: bool = False, can_manage: bool = False,
         user_id: Any = None, ip: str | None = None) -> list[dict]:
    """Stejné nastavení nebo profil na vybrané porty víc zařízení: jeden plán na zařízení.
    targets: [{device_id, ports: [...]}]. Vrací výsledek po zařízeních (plán / chyba)."""
    if not targets or not isinstance(targets, list) or len(targets) > 100:
        raise ServiceError("vyber porty (nejvýš 100 zařízení)")
    if (desired is None) == (profile_id is None):
        raise ServiceError("zadej buď nastavení, nebo profil")
    if desired is not None:
        allowed = {"disabled", "poe", "pvid", "tagged", "edge", "loop_protect", "poe_watchdog"}
        if not isinstance(desired, dict) or not desired or set(desired) - allowed:
            raise ServiceError("hromadně lze měnit: " + ", ".join(sorted(allowed)))
    out = []
    for t in targets:
        did = str(t.get("device_id") or "")
        ports = [str(p) for p in t.get("ports") or [] if _PORT.match(str(p))]
        item: dict[str, Any] = {"device_id": did, "ports": ports}
        try:
            if not ports or len(ports) > 128:
                raise ServiceError("žádné porty")
            warn: list[str] = []
            with tenant_scope(db, tenant_id) as tx:
                inv = inventory.load(tx, did)
                ups = _uplinks(tx, inv)
                if profile_id is not None:
                    prof = profiles.get(tx, profile_id)
                items = {}
                for port in ports:
                    if profile_id is not None:
                        tgt = watchdog_target(tx, inv, did, port) if prof.get("poe_watchdog") else None
                        d, w = profiles.desired(prof, tgt)
                        if w:
                            warn.append(f"{port}: {w}")
                        # profil s PoE na portu bez PoE: PoE vynechat, zbytek použít
                        st = portcfg.port_state(inv, port) if any(i.get("name") == port for i in inv.get("interfaces") or []) else None
                        if st and st["poe"] is None:
                            d.pop("poe", None)
                            d.pop("poe_watchdog", None)
                        items[port] = d
                    else:
                        d = dict(desired)
                        if "poe_watchdog" in d and (d["poe_watchdog"] or {}).get("enabled") and not d["poe_watchdog"].get("address"):
                            tgt = watchdog_target(tx, inv, did, port)
                            if not tgt:
                                warn.append(f"{port}: PoE hlídač vynechán – na portu není známé zařízení")
                                d.pop("poe_watchdog")
                            else:
                                d["poe_watchdog"] = {**d["poe_watchdog"], "address": tgt}
                        items[port] = d
            res = plans.create_ports_plan(db, tenant_id, did, items, user_id, ip, ups, warn)
            item.update(res)
            if apply:
                if not can_manage:
                    raise ServiceError("plán je uložený – schválit ho musí správce tenanta")
                item["command_id"] = plans.approve_plan(db, settings, tenant_id, res["plan_id"], user_id, ip)
        except (ServiceError, portcfg.PortError) as e:
            item["error"] = str(e)
        out.append(item)
    return out


def site_ports(tx: Tx, site_id: Any) -> list[dict]:
    """Všechny fyzické porty spravovaných switchů lokality pro hromadnou úpravu (stav z inventury)."""
    devs = tx.fetchall("""SELECT id, identity, serial, model, state FROM device WHERE site_id = %(s)s
                          AND state IN ('adopted', 'offline') ORDER BY identity, serial""", {"s": str(site_id)})
    profs = profiles.list_profiles(tx)
    out = []
    for d in devs:
        inv = inventory.load(tx, d["id"])
        if not inv:
            continue
        mods = plans.module_modes(tx, d["id"])
        running = {str(i.get("name")): str(i.get("running")) in ("true", "yes") for i in inv.get("interfaces") or []
                   if isinstance(i, dict)}
        neigh = {}
        for n in inv.get("neighbors") or []:
            if isinstance(n, dict) and n.get("interface"):
                neigh.setdefault(str(n["interface"]).split(",")[0], n.get("identity") or n.get("address") or "")
        ports = []
        for i in sorted((i for i in inv.get("interfaces") or [] if isinstance(i, dict) and i.get("type") == "ether"),
                        key=lambda x: portcfg._port_key(str(x.get("name")))):
            try:
                st = portcfg.port_state(inv, str(i["name"]))
            except portcfg.PortError:
                continue
            m = profiles.matching(profs, st)
            ports.append({"port": st["port"], "comment": st["comment"], "disabled": st["disabled"], "running": running.get(st["port"]),
                          "poe": st["poe"], "pvid": st["pvid"], "tagged": st["tagged"], "bond": st["bond"],
                          "lock": st["lock"], "watchdog": (st["poe_watchdog"] or {}).get("enabled"),
                          "profile": m["name"] if m else None, "neighbor": neigh.get(st["port"])})
        from . import bondcfg

        out.append({"device_id": str(d["id"]), "device": d["identity"] or d["serial"], "model": d["model"],
                    "online": d["state"] == "adopted", "manage": mods.get("ports") == "manage", "ports": ports,
                    "bonds": bondcfg.summarize_bonds(inv)})
    return out


def mlag_pair(db: Database, tenant_id: Any, a: dict, b: dict, user_id: Any = None, ip: str | None = None) -> list[dict]:
    """Plány MLAG pro oba switche dvojice (A primární). a/b: {device_id, peer_port, members:[{ports, mlag_id}]}."""
    ida, idb = str(a.get("device_id") or ""), str(b.get("device_id") or "")
    if not ida or ida == idb:
        raise ServiceError("vyber dva různé switche")
    ma = sorted(int(m.get("mlag_id") or 0) for m in a.get("members") or [])
    mb = sorted(int(m.get("mlag_id") or 0) for m in b.get("members") or [])
    if ma != mb:
        raise ServiceError("oba switche musí mít stejná mlag-id (každý bond ke klientovi na obou stranách)")
    from . import bondcfg

    with tenant_scope(db, tenant_id) as tx:
        da = tx.fetchone("SELECT identity, serial, site_id FROM device WHERE id = %(d)s", {"d": ida})
        db_ = tx.fetchone("SELECT identity, serial, site_id FROM device WHERE id = %(d)s", {"d": idb})
        if not da or not db_:
            raise NotFound("zařízení neexistuje")
        if da["site_id"] != db_["site_id"]:
            raise ServiceError("MLAG jen mezi switchi jedné lokality")
        pair = bondcfg.pair_checks(inventory.load(tx, ida), str(a.get("peer_port")), inventory.load(tx, idb),
                                   str(b.get("peer_port")), da["identity"] or da["serial"], db_["identity"] or db_["serial"])
    out = []
    for cfg, primary in ((a, True), (b, False)):
        res = plans.create_bond_plan(db, tenant_id, cfg["device_id"], "mlag",
                                     {"peer_port": cfg.get("peer_port"), "members": cfg.get("members") or [],
                                      "primary": primary, "pair_warnings": pair}, user_id, ip)
        out.append({"device_id": str(cfg["device_id"]), **res})
    return out


