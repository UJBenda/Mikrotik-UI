"""Zálohy a obnova controlleru (ne konfigurací zařízení – ty jsou v DB a jdou s ní).

Záloha = jeden šifrovaný soubor `mtc-<host>-<čas>.mtcbak`:

    tar: manifest.json   formát, verze aplikace, verze schématu DB, režim, čas
         db.dump         pg_dump -Fc (vše: zařízení, zálohy zařízení, uživatelé, logy…)
         etc/…           config.toml, master.key, hesla koncentrátorů, CA pro zařízení, plán průvodce
         ssl/ca.key|crt  vlastní CA controlleru (zařízení jí věří – po obnově musí zůstat stejná)

Šifrování: heslo zálohy → scrypt → AES-256-GCM po blocích 1 MiB (pořadí a konec
souboru jsou v AAD, zkrácení nebo přehození bloků se pozná). Heslo vygeneruje průvodce
a ukáže ho v záchranných údajích; na serveru je v `backup.passphrase_file` (jen root).

Verze: obnova kontroluje verzi schématu. Starší záloha do novější aplikace = po obnovení
DB se pustí migrace (upgrade). Záloha z novější verze do starší aplikace se odmítne –
nejdřív je potřeba nainstalovat aspoň tu verzi, ze které záloha je.

Spouští root (`mtc-admin backup`, timer `mtc-backup.timer`, na požádání z UI přes
`mtc-backup.path`); obnova `mtc-admin restore` nebo průvodce prvního spuštění.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import re
import secrets
import socket
import struct
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from . import __version__

log = logging.getLogger("mtcontroller.sysbackup")

FORMAT = 1
MAGIC = b"MTCBAK1\n"
CHUNK = 1 << 20
SUFFIX = ".mtcbak"
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
# soubory z /etc/mtcontroller, které patří do zálohy (backup.key ne – je to heslo samo)
ETC_FILES = re.compile(r"^(config\.toml|master\.key|[a-z0-9_-]+\.password|device-ca\.crt|setup-plan\.json)$")
# CA controlleru a certifikáty nginx (slotů); active.json = druh certifikátu slotu (tlscfg)
SSL_FILES = ("ca.key", "ca.crt", "admin.crt", "admin.key", "checkin.crt", "checkin.key")
PUBLIC_FILES = ("active.json",)


class BackupError(RuntimeError):
    pass


# --- šifrování ----------------------------------------------------------------------------


def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, **_SCRYPT).derive(passphrase.encode())


def new_passphrase() -> str:
    """Heslo zálohy: 6 skupin po 5 znacích (~150 bitů), bez zaměnitelných znaků."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(6))


def encrypt_stream(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, prefix = os.urandom(16), os.urandom(8)
    head = MAGIC + salt + prefix
    aes = AESGCM(_key(passphrase, salt))
    dst.write(head)
    n = 0
    buf = src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK) if buf else b""
        last = not nxt
        aad = head + struct.pack(">IB", n, 1 if last else 0)
        ct = aes.encrypt(prefix + struct.pack(">I", n), buf, aad)
        dst.write(struct.pack(">I", len(ct)) + ct)
        if last:
            return
        buf, n = nxt, n + 1


