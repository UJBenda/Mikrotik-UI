"""Kontrola instalace: `mtc-admin doctor`.

Projde konfiguraci, master klíč, databázi, migrace, koncentrátory a síť
(IP controlleru, routa na tunelový pool) a u každé chyby napíše, co s ní.
Nic nemění – jen čte. Spouští se jako root (stejně jako ostatní mtc-admin).
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings

OK, WARN, FAIL = "OK", "!!", "XX"
ZERO_FP = "0" * 64


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""


def _readable(path: Path) -> str | None:
    """None = v pořádku, jinak popis problému."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return f"{path} neexistuje"
    except PermissionError:
        return f"{path} nejde číst"
    if st.st_mode & stat.S_IROTH:
        return f"{path} je čitelný pro všechny (chmod 0640, chown root:mtcontroller)"
    return None


def check_config(s: Settings) -> list[Check]:
    out = []
    example = [h for h in (s.web.ui_host, s.web.checkin_host) if h.endswith("example.cz")]
    if example:
        out.append(Check(FAIL, "hostname", f"v config.toml zůstalo {', '.join(example)}"))
    else:
        out.append(Check(OK, "hostname", f"UI {s.web.ui_host}, check-in {s.web.checkin_host}"))
    if s.security.device_check_certificate == "no":
        out.append(Check(WARN, "certifikáty zařízení",
                         "device_check_certificate = \"no\" – jen laboratoř, v produkci \"yes-without-crl\""))
    return out


def check_master_key(s: Settings) -> list[Check]:
    from .security import SecretBox
    try:
        box = SecretBox.from_file(s.security.master_key_file)
        if box.open(box.seal(b"doctor", b"doctor"), b"doctor") != b"doctor":
            raise ValueError("šifrování nefunguje")
    except Exception as e:  # noqa: BLE001 – hlásíme cokoliv
        return [Check(FAIL, "master klíč", f"{type(e).__name__}: {e}")]
    return [Check(OK, "master klíč", f"{s.security.master_key_file} (nezapomeň na offline zálohu)")]


