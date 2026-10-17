"""Admin API: detail zařízení, moduly, zálohy, plány změn, topologie."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import analysis, backup, firewall, inventory, plans, ports, profiles, ros_versions
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
                          d.backup_at
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
        ifaces = [{
            "name": i.get("name"), "type": i.get("type"), "running": i.get("running") == "true",
            "disabled": i.get("disabled") == "true", "mac": i.get("mac-address"), "comment": i.get("comment"),
            "link_downs": i.get("link-downs"), "rx_error": i.get("rx-error"), "tx_error": i.get("tx-error"),
        } for i in (inv.get("interfaces") or []) if isinstance(i, dict) and i.get("dynamic") != "true"]
        pinfo = ports.port_info(inv)
        for i in ifaces:
            i["port"] = pinfo.get(str(i["name"]))
        return {
            "device": d, "summary": _summary(inv), "modules": modes,
            "warnings": [{"severity": i.severity, "text": i.text, "source": i.source}
                         for i in ros_versions.issues_for(d.get("ros_version") or "")],
            "interfaces": ifaces,
            "addresses": inv.get("addresses") or [],
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
            return {**state, "known_vlans": vlans, "uplink": port in _uplinks(tx, inv), "module_mode": mode,
                    "profiles": profs, "profile": match["id"] if match else None}

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
    if request.method == "POST":
        if not p.can(tid, MANAGE):
            return _err(403, "nedostatečná oprávnění")
        data = await _json(request)
        pol = await run_in_threadpool(plans.set_site_policy, st.db, tid, sid, "ntp", data, p.user_id,
                                      client_ip(request))
        return json_response(pol)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return plans.get_site_policy(tx, sid, "ntp")

    return json_response({"policy": await run_in_threadpool(q)})


@endpoint(OPERATE)
async def generate_plans(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])
    module = request.path_params["module"]
    if module != "ntp":
        return _err(400, "plány lokality jsou zatím jen pro NTP")
    out = await run_in_threadpool(plans.generate_ntp_plans, st.db, tid, sid, p.user_id, client_ip(request))
    return json_response(out)


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
    from .. import clients

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    history = request.query_params.get("history") == "1"

    def q():
        with tenant_scope(st.db, tid) as tx:
            if history:
                return clients.history(tx, sid, _int(request.query_params.get("days"), 30, 1, 90))
            devs = tx.fetchall("""SELECT id, serial, identity FROM device
                                  WHERE site_id = %(s)s AND state IN ('adopted', 'offline')""", {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"], clients.SECTIONS) for d in devs}
            seen = {r["mac"]: r for r in tx.fetchall(
                "SELECT mac, first_seen, last_seen FROM client WHERE site_id = %(s)s", {"s": sid})}
        out = clients.correlate(devs, invs)
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
    from .. import infra

    st = get_state(request)
    sid = _uuid(request.path_params["site_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            devs = tx.fetchall("""SELECT id, serial, identity, model, role::text AS role, state::text AS state, ros_version
                                  FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline', 'provisioning')""",
                               {"s": sid})
            invs = {str(d["id"]): inventory.load(tx, d["id"], INFRA_SECTIONS) for d in devs}
        return infra.site_infra(devs, invs)

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
        res = await run_in_threadpool(upgrades.start, st.db, tid, sid, p.user_id, client_ip(request))
        return json_response(res, 201)

    def q():
        with tenant_scope(st.db, tid) as tx:
            cands = upgrades.candidates(tx, sid)
            return {"candidates": [{"device_id": str(c["id"]), "label": c["identity"] or c["serial"],
                                    "role": c["role"], "from": (c["ros_version"] or "").split(" ")[0],
                                    "version": c["latest"]} for c in cands],
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


def routes() -> list[Route]:
    d = T + "/devices/{device_id}"
    s = T + "/sites/{site_id}"
    return [
        Route(d, device_detail, methods=["GET"]),
        Route(d, device_update, methods=["PATCH"]),
        Route(d + "/modules", device_module, methods=["POST"]),
        Route(d + "/backup", request_backup, methods=["POST"]),
        Route(d + "/backups/{backup_id}/diff", backup_diff, methods=["GET"]),
        Route(d + "/backups/{backup_id}/download", backup_download, methods=["GET"]),
        Route(d + "/vlan-migration", vlan_migration, methods=["GET"]),
        Route(d + "/metrics", metrics, methods=["GET"]),
        Route(d + "/traffic", traffic, methods=["GET"]),
        Route(d + "/release-notes", release_notes, methods=["GET"]),
        Route(d + "/ports/{port}", port_get, methods=["GET"]),
        Route(d + "/ports/{port}/plan", port_plan, methods=["POST"]),
        Route(s + "/policy/ntp", site_ntp_policy, methods=["GET", "POST"]),
        Route(s + "/plans/{module}", generate_plans, methods=["POST"]),
        Route(s + "/topology", topology, methods=["GET"]),
        Route(s + "/clients", site_clients, methods=["GET"]),
        Route(s + "/wifi", site_wifi, methods=["GET"]),
        Route(s + "/stp", site_stp, methods=["GET"]),
        Route(s + "/infra", site_infra, methods=["GET"]),
        Route(s + "/upgrade", site_upgrade, methods=["GET", "POST"]),
        Route(T + "/upgrade-jobs/{job_id}/cancel", upgrade_cancel, methods=["POST"]),
        Route(T + "/port-profiles", port_profiles, methods=["GET", "POST"]),
        Route(T + "/port-profiles/{profile_id}", port_profile_delete, methods=["DELETE"]),
        Route(T + "/plans", plan_list, methods=["GET"]),
        Route(T + "/clients", client_search, methods=["GET"]),
        Route(T + "/alerts", alert_list, methods=["GET"]),
        Route(T + "/alerts/{alert_id}/ack", alert_ack, methods=["POST"]),
        Route(T + "/plans/{plan_id}/approve", plan_approve, methods=["POST"]),
        Route(T + "/plans/{plan_id}/discard", plan_discard, methods=["POST"]),
    ]
