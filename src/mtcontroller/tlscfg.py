"""Certifikáty HTTPS pro nginx: vždy něco platného pro start, Let's Encrypt nebo vlastní až potom.

Zásada: nginx nesmí zůstat bez certifikátu. Při instalaci se vždy nejdřív vydá certifikát
z vlastní CA controlleru (zařízení si ji stáhnou a ověří otiskem), nginx naběhne. Let's Encrypt
nebo nahraný certifikát se nastaví potom – v průvodci, `mtc-admin tls …` nebo v administraci
(Systém → Certifikát HTTPS); když se nepovede, zůstane běžet ten předchozí.

Sloty (soubory, na které ukazuje nginx; klíče jen root):
- `admin`   – administrace (u lokálního režimu i check-in – jedna adresa),
- `checkin` – check-in a adopce zařízení (vzdálená správa).

Zařízení ověřují certifikát check-inu: CA controlleru znají (z adopce), veřejné CA jen pokud
je RouterOS má v důvěryhodných (7.19+ vestavěné kořenové CA, /certificate/settings builtin-trust-store). Proto se u vzdálené správy
doporučuje Let's Encrypt jen pro administraci a check-in nechat na CA controlleru.

Let's Encrypt (`certbot --webroot`, ACME přes port 80, který nginx obsluhuje vždy) má deploy hook
`mtc-admin tls-sync`: po každém obnovení zkopíruje nový certifikát do slotu a přenačte nginx.

Privilegované kroky dělá root (`mtc-admin tls-apply`, `mtc-tls.path` nad `tls-request.json`
od webu); web jen ověří vstup a zapíše žádost.
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

log = logging.getLogger("mtcontroller.tls")

SSL = Path("/etc/ssl/mtc")
PUBLIC = Path("/etc/ssl/mtc-public")
LE_LIVE = Path("/etc/letsencrypt/live")
SLOTS = ("admin", "checkin")


def device_slot(local: bool) -> str:
    """Slot, jehož certifikát ověřují zařízení (lokální režim: jedna adresa pro obojí)."""
    return "admin" if local else "checkin"
KINDS = ("own_ca", "letsencrypt", "custom")
_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
MAX_PEM = 65536


class TlsError(ValueError):
    pass


def slot_paths(slot: str, ssl_dir: Path = SSL) -> tuple[Path, Path]:
    if slot not in SLOTS:
        raise TlsError("neznámý slot certifikátu")
    return ssl_dir / f"{slot}.crt", ssl_dir / f"{slot}.key"


def _write(path: Path, data: bytes, mode: int) -> None:
    from .firstrun import _write as w

    path.parent.mkdir(parents=True, exist_ok=True)
    w(path, data, mode)


# --- rozbor certifikátu ---------------------------------------------------------------------


def _load_chain(pem: bytes) -> list:
    from cryptography import x509

    try:
        certs = x509.load_pem_x509_certificates(pem)
    except ValueError as e:
        raise TlsError("certifikát není platné PEM") from e
    if not certs:
        raise TlsError("v PEM není certifikát")
    return certs


def inspect(pem: bytes, kind: str | None = None) -> dict:
    """Přehled certifikátu pro UI: jména, vydavatel, platnost, zda je od vlastní CA / self-signed."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    c = _load_chain(pem)[0]
    try:
        sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(x) for x in sans.get_values_for_type(x509.DNSName)] + \
                [str(x) for x in sans.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    cn = c.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer = c.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    org = c.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    now = dt.datetime.now(dt.timezone.utc)
    nb, na = c.not_valid_before_utc, c.not_valid_after_utc
    return {"subject": cn[0].value if cn else "", "names": names,
            "issuer": (issuer[0].value if issuer else "") + (f" ({org[0].value})" if org else ""),
            "not_before": nb.isoformat(), "not_after": na.isoformat(), "days_left": (na - now).days,
            "self_signed": c.issuer == c.subject, "kind": kind}


def covers(info: dict, host: str) -> bool:
    for n in info["names"]:
        if n == host or (n.startswith("*.") and host.count(".") == n.count(".") and host.endswith(n[1:])):
            return True
    return False


def validate_pair(cert_pem: bytes, key_pem: bytes) -> dict:
    """Nahraný certifikát: platné PEM, klíč patří k certifikátu, platí teď."""
    from cryptography.hazmat.primitives import serialization

    if len(cert_pem) > MAX_PEM or len(key_pem) > MAX_PEM:
        raise TlsError("certifikát nebo klíč je příliš velký")
    chain = _load_chain(cert_pem)
    try:
        key = serialization.load_pem_private_key(key_pem, None)
    except (ValueError, TypeError) as e:
        raise TlsError("klíč není platné PEM bez hesla") from e
    pub = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)  # noqa: E731
    if pub(key.public_key()) != pub(chain[0].public_key()):
        raise TlsError("klíč nepatří k certifikátu")
    info = inspect(cert_pem, "custom")
    now = dt.datetime.now(dt.timezone.utc)
    if chain[0].not_valid_after_utc < now:
        raise TlsError("certifikát už vypršel")
    if chain[0].not_valid_before_utc > now + dt.timedelta(hours=1):
        raise TlsError("certifikát ještě neplatí")
    return info


