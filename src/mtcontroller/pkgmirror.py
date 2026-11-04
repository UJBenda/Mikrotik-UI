"""Balíčky RouterOS přes controller pro izolovaná zařízení.

Řízené switche a AP v lokálním režimu nevidí do internetu (izolace, etapa 3c), takže si
aktualizace ani ovladač Wi-Fi nestáhnou z mikrotik.com. Balíčky proto stahuje controller
(worker, HTTPS z download.mikrotik.com) do `<data_dir>/pkg` a zařízení si je bere z
`/api/checkin/pkg/<soubor>` s klíčem check-inu. Podpis balíčku ověřuje RouterOS při instalaci.

- Příkaz `upgrade` / `wifi_package` izolovaného zařízení se doručí, až jsou balíčky u controlleru
  (do té doby čeká ve frontě, worker je mezitím stáhne).
- Dostupnou verzi zjišťuje controller (upgrade.mikrotik.com, NEWESTa7.<kanál>) a doplní ji do
  inventury izolovaného zařízení – stejně jako by ji nahlásilo zařízení samo.
- Výjimka „Povolit přístup do internetu“ u zařízení = zařízení bere vše jako dřív přímo.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import ssl
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import packages, routeros
from .db import Database, Tx, operator_scope
from .routeros import q

log = logging.getLogger("mtcontroller.pkgmirror")
DOWNLOAD = packages.DOWNLOAD
NEWEST = "https://upgrade.mikrotik.com/routeros/NEWESTa7.{channel}"
CHANNELS = ("stable", "long-term", "testing")
FILE_RE = re.compile(r"^(?P<pkg>[a-z0-9]+(-[a-z0-9]+)*)-(?P<v>\d+\.\d+(\.\d+)?(beta\d+|rc\d+)?)-(?P<arch>[a-z0-9]+)\.npk$")
MAX_SIZE = 200 * 1024 * 1024
KEEP_DAYS = 45
LATEST_KEY = "ros_latest"
MIRROR_KINDS = ("upgrade", "wifi_package")
Getter = Callable[[str, Path | None], bytes | None]


def pkg_dir(data_dir: Path) -> Path:
    return data_dir / "pkg"


def isolated(tx: Tx, settings, device_id: Any) -> bool:
    """Bez přístupu do internetu: lokální režim, řízené zařízení, bez výjimky."""
    if not settings.tunnel.local:
        return False
    r = tx.fetchone("""SELECT d.managed_at, coalesce(i.internet, false) AS internet FROM device d
                       LEFT JOIN device_intent i ON i.device_id = d.id WHERE d.id = %(d)s""", {"d": str(device_id)})
    return bool(r and r["managed_at"] is not None and not r["internet"])


def _arch_ver(inv: dict) -> tuple[str, str]:
    res = inv.get("resource") if isinstance(inv.get("resource"), dict) else {}
    ver = str(res.get("version") or "").split(" ")[0]
    arch = str(res.get("architecture-name") or "")
    if arch not in packages.ARCHS or arch == "x86":
        raise ValueError("balíčky přes controller: neznámá nebo nepodporovaná architektura (x86/CHR zatím ne)")
    return arch, ver


def upgrade_files(inv: dict, version: str) -> list[str]:
    """Všechny zapnuté balíčky zařízení v cílové verzi (routeros + wifi-qcom…)."""
    arch, _v = _arch_ver(inv)
    names = [n for n, p in packages.installed(inv).items() if not packages._true(p.get("disabled"))]
    if "routeros" not in names:
        names.insert(0, "routeros")
    return sorted({f"{n}-{version}-{arch}.npk" for n in names if re.fullmatch(r"[a-z0-9-]{2,40}", n)})


def wifi_files(inv: dict) -> list[str]:
    st = packages.wifi_status(inv)
    if not st["fix"]:
        raise ValueError("zařízení nepotřebuje výměnu Wi-Fi balíčku (nebo ji nejde určit)")
    arch, ver = _arch_ver(inv)
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", ver):
        raise ValueError("neznámá verze RouterOS")
    return [f"{st['fix']['install']}-{ver}-{arch}.npk"]


def url_for(fname: str) -> str:
    m = FILE_RE.match(fname)
    if not m:
        raise ValueError(f"neplatný soubor balíčku {fname!r}")
    return DOWNLOAD.format(v=m["v"], pkg=m["pkg"], arch=m["arch"])


def ready(data_dir: Path, files: list[str]) -> bool:
    return all((pkg_dir(data_dir) / f).is_file() for f in files)


def files_for(inv: dict, cmd: dict) -> list[str]:
    if cmd["kind"] == "upgrade":
        return upgrade_files(inv, str((cmd.get("params") or {}).get("version") or ""))
    return wifi_files(inv)


# --- stažení (worker) -----------------------------------------------------------------------------------------

def _http_get(url: str, dst: Path | None = None) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "mtcontroller"})
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:  # noqa: S310
        if dst is None:
            return r.read(4096)
        n = 0
        with open(dst, "wb") as fh:
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if n > MAX_SIZE:
                    raise ValueError("balíček je podezřele velký")
                fh.write(chunk)
    return None


def fetch(data_dir: Path, files: list[str], get: Getter = _http_get) -> int:
    d = pkg_dir(data_dir)
    d.mkdir(mode=0o750, parents=True, exist_ok=True)
    n = 0
    for f in files:
        dst = d / f
        if dst.is_file():
            continue
        tmp = dst.with_name(f + ".part")
        try:
            get(url_for(f), tmp)
            if tmp.stat().st_size < 1024:
                raise ValueError("stažený balíček je prázdný")
            os.replace(tmp, dst)
            n += 1
        except Exception as e:  # noqa: BLE001 – další balíček zkusit tak jako tak
            log.warning("balíček %s se nestáhl: %s", f, e)
            tmp.unlink(missing_ok=True)
    return n


def wanted(db: Database, settings) -> list[str]:
    """Balíčky, na které čekají příkazy izolovaných zařízení."""
    from . import inventory

    out: set[str] = set()
    with operator_scope(db) as tx:
        cmds = tx.fetchall(
            """SELECT c.id, c.kind, c.params, c.device_id FROM device_command c
               WHERE c.status = 'queued' AND c.kind IN ('upgrade', 'wifi_package')""", {})
        for c in cmds:
            if not isolated(tx, settings, c["device_id"]):
                continue
            inv = inventory.load(tx, c["device_id"], ["packages", "resource", "wifi", "wireless", "wifi_radios"])
            try:
                out.update(files_for(inv, c))
            except ValueError as e:
                log.info("příkaz %s: %s", c["id"], e)
    return sorted(out)


def latest(get: Getter = _http_get) -> dict[str, str]:
    """Nejnovější verze v kanálech (jako ji vidí zařízení samo)."""
    out = {}
    for ch in CHANNELS:
        try:
            raw = (get(NEWEST.format(channel=ch), None) or b"").decode("ascii", "ignore").split()
        except Exception as e:  # noqa: BLE001
            log.info("kanál %s: %s", ch, e)
            continue
        if raw and re.fullmatch(r"\d+\.\d+(\.\d+)?(beta\d+|rc\d+)?", raw[0]):
            out[ch] = raw[0]
    return out


def run(db: Database, settings, get: Getter = _http_get) -> int:
    """Krok workeru: stáhnout čekající balíčky, jednou za 6 h zjistit verze, promazat staré."""
    data_dir = settings.paths.data_dir
    n = fetch(data_dir, wanted(db, settings), get)
    if settings.tunnel.local:
        with operator_scope(db) as tx:
            r = tx.fetchone("SELECT data, updated_at FROM system_setting WHERE key = %(k)s", {"k": LATEST_KEY})
        if not r or dt.datetime.now(dt.timezone.utc) - r["updated_at"] > dt.timedelta(hours=6):
            lv = latest(get)
            if lv:
                with operator_scope(db) as tx:
                    tx.execute("""INSERT INTO system_setting (key, data) VALUES (%(k)s, %(d)s)
                                  ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                               {"k": LATEST_KEY, "d": lv})
    cutoff = dt.datetime.now().timestamp() - KEEP_DAYS * 86400
    for f in pkg_dir(data_dir).glob("*.npk") if pkg_dir(data_dir).is_dir() else []:
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    return n