def check_database(s: Settings, app_db, owner_db) -> list[Check]:
    from . import migrate
    from .db import operator_scope
    out = []
    try:
        with operator_scope(app_db) as tx:
            n = tx.fetchone("""SELECT count(*) AS n FROM role_binding WHERE role = 'superadmin'""")["n"]
        out.append(Check(OK, "databáze (aplikace)", "připojení rolí aplikace funguje"))
        if n == 0:
            out.append(Check(WARN, "superadmin", "zatím žádný – mtc-admin create-superadmin EMAIL"))
        else:
            out.append(Check(OK, "superadmin", f"{n}"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(FAIL, "databáze (aplikace)",
                         f"{e} – zkontroluj pg_ident.conf (mapa mtc) a pg_hba.conf, systemctl reload postgresql"))
    try:
        with owner_db.transaction() as tx:
            have = {r["version"] for r in tx.fetchall("SELECT version FROM schema_migration")}
        want = {v for v, _ in migrate.available()}
        missing = sorted(want - have)
        if missing:
            out.append(Check(FAIL, "migrace", f"chybí {missing} – mtc-admin migrate"))
        else:
            out.append(Check(OK, "migrace", f"schéma aktuální ({max(want)})"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(FAIL, "migrace", f"{e} – mtc-admin migrate"))
    return out


def _route_get(ip: str) -> dict | None:
    try:
        r = subprocess.run(["ip", "-j", "route", "get", ip], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout or "[]")
        return data[0] if data else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _is_local(ip: str) -> bool:
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with socket.socket(fam, socket.SOCK_DGRAM) as so:
        try:
            so.bind((ip, 0))
            return True
        except OSError:
            return False


def check_network(s: Settings, route_get: Callable[[str], dict | None] = _route_get,
                  is_local: Callable[[str], bool] = _is_local) -> list[Check]:
    from urllib.parse import urlsplit
    out = []
    ctrl = s.tunnel.controller_ip
    if is_local(ctrl):
        out.append(Check(OK, "controller_ip", f"{ctrl} je adresa tohoto serveru"))
    else:
        out.append(Check(FAIL, "controller_ip",
                         f"{ctrl} není na žádném rozhraní – koncentrátor pouští REST/src-nat jen z této adresy"
                         if not s.tunnel.local else
                         f"{ctrl} není na žádném rozhraní – zařízení pouští správu jen z této adresy"))
    if s.tunnel.local:
        out.append(Check(OK, "režim", "lokální – zařízení se spravují přímo v LAN, bez tunelů"))
        out.extend(_check_dns(s))
        return out
    pool = ipaddress.ip_network(s.tunnel.pool)
    probe = str(pool.network_address + 256 + 2)  # první adresa v bloku prvního tenanta
    r = route_get(probe)
    gws = {urlsplit(c.api_url).hostname for c in s.tunnel.concentrators}
    if r is None:
        out.append(Check(WARN, "routa na pool", f"nelze zjistit (ip route get {probe})"))
    elif r.get("gateway") in gws:
        src = r.get("prefsrc")
        detail = f"{pool} přes {r['gateway']}"
        if src and src != ctrl:
            out.append(Check(WARN, "routa na pool", f"{detail}, ale zdrojová adresa {src} ≠ controller_ip {ctrl}"))
        else:
            out.append(Check(OK, "routa na pool", detail))
    else:
        via = r.get("gateway") or r.get("dev") or "?"
        first = sorted(g for g in gws if g)[:1]
        hint = f"ip route add {pool} via {first[0]}" if first else "ip route add …"
        out.append(Check(FAIL, "routa na pool",
                         f"{probe} jde přes {via}, ne přes koncentrátor – {hint} (a trvale v /etc/network/interfaces)"))
    out.extend(_check_dns(s))
    return out


def _check_dns(s: Settings) -> list[Check]:
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(s.web.checkin_host, 443, proto=socket.IPPROTO_TCP)})
        return [Check(OK, "DNS check-in", f"{s.web.checkin_host} → {', '.join(addrs)} (musí být dostupné i ze zařízení)")]
    except OSError as e:
        return [Check(WARN, "DNS check-in", f"{s.web.checkin_host} se nepřeložil ({e}) – zařízení ho přeložit musí")]


