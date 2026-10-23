"""Zálohy controlleru: šifrování, obsah, retence, obnova a kontrola verzí."""

import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from helpers import drop_db, fresh_db, make_settings

from mtcontroller import migrate, sysbackup
from mtcontroller.db import operator_scope

PW = "abcde-fghij-kmnpq-rstuv-wxyz2-34567"


def test_encrypt_roundtrip_and_tamper():
    for size in (0, 10, sysbackup.CHUNK, sysbackup.CHUNK * 2 + 7):
        data = os.urandom(size)
        enc = io.BytesIO()
        sysbackup.encrypt_stream(io.BytesIO(data), enc, PW)
        out = io.BytesIO()
        sysbackup.decrypt_stream(io.BytesIO(enc.getvalue()), out, PW)
        assert out.getvalue() == data
    raw = enc.getvalue()
    with pytest.raises(sysbackup.BackupError, match="heslo"):
        sysbackup.decrypt_stream(io.BytesIO(raw), io.BytesIO(), "spatne-heslo-123")
    with pytest.raises(sysbackup.BackupError):  # zkrácený soubor (chybí poslední blok)
        sysbackup.decrypt_stream(io.BytesIO(raw[: len(raw) - 30]), io.BytesIO(), PW)
    bad = bytearray(raw)
    bad[len(sysbackup.MAGIC) + 24 + 4 + sysbackup.CHUNK + 40] ^= 1  # druhý blok
    with pytest.raises(sysbackup.BackupError, match="poškozená"):
        sysbackup.decrypt_stream(io.BytesIO(bytes(bad)), io.BytesIO(), PW)
    with pytest.raises(sysbackup.BackupError, match="není záloha"):
        sysbackup.decrypt_stream(io.BytesIO(b"PK\x03\x04nesmysl" * 10), io.BytesIO(), PW)
    assert len(sysbackup.new_passphrase()) == 35


def test_compatibility():
    sysbackup.check_compatible({"format": 1, "schema": 10}, 19)  # starší záloha → migrace
    with pytest.raises(sysbackup.BackupError, match="novější verze"):
        sysbackup.check_compatible({"format": 1, "schema": 25, "app_version": "0.3"}, 19)
    with pytest.raises(sysbackup.BackupError, match="formát"):
        sysbackup.check_compatible({"format": 9, "schema": 1}, 19)


def _etc(tmp: Path) -> sysbackup.Paths:
    etc, ssl = tmp / "etc", tmp / "ssl"
    etc.mkdir()
    ssl.mkdir()
    for n, v in {"config.toml": "[web]\n", "master.key": "MK", "vpn1.password": "pw", "device-ca.crt": "CA",
                 "setup-plan.json": "{}", "backup.key": PW, "setup-code": "X", "jine.txt": "ne"}.items():
        (etc / n).write_text(v)
    (ssl / "ca.key").write_text("KEY")
    (ssl / "ca.crt").write_text("CRT")
    (ssl / "server.key").write_text("ne")
    (ssl / "admin.crt").write_text("ADMIN-CRT")  # certifikát nginx (např. koupený)
    (ssl / "admin.key").write_text("ADMIN-KEY")
    pub = tmp / "pub"
    pub.mkdir()
    (pub / "active.json").write_text('{"admin": {"kind": "custom"}}')
    return sysbackup.Paths(etc, ssl, pub)


def test_create_content_and_prune(tmp_path):
    from pgwire import PgWireDatabase

    db, name = fresh_db()
    owner = PgWireDatabase("mtc_owner", name)
    try:
        s = make_settings(tmp_path)
        paths = _etc(tmp_path)
        out = tmp_path / "bk"
        made = []
        for i in range(3):
            def dump(dbn, p, i=i):
                assert dbn == "mtcontroller"
                p.write_bytes(b"DUMP%d" % i)
            f = sysbackup.create(s, owner, out, PW, paths, dump=dump)
            os.utime(f, (1000 + i, 1000 + i))
            made.append(f.rename(out / f"mtc-x-2026010{i}-000000.mtcbak"))
        assert oct(made[0].stat().st_mode & 0o777) == "0o640"
        assert sysbackup.prune(out, 2) == [made[0].name]
        assert [b["name"] for b in sysbackup.listing(out)] == [made[2].name, made[1].name]
        assert not [p for p in out.iterdir() if p.name.startswith(".")]  # žádné dočasné soubory
        work = tmp_path / "w"
        work.mkdir()
        man = sysbackup.open_backup(made[2], PW, work)
        assert man["schema"] == len(migrate.available()) and man["format"] == 1
        names = sorted(str(p.relative_to(work / "x")) for p in (work / "x").rglob("*") if p.is_file())
        assert names == ["db.dump", "etc/config.toml", "etc/device-ca.crt", "etc/master.key", "etc/setup-plan.json",
                         "etc/vpn1.password", "manifest.json", "ssl-public/active.json", "ssl/admin.crt",
                         "ssl/admin.key", "ssl/ca.crt", "ssl/ca.key"]
        assert (work / "x" / "db.dump").read_bytes() == b"DUMP2"
        with pytest.raises(sysbackup.BackupError, match="krátké"):
            sysbackup.create(s, owner, out, "kratke", paths, dump=lambda d, p: None)
    finally:
        drop_db(name)


