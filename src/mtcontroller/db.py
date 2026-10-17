"""Připojení k DB a tenant kontext pro row-level security.

Každá transakce, která sahá na data tenanta, musí běžet v `tenant_scope`.
Ten nastaví `app.tenant_id` jen pro danou transakci (SET LOCAL), takže se
kontext nemůže „přelít“ do jiného požadavku přes connection pool.
Bez nastaveného kontextu RLS nevrátí žádné řádky.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(get_settings().database.dsn, pool_pre_ping=True)
        _session_factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def tenant_scope(tenant_id: uuid.UUID) -> Iterator[Session]:
    """Transakce omezená na jednoho tenanta."""
    with _factory().begin() as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenant_id)},
        )
        yield session


@contextmanager
def operator_scope() -> Iterator[Session]:
    """Transakce provozovatele (superadmin) – vidí všechny tenanty.

    Používat jen pro operace, které tenanta z podstaty přesahují
    (správa tenantů, koncentrátor, globální přehled). Volající musí mít
    ověřenou roli superadmin.
    """
    with _factory().begin() as session:
        session.execute(text("SELECT set_config('app.operator', 'on', true)"))
        yield session
