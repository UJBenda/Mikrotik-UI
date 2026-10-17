"""Jednoduchý migrátor: aplikuje src/mtcontroller/sql/NNNN_*.up.sql v pořadí.

Běží rolí vlastníka schématu (database.migrate_dsn), každá migrace ve vlastní
transakci. `{app_role}` v SQL se nahradí jménem aplikační role.
"""

from __future__ import annotations

import re
from pathlib import Path

from .db import Database

SQL_DIR = Path(__file__).resolve().parent / "sql"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.up\.sql$")
_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def available() -> list[tuple[int, Path]]:
    out = []
    for p in sorted(SQL_DIR.iterdir()):
        m = _NAME.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    versions = [v for v, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrace musí být číslované souvisle od 0001: {versions}")
    return out


def applied(db: Database) -> set[int]:
    with db.transaction() as tx:
        tx.execute("""CREATE TABLE IF NOT EXISTS schema_migration (
                        version integer PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT now())""")
        return {r["version"] for r in tx.fetchall("SELECT version FROM schema_migration")}


def upgrade(db: Database, app_role: str = "mtc_app") -> list[int]:
    if not _ROLE.match(app_role):
        raise ValueError(f"neplatné jméno role: {app_role!r}")
    done = applied(db)
    ran = []
    for version, path in available():
        if version in done:
            continue
        sql = path.read_text().replace("{app_role}", app_role)
        with db.transaction() as tx:
            tx.execute(sql)
            tx.execute("INSERT INTO schema_migration (version) VALUES (%(v)s)", {"v": version})
        ran.append(version)
    return ran
