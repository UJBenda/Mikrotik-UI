"""Načítání konfigurace z config.toml s přepisem z prostředí.

Pořadí: výchozí hodnoty < config.toml < proměnné MTC_<SEKCE>__<KLÍČ>.
Cesta ke config.toml: $MTC_CONFIG, jinak /etc/mtcontroller/config.toml.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("/etc/mtcontroller/config.toml")
ENV_PREFIX = "MTC_"
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def _hostname(v: str) -> str:
    v = v.strip().lower()
    if not _HOST.match(v):
        raise ValueError(f"neplatný hostname: {v!r}")
    return v


class DatabaseConfig(BaseModel):
    # libpq conninfo; přes unix socket a peer autentizaci (bez hesla po síti)
    dsn: str = "dbname=mtcontroller user=mtc_app host=/run/postgresql"
    migrate_dsn: str = "dbname=mtcontroller user=mtc_owner host=/run/postgresql"
    app_role: str = "mtc_app"


class WebConfig(BaseModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    ui_host: str = "localhost"
    checkin_host: str = "localhost"
    session_hours: int = Field(default=8, ge=1, le=24)

    @field_validator("ui_host", "checkin_host")
    @classmethod
    def check_hosts(cls, v: str) -> str:
        return _hostname(v)


class SecurityConfig(BaseModel):
    master_key_file: Path = Path("/etc/mtcontroller/master.key")
    adoption_token_ttl_minutes: int = Field(default=60, ge=5, le=24 * 60)
    # RouterOS fetch: "yes-without-crl" (doporučeno), "yes", nebo "no" jen pro laboratoř
    device_check_certificate: str = "yes-without-crl"
    # moduly, jejichž plány musí schválit jiný uživatel než autor
    four_eyes_modules: list[str] = Field(default_factory=lambda: ["vlan", "firewall"])

    @field_validator("device_check_certificate")
    @classmethod
    def _cc(cls, v: str) -> str:
        if v not in {"yes", "yes-without-crl", "no"}:
            raise ValueError("device_check_certificate: yes | yes-without-crl | no")
        return v


class PathsConfig(BaseModel):
    data_dir: Path = Path("/var/lib/mtcontroller")


class ConcentratorConfig(BaseModel):
    name: str
    # hostname, na který se připojují SSTP klienti (musí sedět s certifikátem SSTP serveru)
    connect_host: str
    # REST API koncentrátoru z pohledu controlleru, např. https://10.10.0.1:8443 (443 patří SSTP)
    api_url: str
    api_user: str = "mtc-controller"
    api_password_file: Path
    # SHA-256 otisk certifikátu www-ssl koncentrátoru (pinning), hex bez dvojteček
    api_fingerprint: str

    @field_validator("connect_host")
    @classmethod
    def check_connect_host(cls, v: str) -> str:
        return _hostname(v)

    @field_validator("api_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("api_url musí být https://")
        return v.rstrip("/")

    @field_validator("api_fingerprint")
    @classmethod
    def _fp(cls, v: str) -> str:
        v = v.replace(":", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("api_fingerprint musí být SHA-256 (64 hex znaků)")
        return v

    def password(self) -> str:
        return self.api_password_file.read_text().strip()


class TunnelConfig(BaseModel):
    provider: str = "mikrotik"
    pool: str = "10.200.0.0/16"
    tenant_block_prefix: int = 24
    # adresa controlleru, ze které chodí správa do tunelů (zařízení ji vidí jako zdroj)
    controller_ip: str = "10.10.0.10"
    concentrators: list[ConcentratorConfig] = Field(default_factory=list)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v not in {"mikrotik", "radius"}:
            raise ValueError("tunnel.provider musí být 'mikrotik' nebo 'radius'")
        return v

    @field_validator("pool")
    @classmethod
    def _pool(cls, v: str) -> str:
        net = ipaddress.ip_network(v, strict=True)
        if not net.is_private:
            raise ValueError("tunnel.pool musí být privátní rozsah")
        return str(net)

    @field_validator("controller_ip")
    @classmethod
    def _cip(cls, v: str) -> str:
        return str(ipaddress.ip_address(v))

    @model_validator(mode="after")
    def _controller_outside_pool(self) -> "TunnelConfig":
        if ipaddress.ip_address(self.controller_ip) in ipaddress.ip_network(self.pool):
            raise ValueError("controller_ip nesmí ležet v tunelovém poolu")
        names = [c.name for c in self.concentrators]
        if len(names) != len(set(names)):
            raise ValueError("jména koncentrátorů musí být unikátní")
        return self


class NotifyTarget(BaseModel):
    name: str
    # "nextcloud_talk" (bot Talk) nebo "json" (obecný webhook s HMAC podpisem)
    kind: str = "json"
    url: str
    secret_file: Path
    min_severity: str = "warning"
    # prázdné = všichni tenanti; jinak seznam UUID tenantů
    tenants: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in {"nextcloud_talk", "json"}:
            raise ValueError("notify.kind: nextcloud_talk | json")
        return v

    @field_validator("min_severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        if v not in {"info", "warning", "critical"}:
            raise ValueError("notify.min_severity: info | warning | critical")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("notify.url musí být https://")
        return v

    def secret(self) -> str:
        return self.secret_file.read_text().strip()


class Settings(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    notify: list[NotifyTarget] = Field(default_factory=list)


def _env_overrides(environ: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX) or "__" not in key or key == "MTC_CONFIG":
            continue
        section, _, field = key[len(ENV_PREFIX):].partition("__")
        value: Any = raw
        if raw[:1] in "[{":
            value = json.loads(raw)
        out.setdefault(section.lower(), {})[field.lower()] = value
    return out


def load_settings(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> Settings:
    environ = os.environ if environ is None else environ
    path = path or Path(environ.get("MTC_CONFIG", DEFAULT_CONFIG_PATH))
    data: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    for section, values in _env_overrides(environ).items():
        data.setdefault(section, {}).update(values)
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
