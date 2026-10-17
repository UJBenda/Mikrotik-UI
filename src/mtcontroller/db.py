"""Přístup k PostgreSQL a tenant kontext pro row-level security.

Rozhraní je záměrně malé (transaction → fetchall/fetchone/execute), aby šlo
v testech nahradit ovladačem, který mluví přímo s testovacím Postgresem.

Parametry v SQL: `%(jmeno)s`. Hodnoty typu dict/list se posílají jako jsonb.
inet/cidr sloupce vybírej jako text (`host(x)`, `x::text`), ať všude dostaneš str.

Každá transakce, která sahá na data tenanta, musí běžet v `tenant_scope`.
Kontext se nastavuje přes SET LOCAL, takže se přes pool nepřelije
do jiného požadavku. Bez kontextu RLS nevrátí nic.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Protocol

Params = Mapping[str, Any] | None
Row = dict[str, Any]


class DatabaseError(Exception):
    def __init__(self, sqlstate: str | None, message: str) -> None:
        super().__init__(f"[{sqlstate}] {message}")
        self.sqlstate = sqlstate
        self.message = message

    @property
    def is_unique_violation(self) -> bool:
        return self.sqlstate == "23505"


class Tx(Protocol):
    def fetchall(self, sql: str, params: Params = None) -> list[Row]: ...
    def fetchone(self, sql: str, params: Params = None) -> Row | None: ...
    def execute(self, sql: str, params: Params = None) -> int: ...


class Database(Protocol):
    def transaction(self) -> "contextmanager[Tx]": ...


# --- psycopg ---------------------------------------------------------------


class PsycopgDatabase:
    def __init__(self, conninfo: str) -> None:
        import psycopg  # noqa: F401  (chyba importu má přijít hned při startu)

        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        import psycopg
        from psycopg.rows import dict_row

        try:
            conn = psycopg.connect(self._conninfo, row_factory=dict_row)
        except psycopg.Error as e:
            raise DatabaseError(getattr(e, "sqlstate", None), str(e)) from e
        try:
            with conn:  # commit při úspěchu, rollback při výjimce
                yield _PsycopgTx(conn)
        except psycopg.Error as e:
            raise DatabaseError(e.sqlstate, str(e)) from e
        finally:
            conn.close()


class _PsycopgTx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @staticmethod
    def _adapt(params: Params) -> Params:
        if not params:
            return params
        from psycopg.types.json import Jsonb

        return {k: Jsonb(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}

    def fetchall(self, sql: str, params: Params = None) -> list[Row]:
        with self._conn.cursor() as cur:
            cur.execute(sql, self._adapt(params))
            return list(cur.fetchall())

    def fetchone(self, sql: str, params: Params = None) -> Row | None:
        with self._conn.cursor() as cur:
            cur.execute(sql, self._adapt(params))
            return cur.fetchone()

    def execute(self, sql: str, params: Params = None) -> int:
        with self._conn.cursor() as cur:
            cur.execute(sql, self._adapt(params))
            return cur.rowcount


# --- scope ------------------------------------------------------------------


@contextmanager
def tenant_scope(db: Database, tenant_id: uuid.UUID | str) -> Iterator[Tx]:
    """Transakce omezená na jednoho tenanta."""
    with db.transaction() as tx:
        tx.execute("SELECT set_config('app.tenant_id', %(t)s, true)", {"t": str(tenant_id)})
        yield tx


@contextmanager
def operator_scope(db: Database) -> Iterator[Tx]:
    """Transakce přes všechny tenanty – jen pro provozovatele a interní služby
    (worker, vyhledání zařízení podle check-in klíče). Volající musí mít
    oprávnění ověřené dřív."""
    with db.transaction() as tx:
        tx.execute("SELECT set_config('app.operator', 'on', true)")
        yield tx


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        from .config import get_settings

        _db = PsycopgDatabase(get_settings().database.dsn)
    return _db


def set_db(db: Database | None) -> None:
    """Pro testy a CLI (migrace běží jinou rolí)."""
    global _db
    _db = db
