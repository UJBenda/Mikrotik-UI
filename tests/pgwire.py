"""Minimální klient PostgreSQL (simple query protokol) pro testy.

Slouží k tomu, aby testy šly spustit proti skutečnému Postgresu i bez psycopg.
Umí jen to, co potřebují testy: trust autentizaci přes unix socket, simple
query, převod základních typů. Parametry `%(x)s` se dosazují jako literály.
NIKDY nepoužívat v produkci – produkce jede přes psycopg (db.PsycopgDatabase).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import socket
import struct
import uuid
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

from mtcontroller.db import DatabaseError

_PARAM = re.compile(r"%\((\w+)\)s|%%")


def literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, bytes):
        return f"'\\x{v.hex()}'::bytea"
    if isinstance(v, (dict, list)):
        return _quote(json.dumps(v)) + "::jsonb"
    if isinstance(v, (dt.datetime, dt.date)):
        return _quote(v.isoformat())
    if isinstance(v, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address,
                      ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return _quote(str(v))
    if isinstance(v, str):
        return _quote(v)
    raise TypeError(f"nepodporovaný typ parametru: {type(v)}")


def _quote(s: str) -> str:
    if "\x00" in s:
        raise ValueError("NUL v řetězci")
    return "'" + s.replace("'", "''") + "'"


def interpolate(sql: str, params: dict | None) -> str:
    if params is None:
        return sql
    return _PARAM.sub(lambda m: "%" if m.group(0) == "%%" else literal(params[m.group(1)]), sql)


def _tz(s: str) -> str:
    return s + ":00" if re.search(r"[+-]\d\d$", s) else s


_CONVERT = {
    16: lambda s: s == "t",
    20: int, 21: int, 23: int, 26: int,
    700: float, 701: float, 1700: Decimal,
    114: json.loads, 3802: json.loads,
    2950: uuid.UUID,
    1184: lambda s: dt.datetime.fromisoformat(_tz(s)),
    1114: dt.datetime.fromisoformat,
    1082: dt.date.fromisoformat,
    17: lambda s: bytes.fromhex(s[2:]),
}


class PgConnection:
    def __init__(self, user: str, database: str, socket_dir: str = "/var/run/postgresql",
                 port: int = 5432) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(f"{socket_dir}/.s.PGSQL.{port}")
        body = struct.pack("!I", 196608)
        for k, v in (("user", user), ("database", database), ("TimeZone", "UTC"),
                     ("DateStyle", "ISO"), ("client_encoding", "UTF8")):
            body += k.encode() + b"\0" + v.encode() + b"\0"
        body += b"\0"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        self._until_ready()

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("spojení s Postgresem uzavřeno")
            buf += chunk
        return buf

    def _msg(self) -> tuple[bytes, bytes]:
        t = self._recv(1)
        (ln,) = struct.unpack("!I", self._recv(4))
        return t, self._recv(ln - 4)

    def _until_ready(self) -> None:
        err = None
        while True:
            t, data = self._msg()
            if t == b"R" and struct.unpack("!I", data[:4])[0] != 0:
                raise RuntimeError("testovací Postgres musí mít pro unix socket trust autentizaci")
            if t == b"E":
                err = _error(data)
            if t == b"Z":
                if err:
                    raise err
                return

    def query(self, sql: str) -> tuple[list[dict], int]:
        payload = sql.encode() + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!I", len(payload) + 4) + payload)
        cols: list[tuple[str, int]] = []
        rows: list[dict] = []
        count = 0
        err = None
        while True:
            t, data = self._msg()
            if t == b"T":
                cols = _row_description(data)
                rows = []
            elif t == b"D":
                rows.append(_data_row(data, cols))
            elif t == b"C":
                tag = data.rstrip(b"\0").decode().split()
                count = int(tag[-1]) if tag and tag[-1].isdigit() else 0
            elif t == b"E":
                err = _error(data)
            elif t == b"Z":
                if err:
                    raise err
                return rows, count

    def close(self) -> None:
        try:
            self.sock.sendall(b"X" + struct.pack("!I", 4))
        finally:
            self.sock.close()


def _row_description(data: bytes) -> list[tuple[str, int]]:
    (n,) = struct.unpack("!H", data[:2])
    pos, cols = 2, []
    for _ in range(n):
        end = data.index(b"\0", pos)
        name = data[pos:end].decode()
        pos = end + 1
        _tbl, _att, oid, _sz, _mod, _fmt = struct.unpack("!IHIhih", data[pos:pos + 18])
        pos += 18
        cols.append((name, oid))
    return cols


def _data_row(data: bytes, cols: list[tuple[str, int]]) -> dict:
    (n,) = struct.unpack("!H", data[:2])
    pos, row = 2, {}
    for i in range(n):
        (ln,) = struct.unpack("!i", data[pos:pos + 4])
        pos += 4
        name, oid = cols[i]
        if ln == -1:
            row[name] = None
            continue
        raw = data[pos:pos + ln].decode()
        pos += ln
        row[name] = _CONVERT.get(oid, str)(raw)
    return row


def _error(data: bytes) -> DatabaseError:
    fields = {}
    for part in data.split(b"\0"):
        if part:
            fields[chr(part[0])] = part[1:].decode(errors="replace")
    return DatabaseError(fields.get("C"), fields.get("M", "?"))


class _Tx:
    def __init__(self, conn: PgConnection) -> None:
        self.conn = conn

    def fetchall(self, sql, params=None):
        return self.conn.query(interpolate(sql, params))[0]

    def fetchone(self, sql, params=None):
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=None):
        return self.conn.query(interpolate(sql, params))[1]


class PgWireDatabase:
    """Stejné rozhraní jako db.PsycopgDatabase."""

    def __init__(self, user: str, database: str) -> None:
        self.user, self.database = user, database

    @contextmanager
    def transaction(self) -> Iterator[_Tx]:
        conn = PgConnection(self.user, self.database)
        try:
            conn.query("BEGIN")
            try:
                yield _Tx(conn)
            except BaseException:
                conn.query("ROLLBACK")
                raise
            conn.query("COMMIT")
        finally:
            conn.close()
