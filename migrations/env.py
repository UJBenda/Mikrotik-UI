"""Alembic prostředí. Migrace běží rolí vlastníka schématu (database.migrate_dsn)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from mtcontroller.config import get_settings
from mtcontroller.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database.migrate_dsn)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline režim není podporován")
run_migrations_online()
