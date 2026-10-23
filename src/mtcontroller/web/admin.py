"""Admin API (`/api/admin/…`) a statické admin UI (`/`).

Každý požadavek kromě přihlášení vyžaduje platnou relaci s dokončeným MFA.
Změnové požadavky (POST) navíc vyžadují hlavičku X-CSRF-Token.
Oprávnění se kontrolují per tenant (Principal.can) a data se čtou v tenant_scope,
takže i chyba v kontrole oprávnění nepustí k datům jiného tenanta (RLS).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import auth, health, ros_versions, services
from ..auth import MANAGE, OPERATE, READ, AuthError, Principal
from ..db import operator_scope, tenant_scope
from ..services import NotFound, ServiceError, audit
from .app import client_ip, get_state, json_response

COOKIE = "mtc_session"
STATIC = Path(__file__).resolve().parent / "static"
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                 ".css": "text/css"}

# --- rate limit přihlášení (v paměti procesu) -----------------------------------

_attempts: dict[str, deque] = defaultdict(deque)
LOGIN_PER_MINUTE = 10


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    q = _attempts[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= LOGIN_PER_MINUTE:
        return True
    q.append(now)
    return False


# --- pomocné ------------------------------------------------------------------


def _err(status: int, detail: str) -> Response:
    return json_response({"detail": detail}, status)


async def _json(request: Request, limit: int = 65536) -> dict:
    raw = await request.body()
    return services.parse_json_body(raw, limit)


def _uuid(v: Any) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except ValueError as e:
        raise NotFound("neplatné ID") from e


async def _principal(request: Request) -> Principal | None:
    st = get_state(request)
    return await run_in_threadpool(auth.load_principal, st.db, request.cookies.get(COOKIE))


def endpoint(action: str | None, mfa: bool = True) -> Callable:
    """Dekorátor: ověří relaci, MFA, CSRF a (je-li v cestě tenant) oprávnění."""

    def deco(fn: Callable) -> Callable:
        async def wrapper(request: Request) -> Response:
            p = await _principal(request)
            if not p:
                return _err(401, "nepřihlášen")
            if mfa and not p.mfa_ok:
                return _err(401, "vyžadováno MFA")
            if request.method != "GET" and not auth.check_csrf(p, request.headers.get("x-csrf-token")):
                return _err(403, "chybí nebo nesedí CSRF token")
            tid = request.path_params.get("tenant_id")
            if tid is not None:
                try:
                    tid = _uuid(tid)
                except NotFound:
                    return _err(404, "nenalezeno")
                if action and not p.can(tid, action):
                    return _err(404, "nenalezeno")  # neprozrazujeme existenci cizích tenantů
            try:
                return await fn(request, p, tid)
            except NotFound as e:
                return _err(404, str(e))
            except (ServiceError, AuthError) as e:
                return _err(400, str(e))

        return wrapper

    return deco


# --- přihlášení -----------------------------------------------------------------


async def login(request: Request) -> Response:
    st = get_state(request)
    ip = client_ip(request) or "?"
    if _rate_limited(f"ip:{ip}"):
        return _err(429, "příliš mnoho pokusů, zkus to za minutu")
    try:
        data = await _json(request)
        token, info = await run_in_threadpool(
            auth.login, st.db, str(data.get("email", "")), str(data.get("password", "")),
            st.settings.web.session_hours, ip)
    except (AuthError, ServiceError) as e:
        return _err(401, str(e))
    p = await run_in_threadpool(auth.load_principal, st.db, token)
    resp = json_response({"mfa": info["mfa"], "csrf": p.csrf})
    resp.set_cookie(COOKIE, token, max_age=st.settings.web.session_hours * 3600, path="/",
                    secure=True, httponly=True, samesite="strict")
    return resp


@endpoint(None, mfa=False)
async def mfa_setup(request: Request, p: Principal, _t) -> Response:
    st = get_state(request)
    return json_response(await run_in_threadpool(auth.mfa_setup, st.db, st.box, p))


@endpoint(None, mfa=False)
async def mfa_verify(request: Request, p: Principal, _t) -> Response:
    st = get_state(request)
    data = await _json(request)
    try:
        await run_in_threadpool(auth.mfa_verify, st.db, st.box, p, str(data.get("code", "")),
                                client_ip(request))
    except AuthError as e:
        return _err(401, str(e))
    return json_response({"ok": True})


@endpoint(None, mfa=False)
async def logout(request: Request, p: Principal, _t) -> Response:
    await run_in_threadpool(auth.logout, get_state(request).db, p)
    resp = json_response({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@endpoint(None, mfa=False)
async def me(request: Request, p: Principal, _t) -> Response:
    return json_response({"email": p.email, "mfa_ok": p.mfa_ok, "csrf": p.csrf,
                          "superadmin": p.superadmin, "tenants": p.tenant_roles,
                          "mode": "local" if get_state(request).settings.tunnel.local else "remote",
                          "defaults": get_state(request).settings.site_defaults.model_dump()})


@endpoint(None)
async def my_password(request: Request, p: Principal, _t) -> Response:
    from .. import users

    if _rate_limited(f"pw:{p.user_id}"):
        return _err(429, "příliš mnoho pokusů, zkus to za minutu")
    data = await _json(request)
    await run_in_threadpool(users.change_password, get_state(request).db, p, str(data.get("old", "")),
                            str(data.get("new", "")), client_ip(request))
    return json_response({"ok": True})


@endpoint(None)
async def user_mfa_reset(request: Request, p: Principal, _t) -> Response:
    from .. import users

    if not p.superadmin:
        return _err(404, "nenalezeno")
    uid = str(_uuid(request.path_params["user_id"]))
    await run_in_threadpool(users.reset_mfa, get_state(request).db, uid, p, client_ip(request))
    return json_response({"ok": True})


@endpoint(MANAGE)
async def tenant_users(request: Request, p: Principal, tid: str) -> Response:
    from .. import users

    st = get_state(request)
    if request.method == "POST":
        data = await _json(request)
        res = await run_in_threadpool(users.add_user, st.db, tid, str(data.get("email", "")),
                                      str(data.get("role", "")), p, client_ip(request))
        return json_response(res, 201)
    return json_response(await run_in_threadpool(users.list_users, st.db, tid))


@endpoint(MANAGE)
async def tenant_user(request: Request, p: Principal, tid: str) -> Response:
    from .. import users

    st = get_state(request)
    uid = str(_uuid(request.path_params["user_id"]))
    if request.method == "DELETE":
        await run_in_threadpool(users.remove_user, st.db, tid, uid, p, client_ip(request))
    else:
        data = await _json(request)
        await run_in_threadpool(users.set_role, st.db, tid, uid, str(data.get("role", "")), p, client_ip(request))
    return json_response({"ok": True})


_BACKUP_NAME = re.compile(r"^mtc-[A-Za-z0-9-]{1,40}-\d{8}-\d{6}\.mtcbak$")


@endpoint(None)
async def system_backups(request: Request, p: Principal, _t) -> Response:
    """Zálohy controlleru (superadmin): seznam, stav, žádost o zálohu teď."""
    from .. import sysbackup

    if not p.superadmin:
        return _err(404, "nenalezeno")
    st = get_state(request)
    data_dir, bdir = st.settings.paths.data_dir, st.settings.backup.dir
    if request.method == "POST":
        req = sysbackup.request_path(data_dir)
        if req.exists():
            return _err(409, "záloha už čeká na spuštění")

        def q():
            with operator_scope(st.db) as tx:
                audit(tx, "system.backup_request", user_id=p.user_id, target="system:backup", ip=client_ip(request))
            fd = os.open(req, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(fd, "w") as fh:
                json.dump({"user": p.email}, fh)

        await run_in_threadpool(q)
        return json_response({"ok": True})
    return json_response({"backups": await run_in_threadpool(sysbackup.listing, bdir),
                          "status": sysbackup.read_status(data_dir), "pending": sysbackup.request_path(data_dir).exists(),
                          "dir": str(bdir), "keep": st.settings.backup.keep})


@endpoint(None)
async def system_backup_download(request: Request, p: Principal, _t) -> Response:
    from starlette.responses import FileResponse

    if not p.superadmin:
        return _err(404, "nenalezeno")
    st = get_state(request)
    name = request.path_params["name"]
    path = st.settings.backup.dir / name
    if not _BACKUP_NAME.match(name) or not path.is_file():
        return _err(404, "záloha neexistuje")

    def q():
        with operator_scope(st.db) as tx:
            audit(tx, "system.backup_download", user_id=p.user_id, target="system:backup", after={"file": name},
                  ip=client_ip(request))

    await run_in_threadpool(q)
    return FileResponse(path, media_type="application/octet-stream", filename=name,
                        headers={"Cache-Control": "no-store"})


@endpoint(None)
async def system_tls(request: Request, p: Principal, _t) -> Response:
    """Certifikát HTTPS (superadmin): stav slotů; POST = žádost pro root (mtc-tls.path)."""
    from .. import tlscfg

    if not p.superadmin:
        return _err(404, "nenalezeno")
    st = get_state(request)
    s = st.settings
    data_dir, hosts = s.paths.data_dir, tlscfg.hosts_of(s)
    if request.method == "POST":
        if tlscfg.request_path(data_dir).exists():
            return _err(409, "změna certifikátu už probíhá")
        data = await _json(request, limit=3 * tlscfg.MAX_PEM)
        try:
            req = tlscfg.check_request(data, local=s.tunnel.local, hosts=hosts)
        except tlscfg.TlsError as e:
            return _err(400, str(e))

        def q():
            with operator_scope(st.db) as tx:
                audit(tx, "system.tls_request", user_id=p.user_id, target="system:tls",
                      after={"action": req["action"], "slots": req["slots"], "email": req.get("email")},
                      ip=client_ip(request))
            tlscfg.write_request(data_dir, {k: v for k, v in req.items() if k != "warnings"} | {"user": p.email})

        await run_in_threadpool(q)
        return json_response({"ok": True, "warnings": req.get("warnings") or []})
    return json_response({"slots": await run_in_threadpool(tlscfg.status), "local": s.tunnel.local, "hosts": hosts,
                          "status": tlscfg.read_status(data_dir), "pending": tlscfg.request_path(data_dir).exists()})


@endpoint(None)
async def system_status(request: Request, p: Principal, _t) -> Response:
    """Stav controlleru pro superadmina: worker (heartbeat), fronty, alerty, verze."""
    from .. import heartbeat

    if not p.superadmin:
        return _err(404, "nenalezeno")
    st = get_state(request)

    def q():
        with operator_scope(st.db) as tx:
            states = {r["state"]: r["n"] for r in tx.fetchall(
                "SELECT state::text AS state, count(*) AS n FROM device GROUP BY state")}
            cmds = {r["status"]: r["n"] for r in tx.fetchall(
                """SELECT status::text AS status, count(*) AS n FROM device_command
                   WHERE status IN ('queued', 'sent') GROUP BY status""")}
            alerts_ = {r["severity"]: r["n"] for r in tx.fetchall(
                """SELECT severity::text AS severity, count(*) AS n FROM alert
                   WHERE resolved_at IS NULL GROUP BY severity""")}
            misc = tx.fetchone(
                """SELECT (SELECT count(*) FROM tenant) AS tenants,
                          (SELECT count(*) FROM upgrade_job WHERE status = 'running') AS upgrade_jobs,
                          (SELECT count(*) FROM ros_release) AS changelogs,
                          pg_database_size(current_database()) AS db_bytes""")
        return {"worker": heartbeat.read(st.settings.paths.data_dir), "devices": states, "commands": cmds,
                "alerts": alerts_, **misc, "version": _app_version()}

    return json_response(await run_in_threadpool(q))


def _app_version() -> str:
    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    except OSError:
        return "dev"


# --- tenanti a lokality -----------------------------------------------------------


@endpoint(None)
async def tenants(request: Request, p: Principal, _t) -> Response:
    st = get_state(request)
    if request.method == "POST":
        if not p.superadmin:
            return _err(403, "jen superadmin")
        data = await _json(request)
        row = await run_in_threadpool(services.create_tenant, st.db, st.settings,
                                      str(data.get("name", "")), p.user_id, client_ip(request))
        return json_response(row, 201)

    def q():
        with operator_scope(st.db) as tx:
            rows = tx.fetchall(
                """SELECT t.id, t.name, t.tunnel_block::text AS tunnel_block,
                          (SELECT count(*) FROM device d WHERE d.tenant_id = t.id AND d.state <> 'removed') AS devices
                   FROM tenant t ORDER BY t.name""")
        return [r for r in rows if p.superadmin or str(r["id"]) in p.tenant_roles]

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def sites(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    if request.method == "POST":
        if not p.can(tid, MANAGE):
            return _err(403, "nedostatečná oprávnění")
        data = await _json(request)
        row = await run_in_threadpool(services.create_site, st.db, tid, str(data.get("name", "")),
                                      data.get("address"), p.user_id, client_ip(request))
        return json_response(row, 201)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return tx.fetchall(
                """SELECT s.id, s.name, s.address,
                          (SELECT count(*) FROM device d WHERE d.site_id = s.id AND d.state <> 'removed') AS devices
                   FROM site s ORDER BY s.name""")

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def adoption_tokens(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    site_id = _uuid(data["site_id"]) if data.get("site_id") else None
    out = await run_in_threadpool(services.create_adoption_token, st.db, st.settings, tid, site_id,
                                  p.user_id, client_ip(request))
    return json_response({"token": out.token, "expires_at": out.expires_at, "command": out.command}, 201)


# --- zařízení ----------------------------------------------------------------------

_DEVICE_COLS = """d.id, d.serial, d.identity, d.note, d.model, d.managed_at IS NOT NULL AS managed,
                  (SELECT c.params->>'name' FROM device_command c WHERE c.device_id = d.id AND c.kind = 'set_identity'
                    AND c.status IN ('queued', 'sent') ORDER BY c.id DESC LIMIT 1) AS identity_pending, d.ros_version, d.role, d.state,
                  host(d.tunnel_ip) AS tunnel_ip, d.site_id, s.name AS site, d.concentrator,
                  d.last_checkin_at, d.last_status, d.approved_at, d.adopted_at, d.created_at,
                  (SELECT i.data->>'latest-version' FROM device_inventory i
                    WHERE i.device_id = d.id AND i.section = 'update') AS latest_version,
                  (SELECT i.data->>'channel' FROM device_inventory i
                    WHERE i.device_id = d.id AND i.section = 'update') AS update_channel,
                  (SELECT count(*) FROM device_inventory i,
                          jsonb_array_elements(CASE WHEN jsonb_typeof(i.data) = 'array' THEN i.data ELSE '[]' END) f
                    WHERE i.device_id = d.id AND i.section = 'hardening'
                      AND f->>'severity' IN ('critical', 'warning')) AS security_issues,
                  (SELECT i.data FROM device_inventory i WHERE i.device_id = d.id AND i.section = 'resource') AS resource,
                  (SELECT count(*) FROM client c WHERE c.device_id = d.id
                     AND c.last_seen > now() - interval '10 minutes') AS clients,
                  (SELECT count(*) FROM alert a WHERE a.device_id = d.id AND a.resolved_at IS NULL) AS alerts"""


@endpoint(READ)
async def devices(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)

    def q():
        with tenant_scope(st.db, tid) as tx:
            rows = tx.fetchall(
                f"""SELECT {_DEVICE_COLS} FROM device d LEFT JOIN site s ON s.id = d.site_id
                    WHERE d.state <> 'removed' ORDER BY d.state, s.name, d.serial""")
        for r in rows:
            r["warnings"] = [{"severity": i.severity, "text": i.text, "source": i.source}
                             for i in ros_versions.issues_for(r.get("ros_version") or "")]
            lv = r.get("latest_version")
            r["update_available"] = bool(lv) and ros_versions.parse(lv) > ros_versions.parse(r.get("ros_version") or "")
            # z inventury jen pár čísel pro sloupce přehledu (uptime, CPU, paměť, teplota)
            res = r.pop("resource", None) or {}
            res = res if isinstance(res, dict) else {}
            r["uptime"] = res.get("uptime")
            r["uptime_s"] = health.uptime_seconds(res.get("uptime")) if res.get("uptime") else None
            r["cpu"] = int(res["cpu-load"]) if str(res.get("cpu-load", "")).isdigit() else None
            try:
                r["mem_pct"] = round(100 - 100 * int(res["free-memory"]) / int(res["total-memory"]))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                r["mem_pct"] = None
            r["arch"] = res.get("architecture-name")
        return rows

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def approve(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    did = _uuid(request.path_params["device_id"])
    errors = await run_in_threadpool(
        services.approve_device, st.db, st.settings, st.box, st.providers, tid, did,
        _uuid(data.get("site_id")), str(data.get("role", "unknown")), p.user_id, client_ip(request),
        data.get("identity") or None)
    return json_response({"ok": True, "concentrator_errors": errors})


@endpoint(OPERATE)
async def device_identity(request: Request, p: Principal, tid: str) -> Response:
    """Přejmenování zařízení v RouterOS ({"name": "..."})."""
    st = get_state(request)
    data = await _json(request)
    did = _uuid(request.path_params["device_id"])
    out = await run_in_threadpool(services.set_identity, st.db, tid, did, data.get("name"), p.user_id,
                                  client_ip(request))
    if not out.get("unchanged"):
        # AP nejdřív (dostanou staré i nové jméno CAPsMANu), samotné zařízení o chvilku později
        for ap in out.get("aps") or []:
            services.kick(st.db, st.settings, st.box, tid, ap)
        services.kick(st.db, st.settings, st.box, tid, did, delay=8.0 if out.get("aps") else 0.0)
    return json_response(out)


@endpoint(OPERATE)
async def command(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    did = _uuid(request.path_params["device_id"])
    params = {k: str(data[k])[:40] for k in ("version", "port") if data.get(k)} or None
    cid = await run_in_threadpool(services.request_command, st.db, tid, did, str(data.get("kind", "")),
                                  p.user_id, client_ip(request), params)
    services.kick(st.db, st.settings, st.box, tid, did)  # doručit hned, ne až scheduler
    return json_response({"command_id": cid}, 201)


@endpoint(MANAGE)
async def forget(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])
    out = await run_in_threadpool(services.forget_device, st.db, tid, did, p.user_id, client_ip(request))
    return json_response(out)


@endpoint(READ)
async def device_commands(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    did = _uuid(request.path_params["device_id"])

    def q():
        with tenant_scope(st.db, tid) as tx:
            return tx.fetchall(
                """SELECT id, kind, status, attempts, created_at, sent_at, finished_at
                   FROM device_command WHERE device_id = %(d)s ORDER BY id DESC LIMIT 50""", {"d": did})

    return json_response(await run_in_threadpool(q))


@endpoint(READ)
async def audit_log(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)

    def q():
        with tenant_scope(st.db, tid) as tx:
            return tx.fetchall(
                """SELECT a.at, u.email AS user, a.action, a.target, a.after, host(a.source_ip) AS ip
                   FROM audit_log a LEFT JOIN app_user u ON u.id = a.user_id
                   WHERE a.tenant_id = %(t)s ORDER BY a.id DESC LIMIT 200""", {"t": tid})

    return json_response(await run_in_threadpool(q))


# --- statické UI ---------------------------------------------------------------------


async def static(request: Request) -> Response:
    name = request.path_params.get("name", "index.html")
    path = (STATIC / name).resolve()
    if path.parent != STATIC or path.suffix not in _STATIC_TYPES or not path.is_file():
        return _err(404, "nenalezeno")
    return Response(path.read_bytes(), media_type=_STATIC_TYPES[path.suffix])


def routes() -> list[Route]:
    t = "/api/admin/tenants/{tenant_id}"
    return [
        Route("/", static, methods=["GET"]),
        Route("/static/{name}", static, methods=["GET"]),
        Route("/api/admin/login", login, methods=["POST"]),
        Route("/api/admin/logout", logout, methods=["POST"]),
        Route("/api/admin/me", me, methods=["GET"]),
        Route("/api/admin/me/password", my_password, methods=["POST"]),
        Route("/api/admin/system", system_status, methods=["GET"]),
        Route("/api/admin/system/backups", system_backups, methods=["GET", "POST"]),
        Route("/api/admin/system/tls", system_tls, methods=["GET", "POST"]),
        Route("/api/admin/system/backups/{name}", system_backup_download, methods=["GET"]),
        Route("/api/admin/users/{user_id}/reset-mfa", user_mfa_reset, methods=["POST"]),
        Route(t + "/users", tenant_users, methods=["GET", "POST"]),
        Route(t + "/users/{user_id}", tenant_user, methods=["PATCH", "DELETE"]),
        Route("/api/admin/mfa/setup", mfa_setup, methods=["POST"]),
        Route("/api/admin/mfa/verify", mfa_verify, methods=["POST"]),
        Route("/api/admin/tenants", tenants, methods=["GET", "POST"]),
        Route(t + "/sites", sites, methods=["GET", "POST"]),
        Route(t + "/adoption-tokens", adoption_tokens, methods=["POST"]),
        Route(t + "/devices", devices, methods=["GET"]),
        Route(t + "/devices/{device_id}/approve", approve, methods=["POST"]),
        Route(t + "/devices/{device_id}/forget", forget, methods=["POST"]),
        Route(t + "/devices/{device_id}/commands", command, methods=["POST"]),
        Route(t + "/devices/{device_id}/identity", device_identity, methods=["POST"]),
        Route(t + "/devices/{device_id}/commands", device_commands, methods=["GET"]),
        Route(t + "/audit", audit_log, methods=["GET"]),
    ]
