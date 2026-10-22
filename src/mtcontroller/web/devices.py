"""Admin API: detail zařízení, moduly, zálohy, plány změn, topologie."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import (analysis, backup, firewall, inventory, oui, packages, plans, portcfg, porthistory, ports, portview,
               profiles, ros_versions)
from ..auth import MANAGE, OPERATE, READ, Principal
from ..db import tenant_scope
from ..services import NotFound, ServiceError, audit
from .admin import _err, _json, _uuid, endpoint
from .app import client_ip, get_state, json_response

T = "/api/admin/tenants/{tenant_id}"


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _summary(inv: dict) -> dict:
    res = inv.get("resource") if isinstance(inv.get("resource"), dict) else {}
    rb = inv.get("routerboard") if isinstance(inv.get("routerboard"), dict) else {}
    upd = inv.get("update") if isinstance(inv.get("update"), dict) else {}

    def pct(free, total):
        try:
            return round(100 - 100 * int(free) / int(total))
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    return {
        "board": res.get("board-name"), "version": res.get("version"), "uptime": res.get("uptime"),
        "cpu_load": res.get("cpu-load"), "cpu": res.get("cpu"), "architecture": res.get("architecture-name"),
        "memory_used_pct": pct(res.get("free-memory"), res.get("total-memory")),
        "hdd_used_pct": pct(res.get("free-hdd-space"), res.get("total-hdd-space")),
        "firmware": rb.get("current-firmware"), "firmware_upgrade": rb.get("upgrade-firmware"),
        "serial": rb.get("serial-number"),
        "update_channel": upd.get("channel"), "latest_version": upd.get("latest-version"),
        "update_status": upd.get("status"),
    }


@endpoint(READ)
async def device_detail(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            d = tx.fetchone(
                """SELECT d.id, d.serial, d.identity, d.model, d.ros_version, d.role, d.state, d.note,
                          host(d.tunnel_ip) AS tunnel_ip, d.site_id, s.name AS site, d.concentrator,
                          d.last_checkin_at, d.last_status, d.adopted_at, d.inventory_at, d.inventory_error,
                          d.backup_at, d.managed_at,
                          (SELECT c.params->>'name' FROM device_command c WHERE c.device_id = d.id
                            AND c.kind = 'set_identity' AND c.status IN ('queued', 'sent')
                            ORDER BY c.id DESC LIMIT 1) AS identity_pending
                   FROM device d LEFT JOIN site s ON s.id = d.site_id WHERE d.id = %(d)s""", {"d": did})
            if not d:
                raise NotFound("zařízení neexistuje")
            inv = inventory.load(tx, did)
            backups = tx.fetchall(
                """SELECT id, taken_at, reason, size_bytes, changed FROM device_backup
                   WHERE device_id = %(d)s ORDER BY id DESC LIMIT 30""", {"d": did})
            pl = tx.fetchall(
                """SELECT id, module, summary, status, created_at, approved_at, finished_at
                   FROM config_plan WHERE device_id = %(d)s ORDER BY id DESC LIMIT 20""", {"d": did})
            cmds = tx.fetchall(
                """SELECT id, kind, status, attempts, created_at, finished_at FROM device_command
                   WHERE device_id = %(d)s ORDER BY id DESC LIMIT 20""", {"d": did})
            modes = plans.module_modes(tx, did)
            dev_alerts = tx.fetchall(
                """SELECT id, kind, subject, severity::text AS severity, message, opened_at, acknowledged_at
                   FROM alert WHERE device_id = %(d)s AND resolved_at IS NULL ORDER BY opened_at DESC""", {"d": did})
            conn = portview.connected(inv, portview.recent_clients(tx, did), portview.managed_roles(tx))
            anomaly = portview.anomalies(tx, did, inv)
            profs = profiles.list_profiles(tx)
        ifaces = [{
            "name": i.get("name"), "type": i.get("type"), "running": i.get("running") == "true",
            "disabled": i.get("disabled") == "true", "mac": i.get("mac-address"), "comment": i.get("comment"),
            "link_downs": i.get("link-downs"), "rx_error": i.get("rx-error"), "tx_error": i.get("tx-error"),
        } for i in (inv.get("interfaces") or []) if isinstance(i, dict) and i.get("dynamic") != "true"]
        pinfo = ports.port_info(inv)
        for i in ifaces:
            name = str(i["name"])
            i["port"] = pinfo.get(name)
            i["connected"] = conn.get(name)
            i["anomaly"] = anomaly.get(name)
            if i["port"] is not None and profs:
                try:
                    m = profiles.matching(profs, portcfg.port_state(inv, name))
                except portcfg.PortError:
                    m = None
                i["profile"] = m["name"] if m else None
        return {
            "device": d, "summary": _summary(inv), "modules": modes,
            "packages": {"wifi": packages.wifi_status(inv), "mismatch": packages.version_mismatch(inv),
                         "installed": sorted(packages.installed(inv))} if inv else None,
            "warnings": [{"severity": i.severity, "text": i.text, "source": i.source}
                         for i in ros_versions.issues_for(d.get("ros_version") or "")],
            "interfaces": ifaces,
            "addresses": inv.get("addresses") or [],
            "vlan_names": portview.vlan_names(inv),
            "neighbors": inv.get("neighbors") or [],
            "vlan": analysis.analyze_vlan(inv).as_dict() if inv else None,
            "ntp": analysis.ntp_status(inv) if inv else None,
            "security": inv.get("hardening") if isinstance(inv.get("hardening"), list) else None,
            "firewall": firewall.analyze(inv) if inv else None,
            "backups": backups, "plans": pl, "commands": cmds, "alerts": dev_alerts,
        }

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def device_update(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    did = _uuid(request.path_params["device_id"])
    from .. import services

    res = await run_in_threadpool(services.update_device, st.db, tid, did, data, p.user_id, client_ip(request))
    return json_response(res)


def _uplinks(tx, inv: dict) -> set[str]:
    """Porty, které vedou ke správě: STP root port a porty ke spravovaným zařízením."""
    from ..infra import _port_of

    out = {str(p.get("interface")) for p in inv.get("stp_ports") or [] if isinstance(p, dict) and p.get("role") == "root-port"}
    managed = {r["identity"] for r in tx.fetchall("SELECT identity FROM device WHERE identity IS NOT NULL")}
    for n in inv.get("neighbors") or []:
        if isinstance(n, dict) and n.get("identity") in managed:
            out.add(_port_of(n.get("interface")))
    return out


@endpoint(READ)
async def port_get(request: Request, p: Principal, tid: str) -> Response:
    from .. import portcfg

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    port = str(request.path_params["port"])[:64]

    def q():
        with tenant_scope(st.db, tid) as tx:
            inv = inventory.load(tx, did)
            try:
                state = portcfg.port_state(inv, port)
            except portcfg.PortError as e:
                raise NotFound(str(e)) from e
            vlans = sorted({str(v.get("vlan-ids")) for v in inv.get("bridge_vlans") or []
                            if isinstance(v, dict) and str(v.get("vlan-ids", "")).isdigit()}, key=int)
            mode = plans.module_modes(tx, did).get("ports")
            profs = profiles.list_profiles(tx)
            match = profiles.matching(profs, state)
            from .. import porttools

            sug = porttools.watchdog_target(tx, inv, did, port) if state.get("poe_watchdog") is not None else None
            return {**state, "known_vlans": vlans, "uplink": port in _uplinks(tx, inv), "module_mode": mode,
                    "watchdog_suggest": sug,
                    "profiles": profs, "profile": match["id"] if match else None,
                    "history": porthistory.history(tx, did, port, days=30, limit=20)}

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def port_plan(request: Request, p: Principal, tid: str) -> Response:
    """Vytvoří plán změny portu; s "apply": true ho rovnou schválí (aplikace s automatickým návratem)."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    port = str(request.path_params["port"])[:64]
    data = await _json(request)
    desired = data.get("desired") if isinstance(data.get("desired"), dict) else {}
    ip = client_ip(request)

    def run():
        with tenant_scope(st.db, tid) as tx:
            up = _uplinks(tx, inventory.load(tx, did))
        res = plans.create_port_plan(st.db, tid, did, port, desired, p.user_id, ip, up)
        if data.get("apply"):
            if not p.can(tid, MANAGE):
                raise ServiceError("plán je uložený – schválit ho musí správce tenanta")
            res["command_id"] = plans.approve_plan(st.db, st.settings, tid, res["plan_id"], p.user_id, ip)
        return res

    return json_response(await run_in_threadpool(run), 201)


