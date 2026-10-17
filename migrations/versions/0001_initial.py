"""Základní schéma F0.

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _app_role() -> str:
    role = os.environ.get("MTC_APP_ROLE", "mtc_app")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"neplatné jméno role: {role!r}")
    return role


def _run(name: str) -> None:
    sql = (SQL_DIR / name).read_text().replace("{app_role}", _app_role())
    op.execute(sql)


def upgrade() -> None:
    _run("0001_initial.up.sql")


def downgrade() -> None:
    _run("0001_initial.down.sql")
