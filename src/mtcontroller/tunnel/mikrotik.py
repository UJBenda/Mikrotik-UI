"""TunnelProvider nad REST API fyzického MikroTiku (RouterOS v7)."""

from __future__ import annotations

import httpx

from . import TunnelAccount, TunnelProvider

PPP_PROFILE = "mtc-tunnel"  # profil s interface-list=TUNNELS, only-one=yes


class MikrotikProvider(TunnelProvider):
    def __init__(self, base_url: str, username: str, password: str,
                 ca_file: str | None = None) -> None:
        # Ověření certifikátu koncentrátoru je povinné; vlastní CA přes ca_file.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/rest",
            auth=(username, password),
            verify=ca_file or True,
            timeout=15.0,
        )

    async def _find(self, username: str) -> dict | None:
        r = await self._client.get("/ppp/secret", params={"name": username})
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def ensure_account(self, account: TunnelAccount) -> None:
        body = {
            "name": account.username,
            "password": account.password,
            "service": "sstp",
            "profile": PPP_PROFILE,
            "remote-address": account.remote_address,
            "comment": account.comment,
            "disabled": "false",
        }
        existing = await self._find(account.username)
        if existing:
            r = await self._client.patch(f"/ppp/secret/{existing['.id']}", json=body)
        else:
            r = await self._client.put("/ppp/secret", json=body)
        r.raise_for_status()

    async def disable_account(self, username: str) -> None:
        existing = await self._find(username)
        if existing:
            r = await self._client.patch(f"/ppp/secret/{existing['.id']}",
                                         json={"disabled": "true"})
            r.raise_for_status()
        r = await self._client.get("/ppp/active", params={"name": username})
        r.raise_for_status()
        for row in r.json():
            await self._client.delete(f"/ppp/active/{row['.id']}")

    async def active_sessions(self) -> dict[str, str]:
        r = await self._client.get("/ppp/active", params={"service": "sstp"})
        r.raise_for_status()
        return {row["name"]: row.get("address", "") for row in r.json()}

    async def aclose(self) -> None:
        await self._client.aclose()
