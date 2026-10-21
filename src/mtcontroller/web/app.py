"""ASGI aplikace (Starlette).

Jedna aplikace obsluhuje dva hostname a routy jsou podle nich striktně oddělené:
- admin UI a API (`/`, `/static/…`, `/api/admin/…`) jen na web.ui_host,
- check-in, adopce a ohlášení (`/api/checkin…`, `/adopt/…`, `/enroll/…`) jen na web.checkin_host.
Cokoli jiného je 404. nginx navíc pouští admin hostname jen z admin sítě.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import __version__
from ..config import Settings
from ..db import Database
from ..security import SecretBox
from ..tunnel import TunnelProvider

log = logging.getLogger("mtc.web")

CHECKIN_PREFIXES = ("/api/checkin", "/adopt/", "/enroll/")
SHARED_PATHS = ("/healthz",)


@dataclass
class AppState:
    settings: Settings
    db: Database
    box: SecretBox
    providers: list[TunnelProvider] = field(default_factory=list)


def json_response(data: Any, status: int = 200, headers: dict | None = None) -> Response:
    return Response(json.dumps(data, default=str, ensure_ascii=False), status_code=status,
                    media_type="application/json", headers=headers)


def script_response(script: str, status: int = 200) -> Response:
    return Response(script, status_code=status, media_type="text/plain; charset=us-ascii")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def create_app(state: AppState) -> Starlette:
    from . import admin, checkin, devices

    from .. import regional

    settings = state.settings
    regional.apply(settings)
    ui_host, checkin_host = settings.web.ui_host, settings.web.checkin_host

    async def host_separation(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        path = request.url.path
        is_checkin = path.startswith(CHECKIN_PREFIXES)
        if path not in SHARED_PATHS:
            if is_checkin and host != checkin_host:
                return json_response({"detail": "not found"}, 404)
            if not is_checkin and host != ui_host:
                return json_response({"detail": "not found"}, 404)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # výjimka jen pro obrázky (loga klientů): soukromá cache a vlastní přísnější CSP
        if "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not is_checkin and "content-security-policy" not in response.headers:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        elif not is_checkin:
            response.headers["X-Frame-Options"] = "DENY"
        return response

    async def healthz(request: Request) -> Response:
        return json_response({"status": "ok", "version": __version__})

    routes = [Route("/healthz", healthz)] + checkin.routes() + admin.routes() + devices.routes()
    app = Starlette(routes=routes, middleware=[Middleware(BaseHTTPMiddleware, dispatch=host_separation)])
    app.state.mtc = state
    return app


def get_state(request: Request) -> AppState:
    return request.app.state.mtc