@endpoint(READ)
async def port_history(request: Request, p: Principal, tid: str) -> Response:
    """Historie portů zařízení (Port Time Machine), volitelně jen jednoho portu."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    port = request.query_params.get("port") or None
    try:
        days = max(1, min(90, int(request.query_params.get("days", "7"))))
    except ValueError:
        days = 7

    def q():
        with tenant_scope(st.db, tid) as tx:
            if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s", {"d": did}):
                raise NotFound("zařízení neexistuje")
            return {"days": days, "port": port,
                    "events": porthistory.history(tx, did, port[:100] if port else None, days=days, limit=500)}

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def dhcp_server_view(request: Request, p: Principal, tid: str) -> Response:
    """Leases a statické IP jednoho DHCP serveru: výrobce, poloha, poznámky, mapa adres, konflikty."""
    from .. import leases

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    server = str(request.path_params["server"])[:64]

    def q():
        with tenant_scope(st.db, tid) as tx:
            d = tx.fetchone("SELECT id, site_id, identity, serial FROM device WHERE id = %(d)s", {"d": did})
            if not d:
                raise NotFound("zařízení neexistuje")
            inv = inventory.load(tx, did)
            clients = {r["mac"]: r for r in tx.fetchall(
                """SELECT mac, hostname, device, port, ssid, signal, last_seen FROM client
                   WHERE site_id = %(s)s""", {"s": str(d["site_id"])})} if d["site_id"] else {}
            notes = {r["mac"]: r["note"] for r in tx.fetchall("SELECT mac, note FROM client_note WHERE note <> ''")}
            pending = tx.fetchall(
                """SELECT id, status::text AS status, summary, changes->>'mac' AS mac, created_at FROM config_plan
                   WHERE device_id = %(d)s AND module = 'dhcp' AND changes->>'server' = %(s)s
                     AND status IN ('draft', 'queued', 'confirming') ORDER BY id DESC""", {"d": did, "s": server})
            mode = plans.module_modes(tx, did).get("dhcp")
        try:
            view = leases.server_view(inv, server, clients, notes)
        except leases.LeaseError as e:
            raise NotFound(str(e)) from e
        return {**view, "device_id": str(did), "device": d["identity"] or d["serial"], "module_mode": mode,
                "pending": pending, "oui": oui.available()}

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def dhcp_lease_plan(request: Request, p: Principal, tid: str) -> Response:
    """Plán změny leasu; s "apply": true ho rovnou schválí (správce tenanta) – aplikace s návratem."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    server = str(request.path_params["server"])[:64]
    data = await _json(request)
    desired = data.get("desired") if isinstance(data.get("desired"), dict) else {}
    ip = client_ip(request)

    def run():
        res = plans.create_lease_plan(st.db, tid, did, server, str(data.get("mac") or ""), desired, p.user_id, ip)
        if data.get("apply"):
            if not p.can(tid, MANAGE):
                raise ServiceError("plán je uložený – schválit ho musí správce tenanta")
            res["command_id"] = plans.approve_plan(st.db, st.settings, tid, res["plan_id"], p.user_id, ip)
        return res

    return json_response(await run_in_threadpool(run), 201)


@endpoint(OPERATE)
async def dhcp_network_plan(request: Request, p: Principal, tid: str) -> Response:
    """Plán změny nastavení sítě DHCP (DNS, NTP, doména, globální DHCP options); s "apply" rovnou schválí."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    server = str(request.path_params["server"])[:64]
    data = await _json(request)
    desired = data.get("desired") if isinstance(data.get("desired"), dict) else {}
    ip = client_ip(request)

    def run():
        res = plans.create_network_plan(st.db, tid, did, server, desired, p.user_id, ip)
        if data.get("apply"):
            if not p.can(tid, MANAGE):
                raise ServiceError("plán je uložený – schválit ho musí správce tenanta")
            res["command_id"] = plans.approve_plan(st.db, st.settings, tid, res["plan_id"], p.user_id, ip)
        return res

    return json_response(await run_in_threadpool(run), 201)


@endpoint(READ)
async def client_dhcp(request: Request, p: Principal, tid: str) -> Response:
    """Který DHCP server klienta obsluhuje (pro rezervaci a options přímo ze seznamu klientů)."""
    from .. import leases

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    mac = leases.norm_mac(request.path_params["mac"])
    if not mac:
        raise ServiceError("neplatná MAC adresa")

    def q():
        with tenant_scope(st.db, tid) as tx:
            devs = tx.fetchall("""SELECT id, identity, serial FROM device WHERE site_id = %(s)s
                                  AND state IN ('adopted', 'offline')""", {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"], ["dhcp_leases", "dhcp_servers", "addresses"]) for d in devs}
            c = tx.fetchone("SELECT ip FROM client WHERE site_id = %(s)s AND mac = %(m)s", {"s": sid, "m": mac})
        found = leases.server_for_client(invs, mac, (c or {}).get("ip"))
        if not found:
            raise NotFound("klienta neobsluhuje žádný DHCP server na spravovaných zařízeních lokality")
        d = next(x for x in devs if str(x["id"]) == found[0])
        return {"device_id": found[0], "server": found[1], "device": d["identity"] or d["serial"], "mac": mac}

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def client_note(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    from .. import clientmeta

    fields = {k: data[k] for k in ("note", "label", "icon") if k in data}
    if not fields:
        raise ServiceError("nic ke změně (note, label, icon)")
    res = await run_in_threadpool(clientmeta.set_client, st.db, tid, request.path_params["mac"], fields, p.user_id,
                                  client_ip(request))
    return json_response(res)


@endpoint(READ)
async def client_logos(request: Request, p: Principal, tid: str) -> Response:
    from .. import clientmeta

    st = get_state(request)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return {"logos": clientmeta.list_logos(tx), "icons": list(clientmeta.ICONS)}

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def client_logo(request: Request, p: Principal, tid: str) -> Response:
    """Logo (obrázek) – GET čte kdokoli z tenanta, PUT/DELETE technik a výš."""
    from .. import clientmeta

    st = get_state(request)
    scope, key = str(request.path_params["scope"]), str(request.path_params["key"])[:200]
    if request.method in ("PUT", "DELETE"):
        if not p.can(tid, OPERATE):
            return _err(403, "nedostatečná oprávnění")
        if request.method == "DELETE":
            await run_in_threadpool(clientmeta.delete_logo, st.db, tid, scope, key, p.user_id, client_ip(request))
            return json_response({"ok": True})
        data = await _json(request, limit=96 * 1024)
        return json_response(await run_in_threadpool(clientmeta.put_logo, st.db, tid, scope, key, data.get("data"),
                                                     p.user_id, client_ip(request)))

    def q():
        with tenant_scope(st.db, tid) as tx:
            return clientmeta.get_logo(tx, scope, key)

    lg = await run_in_threadpool(q)
    if not lg:
        raise NotFound("logo neexistuje")
    # obrázek podle ověřené hlavičky; nosniff a sandbox CSP – prohlížeč ho nikdy nespustí jako stránku
    return Response(lg["data"], media_type=lg["mime"], headers={
        "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox", "Content-Disposition": "inline"})


@endpoint(READ)
async def port_profiles(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    if request.method == "POST":
        if not p.can(tid, MANAGE):
            return _err(403, "nedostatečná oprávnění")
        data = await _json(request)
        return json_response(await run_in_threadpool(profiles.create, st.db, tid, data, p.user_id, client_ip(request)), 201)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return profiles.list_profiles(tx)

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def port_profile_update(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    try:
        pid = int(request.path_params["profile_id"])
    except ValueError:
        return _err(404, "profil neexistuje")
    data = await _json(request)
    return json_response(await run_in_threadpool(profiles.update, st.db, tid, pid, data, p.user_id, client_ip(request)))


@endpoint(OPERATE)
async def device_tool(request: Request, p: Principal, tid: str) -> Response:
    """Živý nástroj na zařízení: ping, traceroute, Wake-on-LAN, test kabelu, blikání LED portu."""
    from .. import porttools

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    return json_response(await run_in_threadpool(porttools.run_tool, st.db, st.box, tid, did,
                                                 str(request.path_params["tool"]), data, p.user_id, client_ip(request)))


@endpoint(OPERATE)
async def ports_bulk(request: Request, p: Principal, tid: str) -> Response:
    """Hromadná úprava portů (nastavení nebo profil) na víc switchích – plán s návratem na každé zařízení."""
    from .. import porttools

    st = get_state(request)
    data = await _json(request)
    pid = data.get("profile_id")
    try:
        pid = int(pid) if pid not in (None, "") else None
    except (TypeError, ValueError):
        return _err(400, "neplatný profil")
    desired = data.get("desired") if pid is None else None
    res = await run_in_threadpool(porttools.bulk, st.db, st.settings, tid, data.get("targets") or [],
                                  desired=desired, profile_id=pid, apply=bool(data.get("apply")),
                                  can_manage=p.can(tid, MANAGE), user_id=p.user_id, ip=client_ip(request))
    return json_response({"results": res}, 201)


@endpoint(READ)
async def site_ports(request: Request, p: Principal, tid: str) -> Response:
    from .. import porttools

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            return {"devices": porttools.site_ports(tx, sid), "profiles": profiles.list_profiles(tx)}

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def device_bond(request: Request, p: Principal, tid: str) -> Response:
    """LACP z vybraných portů ("lacp") nebo zrušení bondu controlleru ("bond_remove")."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    kind = str(data.get("kind") or "lacp")
    if kind not in ("lacp", "bond_remove"):
        return _err(400, "neznámý druh")
    ip = client_ip(request)

    def run():
        with tenant_scope(st.db, tid) as tx:
            up = _uplinks(tx, inventory.load(tx, did))
        res = plans.create_bond_plan(st.db, tid, did, kind, data, p.user_id, ip, up)
        if data.get("apply"):
            if not p.can(tid, MANAGE):
                raise ServiceError("plán je uložený – schválit ho musí správce tenanta")
            res["command_id"] = plans.approve_plan(st.db, st.settings, tid, res["plan_id"], p.user_id, ip)
        return res

    return json_response(await run_in_threadpool(run), 201)


