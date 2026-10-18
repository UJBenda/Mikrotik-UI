"""Endpointy pro zařízení: adopce a check-in. Odpovědi jsou RouterOS skripty.

Autentizace zařízení: `Authorization: Bearer <check-in klíč>` přes TLS
s ověřeným certifikátem serveru (RouterOS fetch check-certificate).
Pozn.: HMAC podpis z návrhu RouterOS skriptování neumí spočítat, proto
bearer klíč (256 bitů, v DB jen hash).
"""

from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import routeros, services
from ..services import NotFound, ServiceError
from .app import client_ip, get_state, script_response

log = logging.getLogger("mtc.checkin")
MAX_BODY = 16384


def _opts(state) -> routeros.FetchOpts:
    return routeros.FetchOpts(state.settings.security.device_check_certificate)


def _base(state) -> str:
    return f"https://{state.settings.web.checkin_host}"


async def _body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ServiceError("tělo požadavku je příliš velké")
    return body


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


async def adopt_stage1(request: Request) -> Response:
    st = get_state(request)
    token = request.path_params["token"]
    if len(token) > 100 or not await run_in_threadpool(services.adoption_token_valid, st.db, token):
        return script_response(routeros.error_script("adopcni token je neplatny nebo prosly"), 404)
    url = f"{_base(st)}/adopt/{token}/register"
    return script_response(routeros.adopt_stage1(url, _opts(st)))


async def adopt_register(request: Request) -> Response:
    st = get_state(request)
    token = request.path_params["token"]
    ip = client_ip(request)
    try:
        payload = services.parse_json_body(await _body(request), MAX_BODY)
        reg = await run_in_threadpool(services.register_device, st.db, st.box, token, payload, ip)
    except NotFound as e:
        log.warning("adopce odmítnuta z %s: %s", ip, e)
        return script_response(routeros.error_script("adopcni token je neplatny nebo prosly"), 404)
    except ServiceError as e:
        log.warning("adopce odmítnuta z %s: %s", ip, e)
        return script_response(routeros.error_script(_ascii(str(e))), 400)
    script = routeros.adopt_stage2(f"{_base(st)}/api/checkin", reg.checkin_key, _opts(st))
    return script_response(script)


async def checkin(request: Request) -> Response:
    st = get_state(request)
    dev = await run_in_threadpool(services.lookup_device_by_key, st.db, _bearer(request))
    if not dev:
        return script_response(routeros.error_script("neplatny check-in klic"), 401)
    try:
        body = services.parse_json_body(await _body(request), MAX_BODY)
        script = await run_in_threadpool(services.checkin, st.db, st.settings, st.box, dev, body,
                                         client_ip(request))
    except ServiceError as e:
        return script_response(routeros.error_script(_ascii(str(e))), 400)
    return script_response(script)


async def ack(request: Request) -> Response:
    st = get_state(request)
    dev = await run_in_threadpool(services.lookup_device_by_key, st.db, _bearer(request))
    if not dev:
        return script_response("unauthorized\n", 401)
    try:
        cid = int(request.query_params.get("id", ""))
        status = request.query_params.get("status", "")
        ok = await run_in_threadpool(services.ack, st.db, dev, cid, status, client_ip(request))
    except (ValueError, ServiceError):
        return script_response("bad request\n", 400)
    if ok and status == "done":
        # další příkaz nebo potvrzení změny doručit hned (zařízení mezitím dokončí skript)
        services.kick(st.db, st.settings, st.box, dev["tenant_id"], dev["id"], delay=3.0)
    return script_response("ok\n" if ok else "ignored\n")


async def bootstrap(request: Request) -> Response:
    """Bootstrap převzetí do řízení – stahuje ho zařízení během schváleného příkazu."""
    from .. import takeover

    st = get_state(request)
    dev = await run_in_threadpool(services.lookup_device_by_key, st.db, _bearer(request))
    if not dev:
        return script_response(routeros.error_script("neplatny check-in klic"), 401)
    try:
        cid = int(request.query_params.get("id", ""))
        script = await run_in_threadpool(takeover.bootstrap_script, st.db, st.settings, st.box, dev, cid,
                                         client_ip(request))
    except (ValueError, ServiceError):
        return script_response(routeros.error_script("zadne probihajici prevzeti"), 404)
    return script_response(script)


def _ascii(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()[:200]


def routes() -> list[Route]:
    return [
        Route("/adopt/{token}", adopt_stage1, methods=["GET"]),
        Route("/adopt/{token}/register", adopt_register, methods=["POST"]),
        Route("/api/checkin", checkin, methods=["POST"]),
        Route("/api/checkin/ack", ack, methods=["POST"]),
        Route("/api/checkin/bootstrap", bootstrap, methods=["POST"]),
    ]