def decrypt_stream(src: BinaryIO, dst: BinaryIO, passphrase: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    head = src.read(len(MAGIC) + 24)
    if len(head) != len(MAGIC) + 24 or not head.startswith(MAGIC):
        raise BackupError("soubor není záloha controlleru (.mtcbak)")
    salt, prefix = head[len(MAGIC):len(MAGIC) + 16], head[len(MAGIC) + 16:]
    aes = AESGCM(_key(passphrase, salt))
    n = 0
    while True:
        raw = src.read(4)
        if len(raw) != 4:
            raise BackupError("záloha je zkrácená (chybí konec)")
        (size,) = struct.unpack(">I", raw)
        if size > CHUNK + 64:
            raise BackupError("záloha je poškozená")
        ct = src.read(size)
        if len(ct) != size:
            raise BackupError("záloha je zkrácená")
        pos = src.tell() if hasattr(src, "tell") else None
        peek = src.read(1)
        last = not peek
        if pos is not None and peek:
            src.seek(pos)
        try:
            pt = aes.decrypt(prefix + struct.pack(">I", n), ct, head + struct.pack(">IB", n, 1 if last else 0))
        except InvalidTag:
            if n == 0:
                raise BackupError("špatné heslo zálohy (nebo poškozený soubor)") from None
            raise BackupError("záloha je poškozená nebo upravená") from None
        dst.write(pt)
        if last:
            return
        n += 1


# --- vytvoření ----------------------------------------------------------------------------


@dataclass
class Paths:
    etc: Path = Path("/etc/mtcontroller")
    ssl: Path = Path("/etc/ssl/mtc")
    public: Path = Path("/etc/ssl/mtc-public")


def schema_version(db) -> int:
    with db.transaction() as tx:
        r = tx.fetchone("SELECT coalesce(max(version), 0) AS v FROM schema_migration")
    return int(r["v"])


def dbname(dsn: str) -> str:
    m = re.search(r"(?:^|\s)dbname=([a-z_][a-z0-9_]{0,62})(?:\s|$)", dsn)
    if not m:
        raise BackupError("v database.migrate_dsn chybí dbname")
    return m.group(1)


def _as_postgres(cmd: list[str]) -> list[str]:
    """pg_dump/pg_restore jako superuser postgres: RLS je FORCE i pro vlastníka schématu,
    vlastník by tabulky tenantů neviděl celé. Superuser RLS obchází."""
    return ["runuser", "-u", "postgres", "--", *cmd] if os.geteuid() == 0 else cmd


def _pg_dump(name: str, out: Path) -> None:
    with open(out, "wb") as fh:
        r = subprocess.run(_as_postgres(["pg_dump", "-Fc", "--no-password", "-d", name]), stdout=fh,
                           stderr=subprocess.PIPE, timeout=3600)
    if r.returncode != 0:
        raise BackupError("pg_dump selhal: " + r.stderr.decode(errors="replace").strip()[-400:])


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o600) -> None:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = len(data), mode, int(dt.datetime.now().timestamp())
    tar.addfile(ti, io.BytesIO(data))


def create(settings, owner_db, out_dir: Path | None = None, passphrase: str | None = None, paths: Paths = Paths(),
           dump: Callable[[str, Path], None] = _pg_dump, reason: str = "scheduled", keep: int | None = None) -> Path:
    """Vytvoří šifrovanou zálohu a promaže staré (nechá `keep` – plán z administrace –,
    jinak `backup.keep` posledních)."""
    out_dir = Path(out_dir or settings.backup.dir)
    if passphrase is None:
        passphrase = settings.backup.passphrase_file.read_text().strip()
    if len(passphrase) < 12:
        raise BackupError("heslo zálohy chybí nebo je krátké")
    out_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    host = re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname())[:40] or "controller"
    final = out_dir / f"mtc-{host}-{now:%Y%m%d-%H%M%S}{SUFFIX}"
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".tmp-") as td:
        tdp = Path(td)
        os.chmod(tdp, 0o700)
        dump_p = tdp / "db.dump"
        dump(dbname(settings.database.migrate_dsn), dump_p)
        manifest = {"format": FORMAT, "app_version": __version__, "schema": schema_version(owner_db),
                    "created_at": now.isoformat(), "hostname": socket.gethostname(),
                    "mode": "local" if settings.tunnel.local else "remote", "reason": reason, "files": []}
        tar_p = tdp / "backup.tar"
        with tarfile.open(tar_p, "w") as tar:
            tar.add(dump_p, "db.dump")
            for f in sorted(paths.etc.iterdir()) if paths.etc.is_dir() else []:
                if f.is_file() and ETC_FILES.match(f.name):
                    tar.add(f, f"etc/{f.name}")
                    manifest["files"].append(f"etc/{f.name}")
            for name in SSL_FILES:
                f = paths.ssl / name
                if f.is_file():
                    tar.add(f, f"ssl/{name}")
                    manifest["files"].append(f"ssl/{name}")
            for name in PUBLIC_FILES:
                f = paths.public / name
                if f.is_file():
                    tar.add(f, f"ssl-public/{name}")
                    manifest["files"].append(f"ssl-public/{name}")
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode(), 0o644)
        part = final.with_name("." + final.name + ".part")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "wb") as dst, open(tar_p, "rb") as src:
            encrypt_stream(src, dst, passphrase)
        _group(part)
        os.replace(part, final)
    prune(out_dir, keep or settings.backup.keep)
    log.info("záloha %s (%d B)", final, final.stat().st_size)
    return final