@endpoint(OPERATE)
async def mlag_create(request: Request, p: Principal, tid: str) -> Response:
    """MLAG mezi dvěma switchi: dva plány (A primární, B sekundární), aplikace vědomě po jednom."""
    from .. import porttools

    st = get_state(request)
    data = await _json(request)
    a, b = data.get("a"), data.get("b")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return _err(400, "chybí nastavení obou switchů")
    for x in (a, b):
        x["device_id"] = str(_uuid(str(x.get("device_id") or "")))
    res = await run_in_threadpool(porttools.mlag_pair, st.db, tid, a, b, p.user_id, client_ip(request))
    return json_response({"plans": res}, 201)


@endpoint(READ)
async def site_rogue(request: Request, p: Principal, tid: str) -> Response:
    """Wi-Fi sítě ze skenu AP lokality: podvržené (naše SSID z cizího AP), sousední, naše."""
    from .. import rogue

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    if request.method == "POST":
        if not p.can(tid, OPERATE):
            return _err(404, "nenalezeno")
        data = await _json(request)
        if data.get("bssid"):
            if not p.can(tid, MANAGE):
                return _err(403, "označit známé AP smí správce tenanta")
            await run_in_threadpool(rogue.acknowledge, st.db, tid, sid, str(data["bssid"]), bool(data.get("ack", True)),
                                    p.user_id, client_ip(request))
            return json_response({"ok": True})
        did = _uuid(str(data.get("device_id") or ""))
        res = await run_in_threadpool(rogue.scan_device, st.db, st.box, tid, did, p.user_id, client_ip(request))
        return json_response(res, 201)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return rogue.site_list(tx, sid)

    return json_response({"bss": await run_in_threadpool(q)})