# --- stav a žádosti ----------------------------------------------------------------------------


def marker(public: Path = PUBLIC) -> dict:
    try:
        return json.loads((public / "active.json").read_text())
    except (OSError, ValueError):
        return {}


def _set_marker(public: Path, slot: str, kind: str, extra: dict | None = None) -> None:
    m = marker(public)
    m[slot] = {"kind": kind, "since": dt.datetime.now(dt.timezone.utc).isoformat(), **(extra or {})}
    public.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write(public / "active.json", json.dumps(m, indent=1).encode(), 0o644)


def status(public: Path | None = None) -> dict:
    """Pro UI (čte uživatel webu): veřejné kopie certifikátů slotů a jejich druh."""
    public = public or PUBLIC
    m = marker(public)
    out = {}
    for slot in SLOTS:
        p = public / f"{slot}.crt"
        if not p.is_file():
            continue
        try:
            out[slot] = inspect(p.read_bytes(), (m.get(slot) or {}).get("kind")) | {"meta": m.get(slot) or {}}
        except TlsError as e:
            out[slot] = {"error": str(e)}
    return out


def request_path(data_dir: Path) -> Path:
    return data_dir / "tls-request.json"


def status_path(data_dir: Path) -> Path:
    return data_dir / "tls-status.json"


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def check_request(req: dict, *, local: bool, hosts: dict[str, str]) -> dict:
    """Ověření žádosti (web i root): {action, slots, email | cert, key}."""
    action = req.get("action")
    if action not in ("own_ca", "letsencrypt", "custom"):
        raise TlsError("neznámá akce")
    slots = [s for s in req.get("slots") or [] if s in SLOTS]
    if local:
        slots = ["admin"]
    if not slots:
        raise TlsError("vyber, pro co certifikát je (administrace / check-in)")
    out: dict[str, Any] = {"action": action, "slots": sorted(set(slots))}
    if action == "letsencrypt":
        email = str(req.get("email") or "").strip()
        if not _EMAIL.match(email):
            raise TlsError("zadej e-mail pro Let's Encrypt")
        for s in out["slots"]:
            if _is_ip(hosts[s]):
                raise TlsError(f"Let's Encrypt potřebuje DNS jméno – {s} běží na IP {hosts[s]}")
        out["email"] = email
    devices_see = device_slot(local)
    if action != "own_ca" and devices_see in out["slots"]:
        out["warnings"] = ["zařízení ověřují check-in: s veřejným certifikátem musí mít RouterOS důvěryhodné "
                           "vestavěné kořenové CA pro fetch (7.19+: /certificate/settings builtin-trust-store), jinak se "
                           "přestanou hlásit – CA controlleru jim zůstává, návrat na ni je kdykoli možný"]
    if action == "custom":
        cert, key = str(req.get("cert") or ""), str(req.get("key") or "")
        info = validate_pair(cert.encode(), key.encode())
        missing = [hosts[s] for s in out["slots"] if not covers(info, hosts[s])]
        out["cert"], out["key"] = cert, key
        if missing:
            out.setdefault("warnings", []).append(f"certifikát neobsahuje jméno {', '.join(missing)} – prohlížeč ho odmítne")
    return out


# --- provedení (root) ------------------------------------------------------------------------


