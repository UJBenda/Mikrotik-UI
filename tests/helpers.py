"""Společné pomůcky testů: testovací DB, ASGI klient, falešný koncentrátor.

Testy s DB potřebují lokální PostgreSQL 16+ s trust autentizací na unix
socketu a role mtc_owner / mtc_app (vytvoří je tests/db/run.sh). Když DB
není k dispozici, testy se přeskočí.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from mtcontroller import migrate
from mtcontroller.config import Settings
from mtcontroller.security import SecretBox
from mtcontroller.tunnel import TunnelAccount, TunnelProvider

SOCKET = Path("/var/run/postgresql/.s.PGSQL.5432")


def fresh_db():
    """Vytvoří čistou DB s aplikovanými migracemi, vrátí (app_db, name)."""
    if not SOCKET.exists():
        pytest.skip("PostgreSQL není k dispozici")
    from pgwire import PgConnection, PgWireDatabase

    name = "mtc_t_" + secrets.token_hex(4)
    try:
        su = PgConnection("postgres", "postgres")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL nedostupný: {e}")
    for role, extra in (("mtc_owner", ""), ("mtc_app", " NOBYPASSRLS")):
        if not su.query(f"SELECT 1 FROM pg_roles WHERE rolname = '{role}'")[0]:
            su.query(f"CREATE ROLE {role} LOGIN{extra}")
    su.query(f"CREATE DATABASE {name} OWNER mtc_owner")
    su.close()
    su = PgConnection("postgres", name)
    su.query("REVOKE ALL ON SCHEMA public FROM PUBLIC; ALTER SCHEMA public OWNER TO mtc_owner")
    su.close()
    if os.environ.get("MTC_TEST_PSYCOPG"):
        # produkční cesta: skutečný psycopg (serverové parametry, typy)
        from mtcontroller.db import PsycopgDatabase

        migrate.upgrade(PsycopgDatabase(f"dbname={name} user=mtc_owner host=/var/run/postgresql"))
        return PsycopgDatabase(f"dbname={name} user=mtc_app host=/var/run/postgresql"), name
    migrate.upgrade(PgWireDatabase("mtc_owner", name))
    return PgWireDatabase("mtc_app", name), name


def drop_db(name: str) -> None:
    from pgwire import PgConnection

    su = PgConnection("postgres", "postgres")
    su.query(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    su.close()


def make_settings(tmp: Path, concentrators: int = 1) -> Settings:
    concs = []
    for i in range(1, concentrators + 1):
        pw = tmp / f"vpn{i}.password"
        pw.write_text("concentrator-pw")
        concs.append({"name": f"vpn{i}", "connect_host": f"vpn{i}.test", "api_url": f"https://10.10.0.{i}:8443",
                      "api_password_file": str(pw), "api_fingerprint": "ab" * 32})
    return Settings.model_validate({
        "web": {"ui_host": "admin.test", "checkin_host": "checkin.test"},
        "tunnel": {"controller_ip": "10.10.0.10", "concentrators": concs},
    })


class FakeProvider(TunnelProvider):
    def __init__(self, name: str = "vpn1", fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.accounts: dict[str, TunnelAccount] = {}
        self.disabled: set[str] = set()

    def ensure_account(self, account: TunnelAccount) -> None:
        if self.fail:
            raise ConnectionError("koncentrátor nedostupný")
        self.accounts[account.username] = account

    def disable_account(self, username: str) -> None:
        self.disabled.add(username)

    def active_sessions(self) -> dict[str, str]:
        return {u: a.remote_address for u, a in self.accounts.items()}


def new_box() -> SecretBox:
    return SecretBox(os.urandom(32))


class Response:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        self.status_code = status
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}
        self.raw_headers = headers
        self.content = body
        self.text = body.decode(errors="replace")

    def json(self):
        return json.loads(self.content)


class AsgiClient:
    """Minimální ASGI klient (bez httpx), drží cookies a CSRF token."""

    def __init__(self, app, host: str, client_ip: str = "198.51.100.7") -> None:
        self.app, self.host, self.client_ip = app, host, client_ip
        self.cookies: dict[str, str] = {}
        self.csrf: str | None = None

    def request(self, method: str, path: str, *, json_body=None, data: bytes | None = None,
                headers: dict | None = None) -> Response:
        path, _, query = path.partition("?")
        body = json.dumps(json_body).encode() if json_body is not None else (data or b"")
        hdrs = [(b"host", self.host.encode())]
        if json_body is not None:
            hdrs.append((b"content-type", b"application/json"))
        if self.cookies:
            hdrs.append((b"cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()).encode()))
        if self.csrf and method != "GET":
            hdrs.append((b"x-csrf-token", self.csrf.encode()))
        for k, v in (headers or {}).items():
            hdrs.append((k.lower().encode(), v.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                 "root_path": "", "headers": hdrs, "client": (self.client_ip, 40000),
                 "server": (self.host, 443)}
        out = {"status": None, "headers": [], "body": b""}
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"], out["headers"] = msg["status"], msg.get("headers", [])
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        resp = Response(out["status"], out["headers"], out["body"])
        for k, v in resp.raw_headers:
            if k.lower() == b"set-cookie":
                c = SimpleCookie(v.decode())
                for name, morsel in c.items():
                    if morsel.value == "" or morsel["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = morsel.value
        return resp

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)


def uid() -> str:
    return str(uuid.uuid4())
