"""Spuštění webové služby: `mtc-web` (systemd: mtc-web.service)."""

from __future__ import annotations

import uvicorn

from ..config import get_settings
from .app import create_app


def main() -> None:
    s = get_settings()
    uvicorn.run(
        create_app(s),
        host=s.web.listen_host,
        port=s.web.listen_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
    )


if __name__ == "__main__":
    main()