def fill_update(db: Database, settings, device_id: Any, data: dict) -> None:
    """Inventura izolovaného zařízení: dostupnou verzi doplní controller (zařízení se nezeptá)."""
    upd = data.get("update")
    if not isinstance(upd, dict) or upd.get("latest-version"):
        return
    with operator_scope(db) as tx:
        if not isolated(tx, settings, device_id):
            return
        r = tx.fetchone("SELECT data FROM system_setting WHERE key = %(k)s", {"k": LATEST_KEY})
    v = ((r or {}).get("data") or {}).get(str(upd.get("channel") or "stable"))
    if v:
        data["update"] = {**upd, "latest-version": v, "status": "zjistil controller (zařízení bez internetu)"}


# --- skripty pro zařízení ------------------------------------------------------------------------------------

def _fetch_lines(files: list[str], base: str, key: str, check_certificate: str) -> list[str]:
    lines = [':local p ""', ':if ([:len [/file find name="flash" type="directory"]] > 0) do={ :set p "flash/" }']
    for f in files:
        lines.append(f"/tool fetch url={q(base + '/api/checkin/pkg/' + f)} "
                     f"http-header-field={q('Authorization: Bearer ' + key)} dst-path=($p . {q(f)}) "
                     f"check-certificate={check_certificate}")
    lines.append(":delay 2s")
    for f in files:
        lines.append(f':if ([:len [/file find name=($p . {q(f)})]] = 0) do={{ :error "mtc: balicek se nestahl" }}')
    return lines


def upgrade_body(version: str, files: list[str], base: str, key: str, check_certificate: str) -> str:
    """Aktualizace z balíčků u controlleru: stáhnout, restart nainstaluje (tail)."""
    if not routeros._VERSION.match(version):
        raise ValueError(f"neplatná verze {version!r}")
    v = q(version)
    body = [f':log info ("mtc: aktualizace z controlleru na " . {v})',
            f':if ([/system package update get installed-version] != {v}) do={{']
    body += ["  " + ln for ln in _fetch_lines(files, base, key, check_certificate)]
    body.append("}")
    return "\n".join(body) + "\n"


def upgrade_tail(version: str) -> str:
    return (f':if ([/system package update get installed-version] != {q(version)}) do={{\n'
            f'  :execute ":delay 5s; /system reboot"\n'
            f'}}\n')


def wifi_body(inv: dict, base: str, key: str, check_certificate: str) -> str:
    st = packages.wifi_status(inv)
    files = wifi_files(inv)
    lines = [f':log warning {q("mtc: vymena Wi-Fi ovladace na " + st["fix"]["install"] + " (z controlleru)")}']
    lines += _fetch_lines(files, base, key, check_certificate)
    for r in st["fix"]["remove"]:
        lines.append(f"/system package uninstall {r}")
    return "\n".join(lines) + "\n"
