"""FastAPI aplikace.

Jedna aplikace obsluhuje dva hostname, ale routery jsou striktně oddělené:
- admin UI/API (`/api/admin/...`) jen pro Host == web.ui_host,
- check-in a adopce (`/api/checkin`, `/adopt/...`) jen pro Host == web.checkin_host.
nginx navíc pouští admin hostname jen z admin sítě.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Settings, get_settings

CHECKIN_PATHS = ("/api/checkin", "/adopt/")
ADMIN_PATHS = ("/api/admin",)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="MikroTik Controller",
        version=__version__,
        docs_url=None,  # žádná veřejná dokumentace API
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def host_separation(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        path = request.url.path
        if path.startswith(ADMIN_PATHS) and host != settings.web.ui_host:
            return JSONResponse({"detail": "not found"}, status_code=404)
        if path.startswith(CHECKIN_PATHS) and host != settings.web.checkin_host:
            return JSONResponse({"detail": "not found"}, status_code=404)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