def _group(p: Path) -> None:
    """Soubor čitelný pro skupinu mtcontroller (UI ho nabídne ke stažení – je šifrovaný)."""
    try:
        import shutil
        shutil.chown(p, "root", "mtcontroller")
    except (LookupError, PermissionError, OSError):
        pass


def listing(out_dir: Path) -> list[dict]:
    if not out_dir.is_dir():
        return []
    out = []
    for p in out_dir.iterdir():
        if p.is_file() and p.name.endswith(SUFFIX) and not p.name.startswith("."):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size,
                        "at": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()})
    # podle času v názvu (po obnově na jiný server se mění jméno hostitele)
    return sorted(out, key=lambda x: x["name"][-22:], reverse=True)


def prune(out_dir: Path, keep: int) -> list[str]:
    old = listing(out_dir)[keep:]
    for b in old:
        (out_dir / b["name"]).unlink(missing_ok=True)
    return [b["name"] for b in old]


# --- obnova -------------------------------------------------------------------------------


def open_backup(src: Path, passphrase: str, work: Path) -> dict:
    """Rozšifruje a bezpečně rozbalí zálohu do `work`; vrátí manifest."""
    tar_p = work / "backup.tar"
    fd = os.open(tar_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as dst, open(src, "rb") as s:
        decrypt_stream(s, dst, passphrase)
    allowed = {"manifest.json", "db.dump"} | {f"ssl/{n}" for n in SSL_FILES} | {f"ssl-public/{n}" for n in PUBLIC_FILES}
    with tarfile.open(tar_p) as tar:
        for m in tar.getmembers():
            if not m.isfile() or not (m.name in allowed or (m.name.startswith("etc/") and "/" not in m.name[4:]
                                                             and ETC_FILES.match(m.name[4:]))):
                raise BackupError(f"neočekávaný obsah zálohy: {m.name!r}")
        tar.extractall(work / "x", filter="data")
    tar_p.unlink()
    try:
        manifest = json.loads((work / "x" / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        raise BackupError("záloha nemá manifest") from e
    return manifest


def check_compatible(manifest: dict, known_schema: int) -> None:
    if manifest.get("format") != FORMAT:
        raise BackupError(f"neznámý formát zálohy {manifest.get('format')!r}")
    schema = int(manifest.get("schema") or 0)
    if schema > known_schema:
        raise BackupError(f"záloha je z novější verze controlleru ({manifest.get('app_version')}, schéma {schema}); "
                          f"tato instalace zná schéma jen do {known_schema} – nainstaluj aspoň stejnou verzi balíčku")


def restore(src: Path, passphrase: str, settings, *, paths: Paths = Paths(),
            recreate_db: Callable[[], None], pg_restore: Callable[[str, Path], None] | None = None,
            migrate_db: Callable[[], list[int]], known_schema: int | None = None) -> dict:
    """Obnoví controller ze zálohy. Služby musí stát. Vrací manifest + spuštěné migrace."""
    from . import migrate

    known = known_schema if known_schema is not None else len(migrate.available())
    with tempfile.TemporaryDirectory(prefix="mtc-restore-") as td:
        work = Path(td)
        manifest = open_backup(src, passphrase, work)
        check_compatible(manifest, known)
        x = work / "x"
        if not (x / "db.dump").is_file() or not (x / "etc" / "master.key").is_file():
            raise BackupError("záloha neobsahuje databázi nebo master.key")
        recreate_db()
        (pg_restore or _pg_restore)(dbname(settings.database.migrate_dsn), x / "db.dump")
        paths.etc.mkdir(parents=True, exist_ok=True)
        for f in sorted((x / "etc").iterdir()):
            mode = 0o644 if f.name == "device-ca.crt" else 0o640
            _put(paths.etc / f.name, f.read_bytes(), mode, group=True)
        for name in SSL_FILES:
            f = x / "ssl" / name
            if f.is_file():
                paths.ssl.mkdir(mode=0o750, parents=True, exist_ok=True)
                _put(paths.ssl / name, f.read_bytes(), 0o600 if name.endswith(".key") else 0o644)
                if name in ("admin.crt", "checkin.crt"):
                    paths.public.mkdir(mode=0o755, parents=True, exist_ok=True)
                    _put(paths.public / name, f.read_bytes(), 0o644)
        for name in PUBLIC_FILES:
            f = x / "ssl-public" / name
            if f.is_file():
                paths.public.mkdir(mode=0o755, parents=True, exist_ok=True)
                _put(paths.public / name, f.read_bytes(), 0o644)
    ran = migrate_db()
    return {"manifest": manifest, "migrated": ran}


def _put(path: Path, data: bytes, mode: int, group: bool = False) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    if group:
        try:
            import shutil
            shutil.chown(tmp, "root", "mtcontroller")
        except (LookupError, PermissionError, OSError):
            pass
    os.replace(tmp, path)


def _pg_restore(name: str, dump: Path) -> None:
    with open(dump, "rb") as fh:
        r = subprocess.run(_as_postgres(["pg_restore", "--no-password", "--exit-on-error", "-d", name]), stdin=fh,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7200)
    if r.returncode != 0:
        raise BackupError("pg_restore selhal: " + r.stderr.decode(errors="replace").strip()[-600:])


def recreate_db_as_postgres(name: str = "mtcontroller") -> None:
    """Prázdná DB vlastněná mtc_owner (jako při instalaci). Spouští root přes uživatele postgres."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError("neplatné jméno DB")
    sql = (f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}' AND pid <> pg_backend_pid();\n"
           f"DROP DATABASE IF EXISTS {name};\nCREATE DATABASE {name} OWNER mtc_owner;\n")
    steps = [(["psql", "-qX", "-v", "ON_ERROR_STOP=1", "-d", "postgres"], sql),
             (["psql", "-qX", "-v", "ON_ERROR_STOP=1", "-d", name, "-c", "REVOKE ALL ON SCHEMA public FROM PUBLIC",
               "-c", "ALTER SCHEMA public OWNER TO mtc_owner"], None)]
    for cmd, stdin in steps:
        r = subprocess.run(["runuser", "-u", "postgres", "--", *cmd], input=stdin,
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise BackupError("příprava databáze selhala: " + r.stderr.strip()[-400:])


# --- žádost z UI a stav -------------------------------------------------------------------


def request_path(data_dir: Path) -> Path:
    return data_dir / "backup-request"


def status_path(data_dir: Path) -> Path:
    return data_dir / "backup-status.json"


def write_status(data_dir: Path, **data) -> None:
    p = status_path(data_dir)
    _put(p, json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), **data}).encode(), 0o640, group=True)
    try:
        import shutil
        shutil.chown(p, "mtcontroller", "mtcontroller")
    except (LookupError, PermissionError, OSError):
        pass


def read_status(data_dir: Path) -> dict:
    try:
        return json.loads(status_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