def _run(*cmd: str, check: bool = False) -> subprocess.CompletedProcess:
    log.info("spouštím: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=600)


def install(slot: str, cert_pem: bytes, key_pem: bytes, kind: str, *, ssl_dir: Path = SSL,
            public: Path = PUBLIC, extra: dict | None = None) -> None:
    crt, key = slot_paths(slot, ssl_dir)
    ssl_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    _write(key, key_pem, 0o600)
    _write(crt, cert_pem, 0o644)
    public.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write(public / f"{slot}.crt", cert_pem, 0o644)
    _set_marker(public, slot, kind, extra)
    write_hsts(slot, kind, cert_pem, ssl_dir)


def hsts_glob(slot: str, ssl_dir: Path = SSL) -> str:
    """Cesta pro `include` v nginx – se zástupným znakem, aby chybějící soubor nevadil."""
    return str(ssl_dir / f"hsts-{slot}*.conf")


def write_hsts(slot: str, kind: str, cert_pem: bytes, ssl_dir: Path = SSL) -> None:
    """HSTS jen u certifikátu, kterému prohlížeče věří: s CA controlleru (nebo self-signed)
    by HSTS znemožnilo udělit výjimku a do administrace by se nedalo dostat."""
    p = ssl_dir / f"hsts-{slot}.conf"
    trusted = kind == "letsencrypt"
    if kind == "custom":
        try:
            trusted = not inspect(cert_pem)["self_signed"]
        except TlsError:
            trusted = False
    if trusted:
        _write(p, b'add_header Strict-Transport-Security "max-age=31536000" always;\n', 0o644)
    else:
        p.unlink(missing_ok=True)


def own_ca(slot: str, hosts: list[str], *, ssl_dir: Path = SSL, public: Path = PUBLIC,
           device_ca: Path | None = None) -> None:
    """Certifikát z CA controlleru pro slot (CA se použije stávající, ať jí zařízení dál věří)."""
    from .firstrun import DEVICE_CA, ensure_own_ca

    crt, key = ensure_own_ca(hosts, ssl_dir, device_ca or DEVICE_CA, public / "ca.crt")
    install(slot, crt.read_bytes(), key.read_bytes(), "own_ca", ssl_dir=ssl_dir, public=public)


def bootstrap(settings, *, ssl_dir: Path = SSL, public: Path = PUBLIC, live: Path = LE_LIVE,
              device_ca: Path | None = None) -> list[str]:
    """`mtc-admin tls bootstrap` (install.sh, aktualizace): sloty bez certifikátu doplní.
    Starší instalace skriptem měla jeden Let's Encrypt `mtcontroller` pro obě jména – převezme se
    (obnovy dál přes deploy hook); jinak certifikát z CA controlleru. Vrací popis změn."""
    legacy = live / "mtcontroller"
    names = {"admin": [settings.web.ui_host], "checkin": [settings.web.checkin_host]}
    if settings.tunnel.local:
        names = {"admin": [settings.web.ui_host, settings.web.checkin_host]}
    out = []
    for slot, hs in names.items():
        crt, key = slot_paths(slot, ssl_dir)
        if crt.is_file() and key.is_file():
            continue
        try:
            cert, k = (legacy / "fullchain.pem").read_bytes(), (legacy / "privkey.pem").read_bytes()
            if covers(inspect(cert), hs[0]) and inspect(cert)["days_left"] > 0:
                install(slot, cert, k, "letsencrypt", ssl_dir=ssl_dir, public=public,
                        extra={"host": hs[0], "lineage": "mtcontroller"})
                out.append(f"{slot}: převzat Let's Encrypt {legacy}")
                continue
        except (OSError, TlsError):
            pass
        all_names = [n for n in dict.fromkeys(hs + [settings.tunnel.controller_ip]) if n]
        own_ca(slot, all_names, ssl_dir=ssl_dir, public=public, device_ca=device_ca)
        out.append(f"{slot}: certifikát z CA controlleru ({', '.join(all_names)})")
    return out


def letsencrypt(slot: str, host: str, email: str, *, run: Callable = _run, ssl_dir: Path = SSL,
                public: Path = PUBLIC, live: Path = LE_LIVE, webroot: str = "/var/www/html") -> None:
    # obnovu do slotu přenese /etc/letsencrypt/renewal-hooks/deploy/mtcontroller (`mtc-admin tls-sync`)
    name = f"mtc-{slot}"
    r = run("certbot", "certonly", "--webroot", "-w", webroot, "--non-interactive", "--agree-tos",
            "-m", email, "-d", host, "--cert-name", name, "--keep-until-expiring")
    if r.returncode != 0:
        raise TlsError(f"Let's Encrypt pro {host} selhal (DNS musí mířit sem a port 80 být otevřený z internetu): "
                       + (r.stderr or r.stdout or "")[-300:])
    base = live / name
    install(slot, (base / "fullchain.pem").read_bytes(), (base / "privkey.pem").read_bytes(), "letsencrypt",
            ssl_dir=ssl_dir, public=public, extra={"host": host, "email": email, "lineage": name})


def sync(*, ssl_dir: Path = SSL, public: Path = PUBLIC, live: Path = LE_LIVE, run: Callable = _run) -> list[str]:
    """Deploy hook certbotu: obnovené certifikáty LE do slotů a přenačíst nginx."""
    done = []
    for slot, meta in marker(public).items():
        if slot not in SLOTS or (meta or {}).get("kind") != "letsencrypt":
            continue
        base = live / str(meta.get("lineage") or f"mtc-{slot}")
        try:
            cert, key = (base / "fullchain.pem").read_bytes(), (base / "privkey.pem").read_bytes()
        except OSError as e:
            log.warning("LE %s: %s", slot, e)
            continue
        crt_p, _ = slot_paths(slot, ssl_dir)
        if crt_p.is_file() and crt_p.read_bytes() == cert:
            continue
        install(slot, cert, key, "letsencrypt", ssl_dir=ssl_dir, public=public,
                extra={k: v for k, v in meta.items() if k in ("host", "email", "lineage")})
        done.append(slot)
    if done:
        reload_nginx(run)
    return done


def reload_nginx(run: Callable = _run) -> None:
    t = run("nginx", "-t")
    if t.returncode != 0:
        raise TlsError("nginx -t: " + (t.stderr or "")[-300:])
    run("systemctl", "reload", "nginx")


def apply(req: dict, *, local: bool, hosts: dict[str, str], ip: str, ssl_dir: Path = SSL, public: Path = PUBLIC,
          run: Callable = _run, live: Path = LE_LIVE, device_ca: Path | None = None) -> dict:
    """Provede ověřenou žádost. Při chybě nginx -t vrátí předchozí certifikáty slotů."""
    r = check_request(req, local=local, hosts=hosts)
    backup = {}
    for slot in r["slots"]:
        crt, key = slot_paths(slot, ssl_dir)
        if crt.is_file() and key.is_file():
            backup[slot] = (crt.read_bytes(), key.read_bytes(), (marker(public).get(slot) or {}).get("kind") or "own_ca")
    try:
        for slot in r["slots"]:
            if r["action"] == "own_ca":
                names = [hosts[slot]] + ([hosts["checkin"]] if local else []) + [ip]
                own_ca(slot, [n for n in dict.fromkeys(names) if n], ssl_dir=ssl_dir, public=public, device_ca=device_ca)
            elif r["action"] == "letsencrypt":
                letsencrypt(slot, hosts[slot], r["email"], run=run, ssl_dir=ssl_dir, public=public, live=live)
            else:
                install(slot, r["cert"].encode(), r["key"].encode(), "custom", ssl_dir=ssl_dir, public=public)
        reload_nginx(run)
    except Exception:
        for slot, (c, k, kind) in backup.items():
            install(slot, c, k, kind, ssl_dir=ssl_dir, public=public)
        try:
            reload_nginx(run)
        except TlsError:
            pass
        raise
    return {"action": r["action"], "slots": r["slots"], "warnings": r.get("warnings") or []}


def apply_request(settings, *, ssl_dir: Path = SSL, public: Path = PUBLIC, run: Callable = _run,
                  live: Path = LE_LIVE) -> int:
    """`mtc-admin tls-apply` (root, mtc-tls.path): žádost z webu → provedení → stav pro web."""
    data_dir = settings.paths.data_dir
    req_p = request_path(data_dir)
    if not req_p.exists():
        return 0
    raw = req_p.read_bytes()
    req_p.unlink()
    who = None
    try:
        req = json.loads(raw)
        who = req.get("user")
        res = apply(req, local=settings.tunnel.local, hosts=hosts_of(settings), ip=settings.tunnel.controller_ip,
                    ssl_dir=ssl_dir, public=public, run=run, live=live, device_ca=settings.security.device_ca_file)
        _status(data_dir, state="done", **res)
        _audit(settings, "system.tls", {"action": res["action"], "slots": res["slots"], "by": who})
        return 0
    except Exception as e:  # noqa: BLE001 – stav musí dostat web
        log.exception("certifikát se nepodařilo nastavit")
        _status(data_dir, state="error", error=str(e)[:500])
        _audit(settings, "system.tls_failed", {"error": str(e)[:300], "by": who})
        return 1


def hosts_of(settings) -> dict[str, str]:
    return {"admin": settings.web.ui_host, "checkin": settings.web.checkin_host}


def _status(data_dir: Path, **data) -> None:
    from .sysbackup import _put

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


def _audit(settings, action: str, after: dict) -> None:
    try:
        from .db import get_db, operator_scope
        from .services import audit

        with operator_scope(get_db()) as tx:
            audit(tx, action, target="system:tls", after=after)
    except Exception as e:  # noqa: BLE001 – audit nesmí shodit výměnu certifikátu
        log.warning("audit certifikátu se nezapsal: %s", e)


def write_request(data_dir: Path, req: dict) -> None:
    """Web (uživatel mtcontroller): žádost pro root. Soubor jen pro vlastníka (může nést klíč)."""
    p = request_path(data_dir)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(req, fh)