@endpoint(MANAGE)
async def port_profile_delete(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    try:
        pid = int(request.path_params["profile_id"])
    except ValueError:
        return _err(404, "profil neexistuje")
    await run_in_threadpool(profiles.delete, st.db, tid, pid, p.user_id, client_ip(request))
    return json_response({"ok": True})


@endpoint(READ)
async def release_notes(request: Request, p: Principal, tid: str) -> Response:
    """Co přinese dostupná aktualizace – změny relevantní pro toto zařízení."""
    from .. import releases

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            d = tx.fetchone("SELECT ros_version FROM device WHERE id = %(d)s", {"d": did})
            if not d:
                raise NotFound("zařízení neexistuje")
            inv = inventory.load(tx, did)
        upd = inv.get("update") if isinstance(inv.get("update"), dict) else {}
        cur = (d["ros_version"] or "").split(" ")[0]
        target = str(upd.get("latest-version") or "")
        if not target or ros_versions.parse(target) <= ros_versions.parse(cur):
            return {"current": cur, "target": None, "versions": [], "relevant": [], "other_count": 0,
                    "complete": False}
        return releases.notes_for(st.db, cur, target, inv)

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def device_module(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    await run_in_threadpool(plans.set_module_mode, st.db, tid, _uuid(request.path_params["device_id"]),
                            str(data.get("module", "")), str(data.get("mode", "")), p.user_id, client_ip(request))
    return json_response({"ok": True})


@endpoint(OPERATE)
async def request_backup(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            n = tx.execute("UPDATE device SET backup_at = NULL WHERE id = %(d)s AND state = 'adopted'", {"d": did})
            if not n:
                raise ServiceError("zálohu lze vyžádat jen u adoptovaného zařízení")
            audit(tx, "device.backup_requested", tenant_id=tid, user_id=p.user_id, target=f"device:{did}",
                  ip=client_ip(request))

    await run_in_threadpool(q)
    return json_response({"ok": True, "detail": "záloha proběhne do minuty"})


@endpoint(OPERATE)
async def backup_diff(request: Request, p: Principal, tid: str) -> Response:
    """Diff zálohy proti předchozí – hesla jsou v diffu skrytá."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    bid = _int(request.path_params["backup_id"], 0, 0, 2**62)

    def q():
        with tenant_scope(st.db, tid) as tx:
            prev = tx.fetchone("""SELECT id FROM device_backup WHERE device_id = %(d)s AND id < %(b)s
                                  ORDER BY id DESC LIMIT 1""", {"d": did, "b": bid})
            new = backup.load_text(tx, st.box, did, bid)
            old = backup.load_text(tx, st.box, did, prev["id"]) if prev else ""
            audit(tx, "device.backup_diff", tenant_id=tid, user_id=p.user_id, target=f"device:{did}",
                  after={"backup_id": bid}, ip=client_ip(request))
        return {"diff": backup.diff(old, new), "previous_id": prev["id"] if prev else None}

    try:
        return json_response(await run_in_threadpool(q))
    except backup.BackupError as e:
        return _err(404, str(e))


@endpoint(MANAGE)
async def backup_download(request: Request, p: Principal, tid: str) -> Response:
    """Celý export včetně hesel – jen tenant_admin, vždy auditováno."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    bid = _int(request.path_params["backup_id"], 0, 0, 2**62)

    def q():
        with tenant_scope(st.db, tid) as tx:
            text = backup.load_text(tx, st.box, did, bid)
            serial = tx.fetchone("SELECT serial FROM device WHERE id = %(d)s", {"d": did})["serial"]
            audit(tx, "device.backup_download", tenant_id=tid, user_id=p.user_id, target=f"device:{did}",
                  after={"backup_id": bid}, ip=client_ip(request))
        return text, serial

    try:
        text, serial = await run_in_threadpool(q)
    except backup.BackupError as e:
        return _err(404, str(e))
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{serial}-{bid}.rsc"'})


@endpoint(READ)
async def vlan_migration(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    target = request.query_params.get("target") or None

    def q():
        with tenant_scope(st.db, tid) as tx:
            inv = inventory.load(tx, did)
        if not inv:
            raise ServiceError("inventura zatím neproběhla")
        return {"analysis": analysis.analyze_vlan(inv).as_dict(),
                "plan": analysis.plan_vlan_migration(inv, target).as_dict()}

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def metrics(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    hours = _int(request.query_params.get("hours"), 24, 1, 24 * 400)
    iface = request.query_params.get("interface")

    def q():
        with tenant_scope(st.db, tid) as tx:
            return tx.fetchall(
                """SELECT hour, name, rx_bps_avg, tx_bps_avg, rx_bps_max, tx_bps_max,
                          rx_errors, tx_errors, link_downs
                   FROM interface_hourly WHERE device_id = %(d)s
                     AND hour > now() - make_interval(hours => %(h)s)
                     AND (%(i)s::text IS NULL OR name = %(i)s)
                   ORDER BY name, hour""", {"d": did, "h": hours, "i": iface})

    return json_response(await run_in_threadpool(q))


# fyzické porty pro součet „celé zařízení“ (bez bridge/vlan – ty by provoz započítaly dvakrát)
PHYS_PORT_RE = "^(ether|sfp|combo|qsfp)"

TRAFFIC_5M = """
WITH s AS (
  SELECT at, name,
         rx_bytes - lag(rx_bytes) OVER w AS drx, tx_bytes - lag(tx_bytes) OVER w AS dtx,
         rx_errors - lag(rx_errors) OVER w AS dre, tx_errors - lag(tx_errors) OVER w AS dte,
         rx_drops - lag(rx_drops) OVER w AS ddr, tx_drops - lag(tx_drops) OVER w AS ddt,
         extract(epoch FROM at - lag(at) OVER w)::float AS secs
  FROM interface_sample
  WHERE device_id = %(d)s AND (name = %(i)s OR (%(i)s = '*' AND name ~ %(re)s))
    AND at > now() - make_interval(hours => %(h)s) - interval '10 minutes'
  WINDOW w AS (PARTITION BY name ORDER BY at)
)
SELECT at AS t,
       round(sum(drx * 8 / secs))::bigint AS rx, round(sum(dtx * 8 / secs))::bigint AS tx,
       sum(greatest(coalesce(dre, 0), 0))::bigint AS rx_err, sum(greatest(coalesce(dte, 0), 0))::bigint AS tx_err,
       sum(greatest(coalesce(ddr, 0), 0))::bigint AS rx_drop, sum(greatest(coalesce(ddt, 0), 0))::bigint AS tx_drop
FROM s
WHERE secs > 0 AND secs < 3600 AND drx >= 0 AND dtx >= 0
GROUP BY at ORDER BY at
"""

TRAFFIC_1H = """
SELECT hour AS t, sum(rx_bps_avg)::bigint AS rx, sum(tx_bps_avg)::bigint AS tx,
       sum(rx_bps_max)::bigint AS rx_max, sum(tx_bps_max)::bigint AS tx_max,
       sum(rx_errors)::bigint AS rx_err, sum(tx_errors)::bigint AS tx_err,
       sum(rx_drops)::bigint AS rx_drop, sum(tx_drops)::bigint AS tx_drop
FROM interface_hourly
WHERE device_id = %(d)s AND (name = %(i)s OR (%(i)s = '*' AND name ~ %(re)s))
  AND hour > now() - make_interval(hours => %(h)s)
GROUP BY hour ORDER BY hour
"""


@endpoint(READ)
async def traffic(request: Request, p: Principal, tid: str) -> Response:
    """Provoz, chyby a zahozené pakety pro graf. interface=jméno portu, nebo * = součet
    fyzických portů. Do 48 h z 5min vzorků, déle z hodinových agregací (+ špičky)."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    hours = _int(request.query_params.get("hours"), 24, 1, 24 * 400)
    iface = str(request.query_params.get("interface") or "*")[:100]

    def q():
        with tenant_scope(st.db, tid) as tx:
            sql, res = (TRAFFIC_5M, "5m") if hours <= 48 else (TRAFFIC_1H, "1h")
            rows = tx.fetchall(sql, {"d": did, "i": iface, "h": hours, "re": PHYS_PORT_RE})
            return {"interface": iface, "resolution": res, "points": rows}

    return json_response(await run_in_threadpool(q))


# --- lokalita: NTP politika, plány, topologie ------------------------------------------


@endpoint(READ)
async def site_ntp_policy(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    module = request.path_params.get("module", "ntp")
    if module not in ("ntp", "dhcp_snooping"):
        return _err(404, "neznámá politika")
    if request.method == "POST":
        if not p.can(tid, MANAGE):
            return _err(403, "nedostatečná oprávnění")
        data = await _json(request)
        pol = await run_in_threadpool(plans.set_site_policy, st.db, tid, sid, module, data, p.user_id,
                                      client_ip(request))
        return json_response(pol)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return plans.get_site_policy(tx, sid, module)

    return json_response({"policy": await run_in_threadpool(q)})


@endpoint(OPERATE)
async def generate_plans(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    module = request.path_params["module"]
    if module == "dhcp_snooping":
        out = await run_in_threadpool(plans.generate_snooping_plans, st.db, tid, sid, p.user_id, client_ip(request))
        return json_response(out)
    if module != "ntp":
        return _err(400, "plány lokality jsou jen pro NTP a DHCP snooping")
    out = await run_in_threadpool(plans.generate_ntp_plans, st.db, tid, sid, p.user_id, client_ip(request))
    return json_response(out)


@endpoint(READ)
async def site_networks(request: Request, p: Principal, tid: str) -> Response:
    """Sítě lokality (VLAN, podsítě, DHCP), nastavení bridgů a analýza DHCP snoopingu."""
    from .. import dhcpguard

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            snoop, devs, invs = plans.site_snooping(tx, sid)
            modes = {str(d["id"]): plans.module_modes(tx, d["id"]).get("dhcp_snooping") for d in devs}
        for item in snoop["devices"]:
            item["module_mode"] = modes.get(item["device_id"])
            item.pop("apply", None)
            item.pop("revert", None)
        return {**dhcpguard.site_networks(devs, invs), "snooping": snoop}

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def plan_list(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    status = request.query_params.get("status", "draft")

    def q():
        with tenant_scope(st.db, tid) as tx:
            return tx.fetchall(
                """SELECT pl.id, pl.module, pl.summary, pl.changes, pl.script, pl.status, pl.created_at,
                          d.id AS device_id, d.serial, d.identity, s.name AS site
                   FROM config_plan pl JOIN device d ON d.id = pl.device_id LEFT JOIN site s ON s.id = d.site_id
                   WHERE pl.status::text = %(s)s ORDER BY pl.id DESC LIMIT 200""", {"s": status})

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def plan_approve(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    pid = _int(request.path_params["plan_id"], 0, 0, 2**62)
    cid = await run_in_threadpool(plans.approve_plan, st.db, st.settings, tid, pid, p.user_id, client_ip(request))
    from ..services import kick_for_command

    await run_in_threadpool(kick_for_command, st.db, st.settings, st.box, tid, cid)
    return json_response({"ok": True, "command_id": cid})


@endpoint(OPERATE)
async def plan_discard(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    pid = _int(request.path_params["plan_id"], 0, 0, 2**62)
    await run_in_threadpool(plans.discard_plan, st.db, tid, pid, p.user_id, client_ip(request))
    return json_response({"ok": True})


def build_topology(devices: list[dict], invs: dict[str, dict]) -> dict:
    """Uzly = zařízení lokality, hrany = MNDP/LLDP sousedé spárovaní podle MAC
    nebo identity. Neznámí sousedé jsou samostatné uzly (nespravovaná zařízení)."""
    mac_owner: dict[str, str] = {}
    ident_owner: dict[str, str] = {}
    for d in devices:
        did = str(d["id"])
        for i in invs.get(did, {}).get("interfaces") or []:
            if isinstance(i, dict) and i.get("mac-address"):
                mac_owner[str(i["mac-address"]).upper()] = did
        if d.get("identity"):
            ident_owner[str(d["identity"])] = did
    nodes = [{"id": str(d["id"]), "label": d.get("identity") or d["serial"], "managed": True,
              "state": d.get("state"), "model": d.get("model")} for d in devices]
    unknown: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    for d in devices:
        a = str(d["id"])
        for n in invs.get(a, {}).get("neighbors") or []:
            if not isinstance(n, dict):
                continue
            mac = str(n.get("mac-address", "")).upper()
            b = mac_owner.get(mac) or ident_owner.get(str(n.get("identity", "")))
            if not b:
                key = mac or str(n.get("identity"))
                b = f"ext:{key}"
                unknown.setdefault(b, {"id": b, "label": n.get("identity") or mac, "managed": False,
                                       "model": n.get("board") or n.get("platform"), "address": n.get("address")})
            if b == a:
                continue
            local_if = str(n.get("interface", "")).split(",")[0]
            k = tuple(sorted((a, b)))
            e = edges.setdefault(k, {"a": k[0], "b": k[1], "ports": {}})
            e["ports"][a] = local_if
            if n.get("interface-name"):
                e["ports"].setdefault(b, n.get("interface-name"))
    return {"nodes": nodes + list(unknown.values()), "edges": list(edges.values())}


@endpoint(READ)
async def topology(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            devs = tx.fetchall("""SELECT id, serial, identity, model, state FROM device
                                  WHERE site_id = %(s)s AND state <> 'removed'""", {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
        return build_topology(devs, invs)

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def site_clients(request: Request, p: Principal, tid: str) -> Response:
    from .. import clientmeta, clients

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    history = request.query_params.get("history") == "1"

    def q():
        with tenant_scope(st.db, tid) as tx:
            if history:
                return clientmeta.annotate(tx, [dict(r) for r in clients.history(
                    tx, sid, _int(request.query_params.get("days"), 30, 1, 90))])
            devs = tx.fetchall("""SELECT id, serial, identity FROM device
                                  WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""", {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"], clients.SECTIONS) for d in devs}
            seen = {r["mac"]: r for r in tx.fetchall(
                "SELECT mac, first_seen, last_seen FROM client WHERE site_id = %(s)s", {"s": sid})}
            out = clients.correlate(devs, invs)
            clientmeta.annotate(tx, out)
        for c in out:
            h = seen.get(c["mac"])
            c["first_seen"] = h["first_seen"] if h else None
            c["last_seen"] = h["last_seen"] if h else None
            c["online"] = True
        return out

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def client_search(request: Request, p: Principal, tid: str) -> Response:
    """Hledání klienta ve všech lokalitách tenanta (historie, MAC/IP/jméno)."""
    from .. import clients

    st = get_state(request)
    q = str(request.query_params.get("q") or "")[:64]

    def run():
        with tenant_scope(st.db, tid) as tx:
            return clients.search(tx, q)

    return json_response(await run_in_threadpool(run))


@endpoint(READ)
async def site_wifi(request: Request, p: Principal, tid: str) -> Response:
    from .. import wifi

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            devs = tx.fetchall("""SELECT id, serial, identity FROM device
                                  WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""", {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
        return wifi.site_wifi(devs, invs)

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def site_infra(request: Request, p: Principal, tid: str) -> Response:
    from .. import clientmeta, infra

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            devs = tx.fetchall("""SELECT id, serial, identity, model, role::text AS role, state::text AS state, ros_version
                                  FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline', 'provisioning')""",
                               {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"], INFRA_SECTIONS) for d in devs}
            # připojení klienti (poslední inventura klientů do 10 min) s MAC, popiskem a logem
            cl = [dict(r) for r in tx.fetchall(
                """SELECT mac, ip, hostname, device_id, port, vlan, ssid, signal FROM client
                   WHERE site_id = %(s)s AND last_seen > now() - interval '10 minutes' AND device_id IS NOT NULL
                   ORDER BY port, mac""", {"s": sid})]
            clientmeta.annotate(tx, cl)
        out = infra.site_infra(devs, invs)
        by_dev: dict[str, list] = {}
        for c in cl:
            c["device_id"] = str(c["device_id"])
            by_dev.setdefault(c["device_id"], []).append(c)
        for n in out["nodes"]:
            n["clients"] = by_dev.get(n["id"], [])
        return out

    return json_response(await run_in_threadpool(q))


INFRA_SECTIONS = ["interfaces", "neighbors", "resource", "ethernet_monitor", "poe_monitor", "stp_bridges",
                  "stp_ports", "bridges", "bridge_ports", "routes_default", "dhcp_client", "addresses"]


@endpoint(READ)
async def site_stp(request: Request, p: Principal, tid: str) -> Response:
    from .. import stp

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            devs = tx.fetchall("""SELECT id, serial, identity FROM device
                                  WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""", {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
        return stp.site_stp(devs, invs)

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def site_upgrade(request: Request, p: Principal, tid: str) -> Response:
    """GET: náhled pořadí a poslední úlohy; POST (MANAGE): spustí postupnou aktualizaci."""
    from .. import upgrades

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    if request.method == "POST":
        if not p.can(tid, MANAGE):
            return _err(403, "nedostatečná oprávnění")
        data = await _json(request)
        order = data.get("order") if isinstance(data, dict) else None
        res = await run_in_threadpool(upgrades.start, st.db, tid, sid, p.user_id, client_ip(request), order)
        return json_response(res, 201)

    def q():
        with tenant_scope(st.db, tid) as tx:
            cands = upgrades.candidates(tx, sid)
            return {"candidates": [{"device_id": str(c["id"]), "label": c["identity"] or c["serial"],
                                    "role": c["role"], "from": (c["ros_version"] or "").split(" ")[0],
                                    "version": c["latest"], "depth": c.get("depth")} for c in cands],
                    "jobs": upgrades.jobs(tx, sid)}

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def upgrade_cancel(request: Request, p: Principal, tid: str) -> Response:
    from .. import upgrades

    st = get_state(request)
    jid = _int(request.path_params["job_id"], 0, 0, 2**62)
    await run_in_threadpool(upgrades.cancel, st.db, tid, jid, p.user_id, client_ip(request))
    return json_response({"ok": True})


@endpoint(READ)
async def alert_list(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    open_only = request.query_params.get("all") != "1"

    def q():
        with tenant_scope(st.db, tid) as tx:
            return tx.fetchall(
                f"""SELECT a.id, a.device_id, d.identity, d.serial, s.name AS site, a.kind, a.subject,
                           a.severity::text AS severity, a.message, a.opened_at, a.last_seen_at, a.resolved_at,
                           a.acknowledged_at, u.email AS acknowledged_by
                    FROM alert a JOIN device d ON d.id = a.device_id LEFT JOIN site s ON s.id = d.site_id
                    LEFT JOIN app_user u ON u.id = a.acknowledged_by
                    {"WHERE a.resolved_at IS NULL" if open_only else ""}
                    ORDER BY a.resolved_at IS NULL DESC,
                             CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, a.opened_at DESC
                    LIMIT 300""")

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def alert_ack(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    aid = _int(request.path_params["alert_id"], 0, 0, 2**62)

    def q():
        with tenant_scope(st.db, tid) as tx:
            n = tx.execute("""UPDATE alert SET acknowledged_by = %(u)s, acknowledged_at = now()
                              WHERE id = %(i)s AND acknowledged_at IS NULL""", {"u": p.user_id, "i": aid})
            if not n:
                raise NotFound("alert neexistuje nebo je už potvrzený")
            audit(tx, "alert.ack", tenant_id=tid, user_id=p.user_id, target=f"alert:{aid}", ip=client_ip(request))

    await run_in_threadpool(q)
    return json_response({"ok": True})


# --- řízený režim (F5a): záměr lokality, převzetí, sladění ------------------------------------------

@endpoint(READ)
async def site_intent_get(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    return json_response(await run_in_threadpool(takeover.site_intent_view, st.db, tid, sid))


@endpoint(MANAGE)
async def site_intent_put(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    data = await _json(request)
    new = await run_in_threadpool(takeover.set_site_intent, st.db, tid, sid, data, p.user_id, client_ip(request))
    return json_response({"ok": True, "intent": new})


@endpoint(MANAGE)
async def site_intent_reconcile(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    out = await run_in_threadpool(takeover.generate_switch_plans, st.db, st.settings, tid, sid, p.user_id,
                                  client_ip(request))
    from ..services import kick

    for o in out:
        if o.get("result") == "auto":
            kick(st.db, st.settings, st.box, tid, o["device_id"])
    return json_response({"devices": out})


@endpoint(READ)
async def device_takeover_get(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    return json_response(await run_in_threadpool(takeover.preview, st.db, tid, did))


@endpoint(MANAGE)
async def device_takeover_post(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    out = await run_in_threadpool(takeover.create_takeover_plan, st.db, st.settings, st.box, tid, did,
                                  data.get("confirm_custom") is True, p.user_id, client_ip(request))
    return json_response(out, 201)


@endpoint(OPERATE)
async def device_intent_put(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    new = await run_in_threadpool(takeover.set_device_intent, st.db, tid, did, data, p.user_id, client_ip(request))
    return json_response({"ok": True, "device_intent": new})


@endpoint(MANAGE)
async def site_ssid_create(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    data = await _json(request)
    out = await run_in_threadpool(takeover.save_ssid, st.db, st.box, tid, sid, data, None, p.user_id, client_ip(request))
    return json_response(out, 201)


@endpoint(MANAGE)
async def site_ssid_update(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    ssid_id = _uuid(request.path_params["ssid_id"])
    data = await _json(request)
    out = await run_in_threadpool(takeover.save_ssid, st.db, st.box, tid, sid, data, ssid_id, p.user_id,
                                  client_ip(request))
    return json_response(out)


@endpoint(MANAGE)
async def site_ssid_delete(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    await run_in_threadpool(takeover.delete_ssid, st.db, tid, _uuid(request.path_params["site_id"]),
                            _uuid(request.path_params["ssid_id"]), p.user_id, client_ip(request))
    return json_response({"ok": True})


@endpoint(READ)
async def site_wifi_preview(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            w = takeover.wifi_preview(tx, sid)
            w["tables"] = [{k: t[k] for k in ("device_id", "device", "table", "sha", "installed")}
                           for t in takeover.wifi_tables(tx, sid)]
        w.pop("host_inv", None)
        return w

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def site_wifi_plan(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    out = await run_in_threadpool(takeover.create_wifi_plan, st.db, st.box, tid, sid, p.user_id, client_ip(request))
    return json_response(out, 201)


@endpoint(READ)
async def site_ssids(request: Request, p: Principal, tid: str) -> Response:
    """SSID lokality včetně jmen a VLAN více hesel (hesla ne)."""
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            return takeover.list_ssids(tx, sid)

    return json_response({"ssids": await run_in_threadpool(q)})


@endpoint(MANAGE)
async def site_ssid_ppsk_reveal(request: Request, p: Principal, tid: str) -> Response:
    from .. import takeover

    st = get_state(request)
    out = await run_in_threadpool(takeover.reveal_ppsk, st.db, st.box, tid, _uuid(request.path_params["site_id"]),
                                  _uuid(request.path_params["ssid_id"]), p.user_id, client_ip(request))
    return json_response({"ppsk": out})


@endpoint(OPERATE)
async def site_ap_group(request: Request, p: Principal, tid: str) -> Response:
    """Přesun AP do skupiny: {"devices": [...], "group": "g-…" | null}."""
    from .. import takeover

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    data = await _json(request)
    group = data.get("group")
    if group is not None and not isinstance(group, str):
        raise ServiceError("neplatná skupina")
    devs = data.get("devices")
    if not isinstance(devs, list):
        raise ServiceError("vyber zařízení")
    out = await run_in_threadpool(takeover.set_ap_group, st.db, tid, sid, [_uuid(str(d)) for d in devs], group,
                                  p.user_id, client_ip(request))
    return json_response(out)


@endpoint(READ)
async def tenant_security(request: Request, p: Principal, tid: str) -> Response:
    """Bezpečnostní nálezy všech zařízení tenanta (pro proklik z přehledu) s možností opravy."""
    from .. import hardening

    st = get_state(request)

    def q():
        with tenant_scope(st.db, tid) as tx:
            rows = tx.fetchall(
                """SELECT d.id, d.serial, d.identity, d.state, s.name AS site, i.data AS findings
                   FROM device d JOIN device_inventory i ON i.device_id = d.id AND i.section = 'hardening'
                   LEFT JOIN site s ON s.id = d.site_id
                   WHERE d.state <> 'removed' ORDER BY s.name, d.identity, d.serial""")
            plans_open = {str(r["device_id"]) for r in tx.fetchall(
                """SELECT device_id FROM config_plan WHERE module = 'hardening'
                   AND status IN ('draft', 'queued', 'confirming')""")}
        out = []
        for r in rows:
            f = [x for x in (r["findings"] if isinstance(r["findings"], list) else []) if isinstance(x, dict)]
            if f:
                out.append({"id": str(r["id"]), "serial": r["serial"], "identity": r["identity"], "state": r["state"],
                            "site": r["site"], "findings": hardening.fixable(f), "plan_open": str(r["id"]) in plans_open})
        return out

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def device_hardening_fix(request: Request, p: Principal, tid: str) -> Response:
    """Plán opravy vybraných bezpečnostních nálezů (s automatickým návratem)."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    out = await run_in_threadpool(plans.create_hardening_plan, st.db, tid, did, data.get("codes"), p.user_id,
                                  client_ip(request))
    return json_response(out, 201)


@endpoint(READ)
async def device_logs(request: Request, p: Principal, tid: str) -> Response:
    """Logy zařízení (syslog) + úroveň a objem dat po dnech."""
    from .. import devlog

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    qp = request.query_params

    def q():
        with tenant_scope(st.db, tid) as tx:
            d = tx.fetchone("SELECT syslog_level, syslog_wifi FROM device WHERE id = %(d)s", {"d": did})
            if not d:
                raise NotFound("zařízení neexistuje")
            before = _int(qp.get("before"), 0, 0, 2 ** 62) or None
            logs = devlog.query(tx, did, str(qp.get("q") or "")[:100], _int(qp.get("sev"), 7, 0, 7),
                                _int(qp.get("limit"), 300, 1, 1000), before)
            return {"level": d["syslog_level"], "wifi": d["syslog_wifi"], "logs": logs, "volume": devlog.volume(tx, did),
                    "retention_days": st.settings.syslog.retention_days}

    return json_response(await run_in_threadpool(q))


@endpoint(MANAGE)
async def device_syslog(request: Request, p: Principal, tid: str) -> Response:
    from .. import devlog
    from ..services import kick

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    level = str(data["level"]) if data.get("level") is not None else None
    wifi = bool(data["wifi"]) if "wifi" in data else None
    out = await run_in_threadpool(devlog.set_level, st.db, st.settings, tid, did, level, p.user_id, client_ip(request), wifi)
    kick(st.db, st.settings, st.box, tid, did)
    return json_response(out)


@endpoint(MANAGE)
async def site_syslog(request: Request, p: Principal, tid: str) -> Response:
    """Úroveň logů pro všechna spravovaná zařízení lokality."""
    from .. import devlog
    from ..services import kick

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    data = await _json(request)
    level = str(data["level"]) if data.get("level") is not None else None
    wifi = bool(data["wifi"]) if "wifi" in data else None
    if level is None and wifi is None:
        raise ServiceError("nic ke změně (level, wifi)")

    def run():
        with tenant_scope(st.db, tid) as tx:
            # Wi-Fi události jen tam, kde je Wi-Fi (rádia nebo CAPsMAN) – ostatní zařízení je nemají
            ids = [str(r["id"]) for r in tx.fetchall(
                """SELECT id FROM device d WHERE site_id = %(s)s AND state IN ('adopted', 'offline')
                     AND (%(only)s::boolean IS NOT TRUE OR EXISTS (SELECT 1 FROM device_inventory i WHERE i.device_id = d.id
                          AND i.section IN ('wifi', 'wireless') AND jsonb_typeof(i.data) = 'array'
                          AND jsonb_array_length(i.data) > 0))""", {"s": sid, "only": level is None})]
        for d in ids:
            devlog.set_level(st.db, st.settings, tid, d, level, p.user_id, client_ip(request), wifi)
        return ids

    ids = await run_in_threadpool(run)
    for d in ids:
        kick(st.db, st.settings, st.box, tid, d)
    return json_response({"level": level, "devices": len(ids)})


@endpoint(READ)
async def site_wifi_journey(request: Request, p: Principal, tid: str) -> Response:
    """Wi-Fi časová osa klienta: u kterého AP byl, signál, roaming, odpojení s důvodem, nálezy."""
    from .. import clientmeta, leases, wifidiag

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    mac = leases.norm_mac(request.query_params.get("mac"))
    if not mac:
        raise ServiceError("neplatná MAC adresa")
    hours = _int(request.query_params.get("hours"), 24, 1, 168)

    def q():
        with tenant_scope(st.db, tid) as tx:
            j = wifidiag.client_journey(tx, sid, mac, hours)
            j["client"] = clientmeta.annotate(tx, [{"mac": mac}])[0]
            c = tx.fetchone("SELECT hostname, ip FROM client WHERE site_id = %(s)s AND mac = %(m)s", {"s": sid, "m": mac})
            j["client"].update(c or {})
        return j

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def site_wifi_roaming(request: Request, p: Principal, tid: str) -> Response:
    """Roaming lokality: klienti podle problémů a matice přechodů mezi AP."""
    from .. import clientmeta, wifidiag

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    hours = _int(request.query_params.get("hours"), 24, 1, 168)

    def q():
        with tenant_scope(st.db, tid) as tx:
            r = wifidiag.site_roaming(tx, sid, hours)
            clientmeta.annotate(tx, r["clients"])
            names = {x["mac"]: x for x in tx.fetchall("SELECT mac, hostname FROM client WHERE site_id = %(s)s", {"s": sid})}
            for c in r["clients"]:
                c["hostname"] = (names.get(c["mac"]) or {}).get("hostname")
            r["logging"] = tx.fetchone("""SELECT count(*) FILTER (WHERE syslog_wifi OR syslog_level = 'info') AS on_, count(*) AS n
                                         FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""", {"s": sid})
        return r

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def device_bwtests(request: Request, p: Principal, tid: str) -> Response:
    """Testy rychlosti zařízení a možné cíle (online zařízení tenanta s jejich adresami)."""
    from .. import bwtest

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            tests = bwtest.list_tests(tx, did)
            devs = tx.fetchall(
                """SELECT d.id, d.identity, d.serial, d.model, s.name AS site, d.site_id FROM device d
                   LEFT JOIN site s ON s.id = d.site_id WHERE d.state = 'adopted' AND d.id <> %(d)s
                   ORDER BY s.name, d.identity""", {"d": did})
            me = tx.fetchone("SELECT site_id FROM device WHERE id = %(d)s", {"d": did})
            targets = []
            for d in devs:
                addrs = bwtest.target_addresses(inventory.load(tx, d["id"], ["addresses"]))
                if addrs:
                    targets.append({"id": str(d["id"]), "name": d["identity"] or d["serial"], "model": d["model"],
                                    "site": d["site"], "same_site": bool(me) and d["site_id"] == me["site_id"],
                                    "addresses": addrs})
        return {"tests": tests, "targets": targets, "durations": list(bwtest.DURATIONS)}

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def device_bwtest_start(request: Request, p: Principal, tid: str) -> Response:
    from .. import bwtest

    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    data = await _json(request)
    out = await run_in_threadpool(
        bwtest.start, st.db, st.box, tid, did, _uuid(data.get("to")), str(data.get("address") or ""),
        _int(data.get("duration"), 10, 1, 60), str(data.get("direction") or "both"), str(data.get("protocol") or "tcp"),
        p.user_id, client_ip(request))
    return json_response(out, 201)


@endpoint(READ)
async def device_activity(request: Request, p: Principal, tid: str) -> Response:
    """Rozpracované plány a příkazy zařízení – UI podle toho sleduje průběh změny."""
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            d = tx.fetchone("SELECT state, inventory_at, last_checkin_at FROM device WHERE id = %(d)s", {"d": did})
            if not d:
                raise NotFound("zařízení neexistuje")
            pl = tx.fetchall(
                """SELECT id, module, summary, status::text AS status, confirm_deadline FROM config_plan
                   WHERE device_id = %(d)s AND (status IN ('queued', 'confirming')
                         OR finished_at > now() - interval '10 minutes' OR confirmed_at > now() - interval '10 minutes')
                   ORDER BY id DESC LIMIT 10""", {"d": did})
            cmds = tx.fetchall(
                """SELECT id, kind, status::text AS status, created_at, sent_at, finished_at FROM device_command
                   WHERE device_id = %(d)s AND (status IN ('queued', 'sent') OR finished_at > now() - interval '10 minutes')
                   ORDER BY id DESC LIMIT 10""", {"d": did})
        busy = any(x["status"] in ("queued", "confirming") for x in pl) or any(c["status"] in ("queued", "sent") for c in cmds)
        return {**d, "plans": pl, "commands": cmds, "busy": busy}

    return json_response(await run_in_threadpool(q))


# --- výchozí konfigurace a ohlášení klíčem lokality ---------------------------------------------------

def _defaults_view(st, key: str) -> dict:
    from .. import defaults, routeros

    opts = routeros.fetch_opts(st.settings)
    base = f"https://{st.settings.web.checkin_host}"
    return {
        "roles": [{"role": r, "label": label, "device_role": defaults.DEVICE_ROLE[r],
                   "one_liner": defaults.one_liner(f"{base}/enroll/{key}/default/{r}", opts)}
                  for r, label in defaults.ROLES.items()],
        "enroll_base": f"{base}/enroll/",
    }


@endpoint(MANAGE)
async def site_defaults(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    from ..services import site_enroll_key

    rotate = request.method == "POST"
    key = await run_in_threadpool(site_enroll_key, st.db, st.box, tid, sid, rotate, p.user_id, client_ip(request))
    return json_response(_defaults_view(st, key))


@endpoint(MANAGE)
async def site_default_file(request: Request, p: Principal, tid: str) -> Response:
    from .. import defaults, routeros
    from ..services import site_enroll_key

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    role = request.path_params["role"]
    if role not in defaults.ROLES:
        raise NotFound("neznámá role")
    key = await run_in_threadpool(site_enroll_key, st.db, st.box, tid, sid, False, p.user_id, client_ip(request))
    opts = routeros.fetch_opts(st.settings)
    body = defaults.script(role, f"https://{st.settings.web.checkin_host}/enroll/{key}?role={role}", opts)
    return Response(body, media_type="text/plain; charset=us-ascii",
                    headers={"Content-Disposition": f'attachment; filename="mtc-default-{role}.rsc"'})


# --- výchozí hesla ze štítků -----------------------------------------------------------------------

@endpoint(OPERATE)
async def factory_passwords(request: Request, p: Principal, tid: str) -> Response:
    from .. import factorypw

    st = get_state(request)
    if request.method == "GET":
        return json_response(await run_in_threadpool(factorypw.listing, st.db, tid))
    if not p.can(tid, MANAGE):
        raise NotFound("nenalezeno")
    data = await _json(request)
    out = await run_in_threadpool(factorypw.save, st.db, st.box, tid, str(data.get("serial", "")),
                                  str(data.get("password", "")), data.get("note"), p.user_id, client_ip(request))
    return json_response(out, 201)


@endpoint(MANAGE)
async def factory_passwords_import(request: Request, p: Principal, tid: str) -> Response:
    from .. import factorypw

    st = get_state(request)
    data = await _json(request)
    out = await run_in_threadpool(factorypw.import_csv, st.db, st.box, tid, str(data.get("csv", "")), p.user_id,
                                  client_ip(request))
    return json_response(out)


@endpoint(MANAGE)
async def factory_password_reveal(request: Request, p: Principal, tid: str) -> Response:
    from .. import factorypw

    st = get_state(request)
    pw = await run_in_threadpool(factorypw.reveal, st.db, st.box, tid, request.path_params["serial"], p.user_id,
                                 client_ip(request))
    return json_response({"password": pw})


@endpoint(MANAGE)
async def factory_password_delete(request: Request, p: Principal, tid: str) -> Response:
    from .. import factorypw

    st = get_state(request)
    await run_in_threadpool(factorypw.delete, st.db, tid, request.path_params["serial"], p.user_id, client_ip(request))
    return json_response({"ok": True})


def routes() -> list[Route]:
    d = T + "/devices/{device_id}"
    s = T + "/sites/{site_id}"
    return [
        Route(d, device_detail, methods=["GET"]),
        Route(d, device_update, methods=["PATCH"]),
        Route(d + "/modules", device_module, methods=["POST"]),
        Route(d + "/activity", device_activity, methods=["GET"]),
        Route(d + "/logs", device_logs, methods=["GET"]),
        Route(d + "/syslog", device_syslog, methods=["POST"]),
        Route(s + "/syslog", site_syslog, methods=["POST"]),
        Route(s + "/wifi/journey", site_wifi_journey, methods=["GET"]),
        Route(s + "/wifi/roaming", site_wifi_roaming, methods=["GET"]),
        Route(d + "/bwtest", device_bwtests, methods=["GET"]),
        Route(d + "/bwtest", device_bwtest_start, methods=["POST"]),
        Route(d + "/hardening-fix", device_hardening_fix, methods=["POST"]),
        Route(T + "/security", tenant_security, methods=["GET"]),
        Route(d + "/takeover", device_takeover_get, methods=["GET"]),
        Route(d + "/takeover", device_takeover_post, methods=["POST"]),
        Route(d + "/intent", device_intent_put, methods=["PUT"]),
        Route(d + "/backup", request_backup, methods=["POST"]),
        Route(d + "/backups/{backup_id}/diff", backup_diff, methods=["GET"]),
        Route(d + "/backups/{backup_id}/download", backup_download, methods=["GET"]),
        Route(d + "/vlan-migration", vlan_migration, methods=["GET"]),
        Route(d + "/metrics", metrics, methods=["GET"]),
        Route(d + "/traffic", traffic, methods=["GET"]),
        Route(d + "/release-notes", release_notes, methods=["GET"]),
        Route(d + "/port-history", port_history, methods=["GET"]),
        Route(d + "/ports/{port}", port_get, methods=["GET"]),
        Route(d + "/ports/{port}/plan", port_plan, methods=["POST"]),
        Route(d + "/dhcp/{server}", dhcp_server_view, methods=["GET"]),
        Route(d + "/dhcp/{server}/lease", dhcp_lease_plan, methods=["POST"]),
        Route(d + "/dhcp/{server}/network", dhcp_network_plan, methods=["POST"]),
        Route(s + "/clients/{mac}/dhcp", client_dhcp, methods=["GET"]),
        Route(s + "/policy/ntp", site_ntp_policy, methods=["GET", "POST"]),
        Route(s + "/policy/{module}", site_ntp_policy, methods=["GET", "POST"]),
        Route(s + "/networks", site_networks, methods=["GET"]),
        Route(s + "/intent", site_intent_get, methods=["GET"]),
        Route(s + "/intent", site_intent_put, methods=["PUT"]),
        Route(s + "/intent/reconcile", site_intent_reconcile, methods=["POST"]),
        Route(s + "/ssids", site_ssids, methods=["GET"]),
        Route(s + "/ssids", site_ssid_create, methods=["POST"]),
        Route(s + "/ssids/{ssid_id}/ppsk/reveal", site_ssid_ppsk_reveal, methods=["POST"]),
        Route(s + "/ap-groups", site_ap_group, methods=["POST"]),
        Route(s + "/ssids/{ssid_id}", site_ssid_update, methods=["PUT"]),
        Route(s + "/ssids/{ssid_id}", site_ssid_delete, methods=["DELETE"]),
        Route(s + "/wifi/preview", site_wifi_preview, methods=["GET"]),
        Route(s + "/wifi/plan", site_wifi_plan, methods=["POST"]),
        Route(s + "/defaults", site_defaults, methods=["GET", "POST"]),
        Route(s + "/defaults/{role}.rsc", site_default_file, methods=["GET"]),
        Route(T + "/factory-passwords", factory_passwords, methods=["GET", "POST"]),
        Route(T + "/factory-passwords/import", factory_passwords_import, methods=["POST"]),
        Route(T + "/factory-passwords/{serial}/reveal", factory_password_reveal, methods=["POST"]),
        Route(T + "/factory-passwords/{serial}", factory_password_delete, methods=["DELETE"]),
        Route(s + "/plans/{module}", generate_plans, methods=["POST"]),
        Route(s + "/topology", topology, methods=["GET"]),
        Route(s + "/clients", site_clients, methods=["GET"]),
        Route(s + "/wifi", site_wifi, methods=["GET"]),
        Route(s + "/stp", site_stp, methods=["GET"]),
        Route(s + "/infra", site_infra, methods=["GET"]),
        Route(s + "/upgrade", site_upgrade, methods=["GET", "POST"]),
        Route(T + "/upgrade-jobs/{job_id}/cancel", upgrade_cancel, methods=["POST"]),
        Route(T + "/port-profiles", port_profiles, methods=["GET", "POST"]),
        Route(T + "/client-notes/{mac}", client_note, methods=["PUT"]),
        Route(T + "/client-logos", client_logos, methods=["GET"]),
        Route(T + "/client-logos/{scope}/{key}", client_logo, methods=["GET", "PUT", "DELETE"]),
        Route(T + "/port-profiles/{profile_id}", port_profile_delete, methods=["DELETE"]),
        Route(T + "/port-profiles/{profile_id}", port_profile_update, methods=["PUT"]),
        Route(d + "/tools/{tool}", device_tool, methods=["POST"]),
        Route(d + "/bonds", device_bond, methods=["POST"]),
        Route(T + "/ports/bulk", ports_bulk, methods=["POST"]),
        Route(T + "/mlag", mlag_create, methods=["POST"]),
        Route(s + "/ports", site_ports, methods=["GET"]),
        Route(s + "/wifi/rogue", site_rogue, methods=["GET", "POST"]),
        Route(T + "/plans", plan_list, methods=["GET"]),
        Route(T + "/clients", client_search, methods=["GET"]),
        Route(T + "/alerts", alert_list, methods=["GET"]),
        Route(T + "/alerts/{alert_id}/ack", alert_ack, methods=["POST"]),
        Route(T + "/plans/{plan_id}/approve", plan_approve, methods=["POST"]),
        Route(T + "/plans/{plan_id}/discard", plan_discard, methods=["POST"]),
    ]
