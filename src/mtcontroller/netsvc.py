"""Čas a DNS pro řízená zařízení: NTP vždy z controlleru, DNS z administrace.

Controller běží jako NTP server (chrony z Debianu); v administraci (Systém → Čas a DNS) se jen
nastavuje, odkud si čas bere controller sám (`ntp_upstream`). Řízené switche a AP mají jako
NTP server vždy controller (`tunnel.controller_ip` – v lokálním režimu jeho adresa v LAN, ve
vzdáleném adresa, ze které chodí správa do tunelů) a DNS servery z administrace (`dns`;
prázdné = ponechat, co zařízení dostane z DHCP). Sledovaná zařízení se nemění.

Uložené v `system_setting` (klíč `netsvc`, jen provozovatel). Zápis do /etc/chrony dělá root:
web zapíše žádost `netsvc-request.json`, `mtc-netsvc.path` spustí `mtc-admin netsvc-apply`, ten
načte nastavení z DB, přepíše konfiguraci chrony a přenačte ji (stav do `netsvc-status.json`).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .db import Database, operator_scope
from .services import ServiceError, audit

log = logging.getLogger("mtcontroller.netsvc")
KEY = "netsvc"
_HOST = re.compile(r"^(?=.{1,100}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")
CHRONY_SOURCES = Path("/etc/chrony/sources.d/mtcontroller.sources")
CHRONY_CONF = Path("/etc/chrony/conf.d/mtcontroller.conf")
CHRONY_MAIN = Path("/etc/chrony/chrony.conf")
# lokální režim: zařízení jsou v LAN – čas jen privátním sítím
PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]
MARK = "# vypnuto MikroTik Controllerem (zdroje času: sources.d/mtcontroller.sources): "


def defaults() -> dict:
    from . import regional

    return {"ntp_upstream": regional.ntp_servers(), "dns": []}


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ServiceError("neplatné nastavení")
    out = defaults()
    if data.get("ntp_upstream") is not None:
        up = data["ntp_upstream"]
        up = up if isinstance(up, list) else str(up).replace(";", ",").replace(" ", ",").split(",")
        up = [str(h).strip() for h in up if str(h).strip()]
        if not 1 <= len(up) <= 4:
            raise ServiceError("NTP pro controller: 1 až 4 servery")
        for h in up:
            if not _HOST.match(h):
                raise ServiceError(f"neplatný NTP server: {h!r}")
        out["ntp_upstream"] = up
    if data.get("dns") is not None:
        dns = data["dns"]
        dns = dns if isinstance(dns, list) else str(dns).replace(";", ",").replace(" ", ",").split(",")
        dns = [str(h).strip() for h in dns if str(h).strip()]
        if len(dns) > 3:
            raise ServiceError("DNS: nejvýš 3 servery")
        for h in dns:
            try:
                ipaddress.ip_address(h)
            except ValueError:
                raise ServiceError(f"DNS server musí být IP adresa: {h!r}") from None
        out["dns"] = dns
    return out


def load(db: Database) -> dict:
    with operator_scope(db) as tx:
        r = tx.fetchone("SELECT data FROM system_setting WHERE key = %(k)s", {"k": KEY})
    try:
        return validate(r["data"] if r else {})
    except ServiceError:
        return defaults()


def save(db: Database, data: dict, user_id: Any = None, ip: str | None = None) -> dict:
    new = validate(data)
    with operator_scope(db) as tx:
        before = tx.fetchone("SELECT data FROM system_setting WHERE key = %(k)s", {"k": KEY})
        tx.execute("""INSERT INTO system_setting (key, data) VALUES (%(k)s, %(d)s)
                      ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                   {"k": KEY, "d": new})
        audit(tx, "system.netsvc", user_id=user_id, target="system:netsvc",
              before=before["data"] if before else None, after=new, ip=ip)
    return new


def device_view(db: Database, settings) -> dict:
    """Co dostanou řízená zařízení: {"ntp": [adresa controlleru], "dns": [...]}."""
    return {"ntp": [settings.tunnel.controller_ip], "dns": load(db)["dns"]}


# --- chrony na controlleru (root) ---------------------------------------------------------------------

def allow_nets(settings) -> list[str]:
    return PRIVATE if settings.tunnel.local else [settings.tunnel.pool]


def render_sources(data: dict) -> str:
    lines = ["# spravuje MikroTik Controller (Systém → Čas a DNS) – ruční úpravy se přepíšou"]
    lines += [f"server {h} iburst" for h in data["ntp_upstream"]]
    return "\n".join(lines) + "\n"


def render_conf(settings) -> str:
    lines = ["# spravuje MikroTik Controller – NTP server pro řízená zařízení"]
    lines += [f"allow {n}" for n in allow_nets(settings)]
    return "\n".join(lines) + "\n"


def disable_default_pool(text: str) -> str:
    """Výchozí `pool 2.debian.pool.ntp.org` vypnout – čas jen z nastavených serverů."""
    out = []
    for ln in text.splitlines(keepends=True):
        if re.match(r"\s*(pool|server)\s", ln):
            out.append(MARK + ln)
        else:
            out.append(ln)
    return "".join(out)


def status_path(data_dir: Path) -> Path:
    return data_dir / "netsvc-status.json"


def request_path(data_dir: Path) -> Path:
    return data_dir / "netsvc-request.json"


def write_request(data_dir: Path, who: str | None) -> None:
    """Web (bez práv k /etc): žádost pro root – jen spouštěč, data jsou v DB."""
    p = request_path(data_dir)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"user": who, "at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    os.replace(tmp, p)


def read_status(data_dir: Path) -> dict:
    try:
        return json.loads(status_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def _run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)


def apply(db: Database, settings, *, sources: Path = CHRONY_SOURCES, conf: Path = CHRONY_CONF,
          main: Path = CHRONY_MAIN, run: Callable = _run) -> dict:
    """`mtc-admin netsvc-apply` (root): konfigurace chrony podle DB a přenačtení."""
    data = load(db)
    for path, text in ((sources, render_sources(data)), (conf, render_conf(settings))):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    if main.is_file():
        old = main.read_text()
        new = disable_default_pool(old)
        if new != old:
            main.with_name(main.name + ".mtc-orig").write_text(old)
            tmp = main.with_name(main.name + ".tmp")
            tmp.write_text(new)
            os.chmod(tmp, 0o644)
            os.replace(tmp, main)
    r = run("systemctl", "restart", "chrony")
    ok = r.returncode == 0
    return {"ok": ok, "ntp_upstream": data["ntp_upstream"], "allow": allow_nets(settings),
            "error": None if ok else (r.stderr or r.stdout or "chrony nejde restartovat").strip()[:300]}


def apply_request(db: Database, settings, **kw) -> int:
    data_dir = settings.paths.data_dir
    req = request_path(data_dir)
    if req.exists():
        req.unlink()
    try:
        res = apply(db, settings, **kw)
    except Exception as e:  # noqa: BLE001 – stav musí dostat web
        log.exception("chrony se nepodařilo nastavit")
        res = {"ok": False, "error": str(e)[:300]}
    res["at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    p = status_path(data_dir)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(res))
    os.replace(tmp, p)
    try:
        import shutil

        shutil.chown(p, "mtcontroller", "mtcontroller")
    except (LookupError, PermissionError, OSError):
        pass
    return 0 if res["ok"] else 1
