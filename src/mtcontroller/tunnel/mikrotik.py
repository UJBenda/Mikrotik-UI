"""TunnelProvider nad REST API fyzického MikroTiku (RouterOS v7)."""

from __future__ import annotations

from ..rosapi import RosApi
from . import TunnelAccount, TunnelProvider

PPP_PROFILE = "mtc-tunnel"  # profil s interface-list=TUNNELS, only-one=yes (viz concentrator.rsc)
COMMENT_PREFIX = "mtc: "


class MikrotikProvider(TunnelProvider):
    def __init__(self, name: str, api: RosApi) -> None:
        self.name = name
        self.api = api

    @classmethod
    def from_config(cls, c) -> "MikrotikProvider":
        return cls(c.name, RosApi(c.api_url, c.api_user, c.password(), c.api_fingerprint))

    def _find(self, username: str) -> dict | None:
        rows = self.api.get("/ppp/secret", name=username)
        return rows[0] if rows else None

    def ensure_account(self, account: TunnelAccount) -> None:
        body = {
            "name": account.username,
            "password": account.password,
            "service": "sstp",
            "profile": PPP_PROFILE,
            "remote-address": account.remote_address,
            "comment": COMMENT_PREFIX + account.comment,
            "disabled": "no",
        }
        existing = self._find(account.username)
        if existing:
            self.api.patch(f"/ppp/secret/{existing['.id']}", body)
        else:
            self.api.put("/ppp/secret", body)

    def disable_account(self, username: str) -> None:
        existing = self._find(username)
        if existing:
            self.api.patch(f"/ppp/secret/{existing['.id']}", {"disabled": "yes"})
        for row in self.api.get("/ppp/active", name=username) or []:
            self.api.delete(f"/ppp/active/{row['.id']}")

    def active_sessions(self) -> dict[str, str]:
        rows = self.api.get("/ppp/active", service="sstp") or []
        return {r["name"]: r.get("address", "") for r in rows}
