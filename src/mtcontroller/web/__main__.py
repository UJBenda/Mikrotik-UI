"""Spuštění webové služby: `python3 -m mtcontroller.web` (systemd: mtc-web.service)."""

from __future__ import annotations

import logging

import uvicorn

from ..config import get_settings
from ..db import get_db
from ..security import get_box
from ..tunnel import build_providers
from .app import AppState, create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = get_settings()
    state = AppState(settings=s, db=get_db(), box=get_box(), providers=build_providers(s))
    uvicorn.run(
        create_app(state),
        host=s.web.listen_host,
        port=s.web.listen_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
