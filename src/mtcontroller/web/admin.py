"""Admin API (`/api/admin/…`) a statické admin UI (`/`).

Každý požadavek kromě přihlášení vyžaduje platnou relaci s dokončeným MFA.
Změnové požadavky (POST) navíc vyžadují hlavičku X-CSRF-Token.
Oprávnění se kontrolují per tenant (Principal.can) a data se čtou v tenant_scope,
takže i chyba v kontrole oprávnění nepustí k datům jiného tenanta (RLS).
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import auth, ros_versions, services
from ..auth import MANAGE, OPERATE, READ, AuthError, Principal
from ..db import operator_scope, tenant_scope
from ..services import NotFound, ServiceError
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


async def _json(request: Request) -> dict:
    raw = await request.body()
    return services.parse_json_body(raw, 65536)


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
                          "superadmin": p.superadmin, "tenants": p.tenant_roles})


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

_DEVICE_COLS = """d.id, d.serial, d.identity, d.model, d.ros_version, d.role, d.state,
                  host(d.tunnel_ip) AS tunnel_ip, d.site_id, s.name AS site, d.concentrator,
                  d.last_checkin_at, d.last_status, d.approved_at, d.adopted_at, d.created_at,
                  (SELECT i.data->>'latest-version' FROM device_inventory i
                    WHERE i.device_id = d.id AND i.section = 'update') AS latest_version,
                  (SELECT i.data->>'channel' FROM device_inventory i
                    WHERE i.device_id = d.id AND i.section = 'update') AS update_channel"""


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
        return rows

    return json_response(await run_in_threadpool(q))


@endpoint(OPERATE)
async def approve(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    did = _uuid(request.path_params["device_id"])
    errors = await run_in_threadpool(
        services.approve_device, st.db, st.settings, st.box, st.providers, tid, did,
        _uuid(data.get("site_id")), str(data.get("role", "unknown")), p.user_id, client_ip(request))
    return json_response({"ok": True, "concentrator_errors": errors})


@endpoint(OPERATE)
async def command(request: Request, p: Principal, tid: str) -> Response:
    st = get_state(request)
    data = await _json(request)
    did = _uuid(request.path_params["device_id"])
    cid = await run_in_threadpool(services.request_command, st.db, tid, did, str(data.get("kind", "")),
                                  p.user_id, client_ip(request))
    return json_response({"command_id": cid}, 201)


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
        Route("/api/admin/mfa/setup", mfa_setup, methods=["POST"]),
        Route("/api/admin/mfa/verify", mfa_verify, methods=["POST"]),
        Route("/api/admin/tenants", tenants, methods=["GET", "POST"]),
        Route(t + "/sites", sites, methods=["GET", "POST"]),
        Route(t + "/adoption-tokens", adoption_tokens, methods=["POST"]),
        Route(t + "/devices", devices, methods=["GET"]),
        Route(t + "/devices/{device_id}/approve", approve, methods=["POST"]),
        Route(t + "/devices/{device_id}/commands", command, methods=["POST"]),
        Route(t + "/devices/{device_id}/commands", device_commands, methods=["GET"]),
        Route(t + "/audit", audit_log, methods=["GET"]),
    ]