def test_open_rejects_unexpected_members(tmp_path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        ti = tarfile.TarInfo("etc/../../evil")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))
    enc = tmp_path / "e.mtcbak"
    with open(enc, "wb") as fh:
        sysbackup.encrypt_stream(io.BytesIO(raw.getvalue()), fh, PW)
    with pytest.raises(sysbackup.BackupError, match="neočekávaný"):
        sysbackup.open_backup(enc, PW, tmp_path)


def test_real_dump_and_restore(tmp_path):
    """Skutečný pg_dump / pg_restore: data přežijí, starší schéma se domigruje."""
    if not shutil.which("pg_dump"):
        pytest.skip("pg_dump není")
    from pgwire import PgConnection, PgWireDatabase

    db, name = fresh_db()
    target = name + "_r"
    try:
        s = make_settings(tmp_path).model_copy(deep=True)
        s.database.migrate_dsn = f"dbname={name} user=mtc_owner host=/var/run/postgresql"
        with operator_scope(db) as tx:
            tx.execute("INSERT INTO tenant (name, tunnel_block) VALUES ('Zálohovaná', '10.200.9.0/24')")
        # simulace zálohy ze starší verze: poslední migrace „ještě nebyla“
        last, up = migrate.available()[-1]
        owner = PgWireDatabase("mtc_owner", name)
        with owner.transaction() as tx:
            tx.execute(Path(str(up).replace(".up.sql", ".down.sql")).read_text().replace("{app_role}", "mtc_app"))
            tx.execute("DELETE FROM schema_migration WHERE version = %(v)s", {"v": last})
        paths = _etc(tmp_path)
        f = sysbackup.create(s, PgWireDatabase("mtc_owner", name), tmp_path / "bk", PW, paths)

        def recreate():
            su = PgConnection("postgres", "postgres")
            su.query(f"DROP DATABASE IF EXISTS {target}")
            su.query(f"CREATE DATABASE {target} OWNER mtc_owner")
            su.close()
            su = PgConnection("postgres", target)
            su.query("REVOKE ALL ON SCHEMA public FROM PUBLIC; ALTER SCHEMA public OWNER TO mtc_owner")
            su.close()

        s2 = s.model_copy(deep=True)
        s2.database.migrate_dsn = f"dbname={target} user=mtc_owner host=/var/run/postgresql"
        dest = sysbackup.Paths(tmp_path / "etc2", tmp_path / "ssl2", tmp_path / "pub2")
        res = sysbackup.restore(f, PW, s2, paths=dest, recreate_db=recreate,
                                migrate_db=lambda: migrate.upgrade(PgWireDatabase("mtc_owner", target)))
        assert res["manifest"]["schema"] == last - 1 and res["migrated"] == [last]
        assert (dest.etc / "master.key").read_text() == "MK" and not (dest.etc / "backup.key").exists()
        assert oct((dest.ssl / "ca.key").stat().st_mode & 0o777) == "0o600"
        # certifikát nginx a jeho druh se obnoví (vlastní certifikát přežije přenos na nový server)
        assert oct((dest.ssl / "admin.key").stat().st_mode & 0o777) == "0o600"
        assert (dest.public / "admin.crt").read_text() == "ADMIN-CRT"
        assert json.loads((dest.public / "active.json").read_text())["admin"]["kind"] == "custom"
        app = PgWireDatabase("mtc_app", target)
        with operator_scope(app) as tx:
            assert tx.fetchone("SELECT name FROM tenant")["name"] == "Zálohovaná"
        # novější záloha do starší instalace se odmítne dřív, než se sáhne na DB
        touched = []
        with pytest.raises(sysbackup.BackupError, match="novější"):
            sysbackup.restore(f, PW, s2, paths=dest, recreate_db=lambda: touched.append(1),
                              migrate_db=lambda: [], known_schema=last - 2)
        assert not touched
    finally:
        su = PgConnection("postgres", "postgres")
        su.query(f"DROP DATABASE IF EXISTS {target}")
        su.close()
        drop_db(name)


def test_status_roundtrip(tmp_path):
    sysbackup.write_status(tmp_path, state="done", file="a.mtcbak")
    assert sysbackup.read_status(tmp_path)["state"] == "done"
    assert json.loads(sysbackup.status_path(tmp_path).read_text())["file"] == "a.mtcbak"
