"""Načítání konfigurace z config.toml s přepisem z prostředí.

Pořadí: výchozí hodnoty < config.toml < proměnné MTC_<SEKCE>__<KLÍČ>.
Cesta ke config.toml: $MTC_CONFIG, jinak /etc/mtcontroller/config.toml.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path("/etc/mtcontroller/config.toml")
ENV_PREFIX = "MTC_"


class DatabaseConfig(BaseModel):
    dsn: str = "postgresql+psycopg://mtc_app@/mtcontroller?host=/run/postgresql"
    migrate_dsn: str = "postgresql+psycopg://mtc_owner@/mtcontroller?host=/run/postgresql"


class WebConfig(BaseModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    ui_host: str = "localhost"
    checkin_host: str = "localhost"


class SecurityConfig(BaseModel):
    master_key_file: Path = Path("/etc/mtcontroller/master.key")
    adoption_token_ttl_minutes: int = Field(default=60, ge=5, le=24 * 60)


class PathsConfig(BaseModel):
    data_dir: Path = Path("/var/lib/mtcontroller")


class TunnelConfig(BaseModel):
    provider: str = "mikrotik"
    pool: str = "10.200.0.0/16"
    tenant_block_prefix: int = 24
    concentrator_hosts: list[str] = Field(default_factory=list)
    concentrator_api: str = ""

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


class Settings(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)


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