def _worker_caps() -> str:
    try:
        r = subprocess.run(["systemctl", "show", "mtc-worker", "-p", "AmbientCapabilities", "--value"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""


def check_routes(s: Settings, caps: Callable[[], str] = _worker_caps) -> list[Check]:
    if not s.tunnel.manage_routes:
        if len(s.tunnel.concentrators) > 1:
            return [Check(WARN, "routy přes koncentrátory",
                          "dva koncentrátory bez tunnel.manage_routes – po failoveru zařízení nepůjde REST")]
        return []
    if "cap_net_admin" not in caps():
        return [Check(FAIL, "routy přes koncentrátory",
                      "manage_routes = true, ale mtc-worker nemá CAP_NET_ADMIN – spusť znovu deploy/install.sh")]
    return [Check(OK, "routy přes koncentrátory", "worker udržuje /32 routy podle aktivních relací")]


def check_concentrators(s: Settings, providers=None) -> list[Check]:
    out = []
    if s.tunnel.local:
        return []
    if not s.tunnel.concentrators:
        return [Check(FAIL, "koncentrátory", "v config.toml chybí [[tunnel.concentrators]]")]
    for c in s.tunnel.concentrators:
        problem = _readable(c.api_password_file)
        if problem:
            out.append(Check(FAIL, f"{c.name}: heslo", problem))
        if c.api_fingerprint.replace(":", "").lower() == ZERO_FP:
            out.append(Check(FAIL, f"{c.name}: otisk",
                             "api_fingerprint není vyplněný – spusť concentrator-script na koncentrátoru a otisk vlož"))
    if providers is None:
        from .tunnel import build_providers
        try:
            providers = build_providers(s)
        except Exception as e:  # noqa: BLE001
            return out + [Check(FAIL, "koncentrátory", f"{e}")]
    for p in providers:
        try:
            ident = p.api.get("/system/identity")
            n = len(p.active_sessions())
            out.append(Check(OK, f"{p.name}: REST", f"{ident.get('name')}, aktivních SSTP relací {n}"))
        except Exception as e:  # noqa: BLE001
            out.append(Check(FAIL, f"{p.name}: REST", f"{e}"))
        finally:
            close = getattr(p.api, "close", None)
            if close:
                close()
    return out


def check_notify(s: Settings) -> list[Check]:
    out = []
    for t in s.notify:
        problem = _readable(t.secret_file)
        out.append(Check(FAIL, f"notifikace {t.name}", problem) if problem
                   else Check(OK, f"notifikace {t.name}", f"{t.kind} → mtc-admin notify-test {t.name}"))
    return out


def check_files(s: Settings) -> list[Check]:
    cfg = Path(os.environ.get("MTC_CONFIG", "/etc/mtcontroller/config.toml"))
    problem = _readable(cfg)
    return [Check(WARN, "config.toml", problem)] if problem else []


def check_oui() -> list[Check]:
    from . import oui

    if oui.available():
        return [Check(OK, "výrobci MAC", "registr IEEE (ieee-data) načten")]
    return [Check(WARN, "výrobci MAC", "chybí /usr/share/ieee-data/oui.csv – apt install ieee-data")]


def check_worker(s: Settings) -> list[Check]:
    from . import heartbeat

    st = heartbeat.read(s.paths.data_dir)
    if st is None:
        return [Check(WARN, "worker", "stav workeru zatím neexistuje – systemctl status mtc-worker")]
    if st["stale"]:
        return [Check(FAIL, "worker", f"poslední kolo před {st['age_seconds']} s – journalctl -u mtc-worker")]
    errs = [f"{k}: {v['error']}" for k, v in (st.get("steps") or {}).items() if v.get("error")]
    if errs:
        return [Check(WARN, "worker", "běží, ale kroky hlásí chyby: " + "; ".join(errs)[:400])]
    return [Check(OK, "worker", f"běží (kolo {st.get('tick')}, před {st['age_seconds']} s)")]


def check_backup(s: Settings) -> list[Check]:
    import datetime as dt

    from . import sysbackup

    out = []
    try:
        ok = len(s.backup.passphrase_file.read_text().strip()) >= 12
        out.append(Check(OK if ok else FAIL, "heslo záloh", str(s.backup.passphrase_file) if ok else "krátké"))
    except PermissionError:
        out.append(Check(WARN, "heslo záloh", "nejde přečíst – spusť doctor jako root"))
    except OSError:
        out.append(Check(FAIL, "heslo záloh", f"chybí {s.backup.passphrase_file} – zálohy neběží (mtc-admin recovery-kit)"))
    st = sysbackup.read_status(s.paths.data_dir)
    if st.get("state") == "error":
        out.append(Check(FAIL, "zálohy", f"poslední záloha selhala: {st.get('error', '')[:200]} – journalctl -u mtc-backup"))
    items = sysbackup.listing(s.backup.dir)
    if not items:
        out.append(Check(WARN, "zálohy", f"v {s.backup.dir} zatím žádná – sudo mtc-admin backup"))
    else:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(items[0]["at"])
        out.append(Check(OK if age < dt.timedelta(days=2) else WARN, "zálohy",
                         f"{len(items)} v {s.backup.dir}, poslední před {int(age.total_seconds() // 3600)} h"))
    return out


def run(s: Settings, app_db, owner_db, providers=None) -> list[Check]:
    checks: list[Check] = []
    for fn in (lambda: check_config(s), lambda: check_files(s), lambda: check_master_key(s),
               lambda: check_database(s, app_db, owner_db), lambda: check_network(s), lambda: check_routes(s), lambda: check_worker(s),
               check_oui, lambda: check_concentrators(s, providers), lambda: check_notify(s), lambda: check_backup(s)):
        try:
            checks.extend(fn())
        except Exception as e:  # noqa: BLE001 – jedna rozbitá kontrola nesmí shodit ostatní
            checks.append(Check(FAIL, "kontrola", f"{type(e).__name__}: {e}"))
    return checks


def render(checks: list[Check]) -> str:
    w = max((len(c.name) for c in checks), default=10)
    return "\n".join(f"[{c.status}] {c.name.ljust(w)}  {c.detail}" for c in checks)
