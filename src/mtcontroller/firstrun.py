"""První spuštění po instalaci balíčku: webový průvodce a jeho aplikace.

Dvě části s oddělenými právy:

- **průvodce** (`python3 -m mtcontroller.firstrun`, služba `mtc-setup`, uživatel
  `mtcontroller`): webová stránka za nginx, přístup jen s jednorázovým kódem z
  `/etc/mtcontroller/setup-code` (vypíše ho instalace a `mtc-admin setup-code`).
  Nová instalace: založí prvního superadmina, firmu a lokalitu (DB, role aplikace).
  Obnova: přijme nahraný soubor zálohy. Pak zapíše požadavek `setup/request.json` –
  nic privilegovaného sám nedělá.
- **aplikace** (`mtc-admin setup-apply`, root, spouští ji `mtc-setup-apply.path`):
  požadavek znovu ověří (nevěří průvodci) a ve dvou krocích
  1. `install` / `restore`: konfigurace (nebo obnova ze zálohy), certifikáty, nginx (zatím
     nezapnutý), heslo záloh, časová zóna serveru, spuštění služeb controlleru; průvodci
     předá záchranné údaje (`setup/kit.json`),
  2. `finish` (po potvrzení, že si je uživatel uložil): přepne nginx z průvodce na
     controller, smaže kód a záchranné údaje a průvodce vypne.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, field_validator, model_validator

from . import sysbackup
from .config import SiteDefaults, _hostname

log = logging.getLogger("mtcontroller.firstrun")

ETC = Path("/etc/mtcontroller")
DATA = Path("/var/lib/mtcontroller")
SHARE = Path("/usr/share/mtcontroller")
SSL = Path("/etc/ssl/mtc")
CODE_FILE = ETC / "setup-code"
DEVICE_CA = ETC / "device-ca.crt"
# veřejná kopie CA pro nginx (zařízení si ji stáhnou z http://<checkin>/mtc-ca.crt)
PUBLIC_CA = Path("/etc/ssl/mtc-public/ca.crt")
SETUP_PORT = 8090
_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")


def setup_dir(data: Path = DATA) -> Path:
    return data / "setup"


# --- plán (ověření vstupu – stejné v průvodci i v aplikaci) ----------------------------


class SetupPlan(BaseModel):
    mode: Literal["local", "remote"]
    controller_ip: str
    ui_host: str
    checkin_host: str
    admin_nets: list[str]
    certs: Literal["own_ca", "letsencrypt"] = "own_ca"
    acme_email: str = ""
    # region: výchozí hodnoty pro lokality a výchozí konfiguraci zařízení
    timezone: str = "Europe/Prague"
    country: str = "Czech Republic"
    ntp: list[str] = ["tik.cesnet.cz", "tak.cesnet.cz"]

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return SiteDefaults(timezone=v).timezone

    @field_validator("country")
    @classmethod
    def _country(cls, v: str) -> str:
        return SiteDefaults(country=v.strip()).country

    @field_validator("ntp")
    @classmethod
    def _ntp(cls, v: list[str]) -> list[str]:
        return SiteDefaults(ntp=[h.strip() for h in v if h.strip()]).ntp

    @field_validator("controller_ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        a = ipaddress.ip_address(v.strip())
        if a.version != 4 or a.is_loopback or a.is_unspecified or a.is_multicast:
            raise ValueError("adresa controlleru: IPv4 adresa tohoto serveru")
        return str(a)

    @field_validator("ui_host", "checkin_host")
    @classmethod
    def _host(cls, v: str) -> str:
        return _hostname(v)

    @field_validator("admin_nets")
    @classmethod
    def _nets(cls, v: list[str]) -> list[str]:
        return parse_nets(v)

    @field_validator("acme_email")
    @classmethod
    def _mail(cls, v: str) -> str:
        v = v.strip()
        if v and not _EMAIL.match(v):
            raise ValueError("neplatný e-mail pro Let's Encrypt")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "SetupPlan":
        if self.mode == "local" and self.ui_host != self.checkin_host:
            raise ValueError("lokální režim: správa i check-in běží na jedné adrese")
        if self.mode == "remote" and self.ui_host == self.checkin_host:
            raise ValueError("vzdálená správa: admin a check-in musí mít různé hostname")
        if self.certs == "letsencrypt":
            if not self.acme_email:
                raise ValueError("Let's Encrypt potřebuje e-mail")
            for h in {self.ui_host, self.checkin_host}:
                if _is_ip(h):
                    raise ValueError("Let's Encrypt potřebuje DNS jméno, ne IP adresu")
        return self


def parse_nets(v: list[str]) -> list[str]:
    out = []
    for n in v:
        n = str(n).strip()
        if n:
            out.append(str(ipaddress.ip_network(n, strict=False)))
    if not out or len(out) > 16:
        raise ValueError("sítě pro správu: 1–16 rozsahů (CIDR)")
    return out


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def render_config(plan: SetupPlan, device_ca: Path = DEVICE_CA) -> str:
    """config.toml z ověřeného plánu (hodnoty jsou jen hostname/IP, ale stejně v uvozovkách JSON)."""
    j = json.dumps
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Vytvořil průvodce prvního spuštění {now}. Úpravy: viz /usr/share/mtcontroller/config.toml.example",
         "", "[web]", f"ui_host = {j(plan.ui_host)}", f"checkin_host = {j(plan.checkin_host)}",
         "", "[security]", 'device_check_certificate = "yes-without-crl"']
    # CA controlleru existuje vždy (první certifikát nginx je od ní); zařízení si ji stáhnou
    # a ponechají jen při shodě otisku. S Let's Encrypt na check-inu nevadí – navíc věří i jí.
    L.append(f"device_ca_file = {j(str(device_ca))}")
    L += ["", "# výchozí hodnoty pro nové lokality a výchozí konfiguraci zařízení", "[site_defaults]",
          f"timezone = {j(plan.timezone)}", f"country = {j(plan.country)}", f"ntp = {j(plan.ntp)}",
          "", "# denní šifrované zálohy (mtc-backup.timer); heslo je v passphrase_file (jen root)", "[backup]",
          'dir = "/var/backups/mtcontroller"', "keep = 14"]
    L += ["", "[tunnel]"]
    if plan.mode == "local":
        L += ["# lokální režim: zařízení se spravují přímo v LAN, bez tunelů a koncentrátorů",
              'provider = "none"', f"controller_ip = {j(plan.controller_ip)}"]
    else:
        L += ['provider = "mikrotik"', 'pool = "10.200.0.0/16"', f"controller_ip = {j(plan.controller_ip)}",
              "", "# DOPLŇ koncentrátor(y) – viz config.toml.example a `mtc-admin concentrator-script`",
              "# [[tunnel.concentrators]]", '# name = "vpn1"', '# connect_host = "vpn1.example.cz"',
              '# api_url = "https://10.10.0.1:8443"', '# api_password_file = "/etc/mtcontroller/vpn1.password"',
              '# api_fingerprint = "..."']
    return "\n".join(L) + "\n"


# --- certifikáty (vlastní CA) ---------------------------------------------------------


def make_ca(cn: str = "mtc controller CA"):
    """CA controlleru (RSA 2048 – RouterOS fetch zvládne spolehlivě)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_server_cert(ca_key, ca_cert, hosts: list[str]):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans: list = []
    for h in dict.fromkeys(hosts):
        sans.append(x509.IPAddress(ipaddress.ip_address(h)) if _is_ip(h) else x509.DNSName(h))
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _pem_cert(cert) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return cert.public_bytes(serialization.Encoding.PEM)


def _pem_key(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _write(path: Path, data: bytes | str, mode: int, owner: tuple[str, str] | None = None) -> None:
    """Atomický zápis s právy nastavenými dřív, než tam jsou data."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data.encode() if isinstance(data, str) else data)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    if owner:
        import shutil
        shutil.chown(tmp, *owner)
    os.replace(tmp, path)


def ensure_own_ca(hosts: list[str], ssl_dir: Path = SSL, device_ca: Path = DEVICE_CA,
                  public_ca: Path = PUBLIC_CA) -> tuple[Path, Path]:
    """CA (klíč jen root) a certifikát serveru pro nginx; existující CA se použije znovu,
    aby zařízení, která ji už mají, nepřestala věřit."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    ssl_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    ca_key_p, ca_crt_p = ssl_dir / "ca.key", ssl_dir / "ca.crt"
    if ca_key_p.exists() and ca_crt_p.exists():
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
        ca_cert = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
    else:
        ca_key, ca_cert = make_ca()
        _write(ca_key_p, _pem_key(ca_key), 0o600)
        _write(ca_crt_p, _pem_cert(ca_cert), 0o644)
    key, cert = make_server_cert(ca_key, ca_cert, hosts)
    crt_p, key_p = ssl_dir / "server.crt", ssl_dir / "server.key"
    _write(key_p, _pem_key(key), 0o600)
    # nginx potřebuje řetěz: server + CA
    _write(crt_p, _pem_cert(cert) + _pem_cert(ca_cert), 0o644)
    _write(device_ca, _pem_cert(ca_cert), 0o644)
    public_ca.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write(public_ca, _pem_cert(ca_cert), 0o644)
    return crt_p, key_p


# --- nginx ------------------------------------------------------------------------------


def render_nginx(plan: SetupPlan, ssl_dir: Path = SSL, share: Path = SHARE) -> str:
    """nginx ukazuje na stálé soubory slotů (tlscfg) – výměna certifikátu nemění konfiguraci."""
    from . import tlscfg

    allow = "\n".join(f"    allow {n};" for n in plan.admin_nets)
    acrt, akey = (str(p) for p in tlscfg.slot_paths("admin", ssl_dir))
    if plan.mode == "local":
        tpl = (share / "nginx" / "mtcontroller-local.conf").read_text()
        out = tpl.replace("mtc.example.lan", plan.ui_host)
        out = out.replace("__CERT__", acrt).replace("__KEY__", akey)
    else:
        ccrt, ckey = (str(p) for p in tlscfg.slot_paths("checkin", ssl_dir))
        tpl = (share / "nginx" / "mtcontroller.conf").read_text()
        out = (tpl.replace("mtc-admin.example.cz", plan.ui_host).replace("mtc-checkin.example.cz", plan.checkin_host)
               .replace("__ADMIN_CERT__", acrt).replace("__ADMIN_KEY__", akey)
               .replace("__CHECKIN_CERT__", ccrt).replace("__CHECKIN_KEY__", ckey))
    out = re.sub(r"^\s*allow [0-9a-fA-F.:/]+;\s*# mtc-admin-net\s*$", allow + "  # mtc-admin-net", out, count=1,
                 flags=re.M)
    return out


# --- aplikace požadavku (root) -------------------------------------------------------


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    log.info("spouštím: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=600)


def _owned(path: Path, data: bytes | str, mode: int, user: str = "mtcontroller", group: str = "mtcontroller") -> None:
    try:
        _write(path, data, mode, (user, group))
    except (LookupError, PermissionError):  # testy bez uživatele mtcontroller
        _write(path, data, mode)


def _status(sd: Path, state: str, detail: str = "", **extra) -> None:
    _owned(sd / "status.json", json.dumps({"state": state, "detail": detail, "at": int(time.time()), **extra}), 0o640)


@dataclass
class Env:
    """Cesty a akce pro aplikaci průvodce (v testech podvržené)."""
    etc: Path = ETC
    data: Path = DATA
    share: Path = SHARE
    ssl: Path = SSL
    le_live: Path = Path("/etc/letsencrypt/live")
    nginx: Path = Path("/etc/nginx")
    run: Callable[..., Any] = _run
    # obnova ze zálohy: (soubor, heslo) -> výsledek sysbackup.restore
    restore: Callable[[Path, str], dict] | None = None
    # první záloha po instalaci (chyba instalaci nezastaví)
    backup: Callable[[], Any] | None = None
    current_ip: Callable[[], str] = lambda: default_ip()  # noqa: E731


def _restore_certs(s, form: dict) -> str:
    """Let's Encrypt po obnově jen když ho chce průvodce (e-mail) a jména nejsou IP."""
    if not form.get("acme_email"):
        return "own_ca"
    names = [s.web.ui_host] + ([] if s.tunnel.local else [s.web.checkin_host])
    return "own_ca" if any(_is_ip(n) for n in names) else "letsencrypt"


def _plan_from_config(etc: Path, form: dict) -> SetupPlan:
    """Plán pro nginx po obnově: z uloženého plánu průvodce, jinak z obnovené konfigurace
    (zálohy ze starší instalace skriptem) a sítí správy zadaných v průvodci."""
    saved = etc / "setup-plan.json"
    if saved.is_file():
        return SetupPlan.model_validate_json(saved.read_text())
    from .config import load_settings

    s = load_settings(etc / "config.toml", environ={})
    return SetupPlan.model_validate({
        "mode": "local" if s.tunnel.local else "remote", "controller_ip": s.tunnel.controller_ip,
        "ui_host": s.web.ui_host, "checkin_host": s.web.checkin_host,
        "admin_nets": form.get("admin_nets") or [], "certs": _restore_certs(s, form),
        "acme_email": form.get("acme_email") or "", "timezone": s.site_defaults.timezone,
        "country": s.site_defaults.country, "ntp": s.site_defaults.ntp})


def slot_hosts(plan: SetupPlan) -> dict[str, list[str]]:
    """Jména do certifikátů z CA controlleru podle slotů (IP controlleru vždy – přístup bez DNS)."""
    uniq = lambda *a: [x for x in dict.fromkeys(a) if x]  # noqa: E731
    if plan.mode == "local":
        return {"admin": uniq(plan.ui_host, plan.checkin_host, plan.controller_ip)}
    return {"admin": uniq(plan.ui_host, plan.controller_ip), "checkin": uniq(plan.checkin_host, plan.controller_ip)}


def _certs(plan: SetupPlan, env: Env) -> list[str]:
    """Certifikáty slotů. Vždy nejdřív z CA controlleru (nginx naběhne i bez internetu a DNS),
    Let's Encrypt jen nad tím: když selže, zůstane vlastní a vrátí se varování – instalace pokračuje."""
    from . import tlscfg

    public = env.ssl.parent / "mtc-public"
    hosts = slot_hosts(plan)
    for slot, names in hosts.items():
        tlscfg.own_ca(slot, names, ssl_dir=env.ssl, public=public, device_ca=env.etc / DEVICE_CA.name)
    if plan.certs != "letsencrypt":
        return []
    warnings = []
    for slot in hosts:
        host = plan.ui_host if slot == "admin" else plan.checkin_host
        try:
            tlscfg.letsencrypt(slot, host, plan.acme_email, run=lambda *c: env.run(*c, check=False),
                               ssl_dir=env.ssl, public=public, live=env.le_live)
        except (tlscfg.TlsError, OSError) as e:
            log.warning("Let's Encrypt %s: %s", host, e)
            warnings.append(f"Let's Encrypt pro {host} se nepodařil – běží certifikát od CA controlleru "
                            "(prohlížeč ho bez importu CA označí za nedůvěryhodný). Nastav ho později "
                            "v administraci: Systém → Certifikát HTTPS.")
    return warnings


def kit(plan, passphrase: str, admin: str = "", restored: dict | None = None) -> dict:
    """Záchranné údaje – ukážou se jednou na konci průvodce (a `mtc-admin recovery-kit`)."""
    return {
        "url": f"https://{plan.ui_host}/", "mode": plan.mode, "controller_ip": plan.controller_ip,
        "admin": admin, "backup_passphrase": passphrase,
        "backup_dir": "/var/backups/mtcontroller",
        "db": {"name": "mtcontroller", "roles": "mtc_owner (schéma, migrace), mtc_app (aplikace, s RLS)",
               "auth": "bez hesel – jen lokálně přes unix socket (peer: root → mtc_owner, mtcontroller → mtc_app)"},
        "files": {"config": "/etc/mtcontroller/config.toml", "master_key": "/etc/mtcontroller/master.key",
                  "backup_key": "/etc/mtcontroller/backup.key"},
        "restored": restored or None,
    }


def kit_text(k: dict) -> str:
    """Záchranné údaje jako text k uložení (správce hesel, tisk)."""
    L = ["MikroTik Controller – záchranné údaje",
         "=" * 40,
         f"Adresa:           {k['url']}",
         f"Režim:            {'lokální' if k['mode'] == 'local' else 'vzdálená správa'} (controller {k['controller_ip']})"]
    if k.get("admin"):
        L.append(f"Administrátor:    {k['admin']} (MFA se nastaví při prvním přihlášení)")
    L += ["",
          "HESLO ZÁLOH (bez něj zálohu nikdo neobnoví – ani ty):",
          f"    {k['backup_passphrase']}",
          f"Zálohy:           {k['backup_dir']} (denně, šifrované; zkopíruj je i mimo server)",
          "",
          f"Databáze:         {k['db']['name']} – role {k['db']['roles']}",
          f"Přihlášení do DB: {k['db']['auth']}",
          f"Konfigurace:      {k['files']['config']}",
          f"Hlavní klíč:      {k['files']['master_key']} (je i v každé záloze)",
          "",
          *([f"Obnoveno ze zálohy {k['restored'].get('from')} (verze {k['restored'].get('version')})", ""]
            if k.get("restored") else []),
          *([f"POZOR: {k['warning']}", ""] if k.get("warning") else []),
          "Obnova: nainstaluj balíček na čistý server, v průvodci zvol „Obnovit ze zálohy“,",
          "nahraj soubor .mtcbak a zadej heslo záloh. Nebo: sudo mtc-admin restore SOUBOR"]
    return "\n".join(L) + "\n"


def _install(req: dict, env: Env, sd: Path) -> None:
    plan = SetupPlan.model_validate(req.get("plan") or {})
    _status(sd, "applying", "Certifikáty a konfigurace", step=1)
    warn = " ".join(_certs(plan, env))
    _owned(env.etc / "config.toml", render_config(plan, env.etc / DEVICE_CA.name), 0o640, "root")
    _owned(env.etc / "setup-plan.json", plan.model_dump_json(indent=1), 0o640, "root")
    passphrase = sysbackup.new_passphrase()
    _write(env.etc / "backup.key", passphrase + "\n", 0o600)
    _finish_common(plan, env, sd)
    if env.backup:
        _status(sd, "applying", "První záloha", step=4)
        try:
            env.backup()
        except Exception as e:  # noqa: BLE001 – instalace kvůli záloze neselže
            log.warning("první záloha selhala: %s", e)
    _owned(sd / "kit.json", json.dumps(kit(plan, passphrase, str(req.get("admin") or "")) | {"warning": warn}),
           0o640, "root")
    _status(sd, "ready", "Hotovo – ulož si záchranné údaje", step=5, warning=warn)


def _restore(req: dict, env: Env, sd: Path) -> None:
    upload = sd / "upload.mtcbak"
    passphrase = str((req.get("restore") or {}).get("passphrase") or "")
    if not upload.is_file():
        raise RuntimeError("soubor zálohy nebyl nahrán")
    if not env.restore:
        raise RuntimeError("obnova není k dispozici")
    _status(sd, "applying", "Obnova databáze a konfigurace ze zálohy", step=1)
    try:
        res = env.restore(upload, passphrase)
    finally:
        upload.unlink(missing_ok=True)
    plan = _plan_from_config(env.etc, req.get("form") or {})
    _status(sd, "applying", "Certifikáty", step=2)
    cert_warn = _certs(plan, env)
    _owned(env.etc / "setup-plan.json", plan.model_dump_json(indent=1), 0o640, "root")
    _write(env.etc / "backup.key", passphrase + "\n", 0o600)
    _finish_common(plan, env, sd)
    man = res.get("manifest") or {}
    restored = {"from": man.get("created_at"), "version": man.get("app_version"), "host": man.get("hostname"),
                "migrated": res.get("migrated") or []}
    here = env.current_ip()
    warn = " ".join(cert_warn)
    if here and here != plan.controller_ip:
        warn += (" " if warn else "") + (f"Záloha počítá s adresou controlleru {plan.controller_ip}, server má teď {here}. "
                "Zařízení se hlásí na původní adresu – nastav serveru původní IP.")
    _owned(sd / "kit.json", json.dumps(kit(plan, passphrase, restored=restored) | {"warning": warn}), 0o640, "root")
    _status(sd, "ready", "Obnoveno – zkontroluj záchranné údaje", step=5, warning=warn)


def _finish_common(plan: SetupPlan, env: Env, sd: Path) -> None:
    """nginx připravit (zapne se až při finish), časová zóna serveru, služby controlleru."""
    _status(sd, "applying", "Webový server a služby", step=3)
    avail = env.nginx / "sites-available"
    avail.mkdir(parents=True, exist_ok=True)
    _write(avail / "mtcontroller.conf", render_nginx(plan, env.ssl, env.share), 0o644)
    env.run("timedatectl", "set-timezone", plan.timezone, check=False)
    env.run("systemctl", "enable", "--now", "mtc-web", "mtc-worker", "mtc-syslog", "mtc-backup.timer",
            "mtc-backup.path", "mtc-tls.path")


def _finish(env: Env, sd: Path) -> None:
    if not (env.etc / "config.toml").exists():
        raise RuntimeError("nejdřív dokonči nastavení")
    enabled = env.nginx / "sites-enabled"
    enabled.mkdir(parents=True, exist_ok=True)
    for name in ("mtcontroller-setup.conf", "default"):
        (enabled / name).unlink(missing_ok=True)
    link = enabled / "mtcontroller.conf"
    link.unlink(missing_ok=True)
    link.symlink_to(env.nginx / "sites-available" / "mtcontroller.conf")
    t = env.run("nginx", "-t", check=False)
    if t.returncode != 0:
        # zpět na průvodce, ať se dá opravit
        link.unlink(missing_ok=True)
        (enabled / "mtcontroller-setup.conf").symlink_to(env.nginx / "sites-available" / "mtcontroller-setup.conf")
        raise RuntimeError("nginx -t: " + (t.stderr or "")[-400:])
    (sd / "kit.json").unlink(missing_ok=True)
    (env.etc / CODE_FILE.name).unlink(missing_ok=True)
    try:
        url = "https://" + SetupPlan.model_validate_json((env.etc / "setup-plan.json").read_text()).ui_host + "/"
    except (OSError, ValueError):
        url = ""
    _status(sd, "done", "Přepnuto na controller", url=url)
    env.run("systemctl", "reload", "nginx")
    env.run("systemctl", "disable", "--now", "mtc-setup.service", "mtc-setup-apply.path", check=False)


def upgrade_nginx(env: Env | None = None) -> bool:
    """Aktualizace balíčku: nginx ze starší verze průvodce (certifikát mimo sloty) přegenerovat
    na sloty /etc/ssl/mtc/{admin,checkin}. Při chybě nginx -t vrátí původní. Vrací, zda změnil."""
    from . import tlscfg

    env = env or Env()
    conf = env.nginx / "sites-available" / "mtcontroller.conf"
    saved = env.etc / "setup-plan.json"
    if not conf.is_file() or not saved.is_file():
        return False
    old = conf.read_text()
    if str(tlscfg.slot_paths("admin", env.ssl)[0]) in old:
        return False
    plan = SetupPlan.model_validate_json(saved.read_text())
    _write(conf, render_nginx(plan, env.ssl, env.share), 0o644)
    if env.run("nginx", "-t", check=False).returncode != 0:
        _write(conf, old, 0o644)
        return False
    env.run("systemctl", "reload", "nginx", check=False)
    return True


def apply_request(env: Env | None = None) -> int:
    """Provede požadavek průvodce (root). Návratový kód pro systemd."""
    env = env or _default_env()
    sd = setup_dir(env.data)
    req_p = sd / "request.json"
    if not req_p.exists():
        return 0
    try:
        raw = req_p.read_bytes()
        req_p.unlink()  # jen jednou; při chybě průvodce pošle nový
        if len(raw) > 16384:
            raise ValueError("požadavek je příliš velký")
        req = json.loads(raw)
        action = req.get("action") if isinstance(req, dict) else None
        if action == "finish":
            _finish(env, sd)
            return 0
        if (env.etc / "config.toml").exists():
            raise RuntimeError("controller je už nastavený (config.toml existuje) – průvodce se znovu nepoužije")
        if action == "install":
            _install(req, env, sd)
        elif action == "restore":
            _restore(req, env, sd)
        else:
            raise ValueError("neznámá akce průvodce")
        return 0
    except Exception as e:  # noqa: BLE001 – stav musí dostat průvodce
        log.exception("aplikace průvodce selhala")
        _status(sd, "error", _msg(e))
        return 1


def _default_env() -> Env:
    from . import migrate
    from .config import load_settings
    from .db import PsycopgDatabase

    def restore(path: Path, passphrase: str) -> dict:
        s = load_settings(ETC / "config.toml", environ={})
        return sysbackup.restore(
            path, passphrase, s, recreate_db=lambda: sysbackup.recreate_db_as_postgres(sysbackup.dbname(s.database.migrate_dsn)),
            migrate_db=lambda: migrate.upgrade(PsycopgDatabase(load_settings(ETC / "config.toml", environ={}).database.migrate_dsn),
                                               s.database.app_role))

    def backup() -> Any:
        s = load_settings(ETC / "config.toml", environ={})
        f = sysbackup.create(s, PsycopgDatabase(s.database.migrate_dsn), reason="install")
        sysbackup.write_status(s.paths.data_dir, state="done", file=f.name, size=f.stat().st_size, reason="install")
        return f

    return Env(restore=restore, backup=backup)


# --- průvodce (web, uživatel mtcontroller) ---------------------------------------------


# země pro regulaci Wi-Fi: (ISO, česky, jméno v RouterOS, výchozí NTP)
COUNTRIES = [
    ("CZ", "Česko", "Czech Republic", ["tik.cesnet.cz", "tak.cesnet.cz"]),
    ("SK", "Slovensko", "Slovakia", ["sk.pool.ntp.org"]),
    ("DE", "Německo", "Germany", ["ptbtime1.ptb.de", "ptbtime2.ptb.de"]),
    ("AT", "Rakousko", "Austria", ["at.pool.ntp.org"]),
    ("PL", "Polsko", "Poland", ["tempus1.gum.gov.pl", "tempus2.gum.gov.pl"]),
    ("HU", "Maďarsko", "Hungary", ["hu.pool.ntp.org"]),
    ("SI", "Slovinsko", "Slovenia", ["si.pool.ntp.org"]),
    ("HR", "Chorvatsko", "Croatia", ["hr.pool.ntp.org"]),
    ("RO", "Rumunsko", "Romania", ["ro.pool.ntp.org"]),
    ("BG", "Bulharsko", "Bulgaria", ["bg.pool.ntp.org"]),
    ("UA", "Ukrajina", "Ukraine", ["ua.pool.ntp.org"]),
    ("GB", "Spojené království", "United Kingdom", ["uk.pool.ntp.org"]),
    ("IE", "Irsko", "Ireland", ["ie.pool.ntp.org"]),
    ("NL", "Nizozemsko", "Netherlands", ["nl.pool.ntp.org"]),
    ("BE", "Belgie", "Belgium", ["be.pool.ntp.org"]),
    ("LU", "Lucembursko", "Luxembourg", ["lu.pool.ntp.org"]),
    ("FR", "Francie", "France", ["fr.pool.ntp.org"]),
    ("CH", "Švýcarsko", "Switzerland", ["ch.pool.ntp.org"]),
    ("IT", "Itálie", "Italy", ["it.pool.ntp.org"]),
    ("ES", "Španělsko", "Spain", ["es.pool.ntp.org"]),
    ("PT", "Portugalsko", "Portugal", ["pt.pool.ntp.org"]),
    ("GR", "Řecko", "Greece", ["gr.pool.ntp.org"]),
    ("DK", "Dánsko", "Denmark", ["dk.pool.ntp.org"]),
    ("SE", "Švédsko", "Sweden", ["se.pool.ntp.org"]),
    ("NO", "Norsko", "Norway", ["no.pool.ntp.org"]),
    ("FI", "Finsko", "Finland", ["fi.pool.ntp.org"]),
    ("EE", "Estonsko", "Estonia", ["ee.pool.ntp.org"]),
    ("LV", "Lotyšsko", "Latvia", ["lv.pool.ntp.org"]),
    ("LT", "Litva", "Lithuania", ["lt.pool.ntp.org"]),
    ("US", "USA", "United States", ["us.pool.ntp.org"]),
    ("CA", "Kanada", "Canada", ["ca.pool.ntp.org"]),
    ("AU", "Austrálie", "Australia", ["au.pool.ntp.org"]),
]


def timezones() -> list[str]:
    try:
        import zoneinfo
        return sorted(z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
    except Exception:  # noqa: BLE001
        return ["Europe/Prague"]


def system_timezone() -> str:
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "Europe/Prague"


def default_ip() -> str:
    """IPv4 adresa, přes kterou jde výchozí routa (bez odeslání paketu)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def guess_nets(ip: str) -> list[str]:
    """Výchozí síť pro správu: /24 kolem adresy serveru (u lokálního režimu typicky LAN)."""
    try:
        return [str(ipaddress.ip_network(f"{ip}/24", strict=False))]
    except ValueError:
        return []


def new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bez 0/O a 1/I
    c = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{c[:4]}-{c[4:8]}-{c[8:]}"


class _Limiter:
    def __init__(self, per_minute: int = 10) -> None:
        self.per_minute = per_minute
        self.fails: list[float] = []

    def blocked(self) -> bool:
        now = time.monotonic()
        self.fails = [t for t in self.fails if now - t < 60]
        return len(self.fails) >= self.per_minute

    def fail(self) -> None:
        self.fails.append(time.monotonic())


def create_admin(db, settings, email: str, password: str, tenant: str, site: str, ip: str | None) -> dict:
    """Superadmin + první firma a lokalita – jen dokud žádný superadmin není."""
    from . import auth
    from .db import operator_scope
    from .services import ServiceError, audit, create_site, create_tenant

    email = email.strip().lower()
    if not _EMAIL.match(email):
        raise ServiceError("neplatný e-mail administrátora")
    if len(password) < 12:
        raise ServiceError("heslo musí mít alespoň 12 znaků")
    with operator_scope(db) as tx:
        if tx.fetchone("SELECT 1 FROM role_binding WHERE role = 'superadmin'"):
            return {"created": False}
        uid = auth.create_user(tx, email, password)
        tx.execute("INSERT INTO role_binding (user_id, role) VALUES (%(u)s, 'superadmin')", {"u": uid})
        audit(tx, "user.create_superadmin", user_id=uid, target=f"user:{uid}", after={"email": email, "via": "setup"}, ip=ip)
    out = {"created": True}
    if tenant.strip():
        t = create_tenant(db, settings, tenant, user_id=uid, ip=ip)
        out["tenant_id"] = str(t["id"])
        if site.strip():
            out["site_id"] = str(create_site(db, t["id"], site, user_id=uid, ip=ip)["id"])
    return out


MAX_UPLOAD = 8 << 30  # 8 GiB


def create_setup_app(db, settings, *, etc: Path = ETC, data: Path = DATA):
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response
    from starlette.routing import Route

    static = Path(__file__).parent / "web" / "static"
    limiter = _Limiter()
    sd = setup_dir(data)

    def _code_ok(request: Request) -> bool:
        if limiter.blocked():
            return False
        given = (request.headers.get("x-setup-code") or "").strip().upper()
        try:
            want = (etc / CODE_FILE.name).read_text().strip().upper()
        except OSError:
            return False
        if want and secrets.compare_digest(given.encode(), want.encode()):
            return True
        limiter.fail()
        return False

    def _ip(request: Request) -> str | None:
        return request.client.host if request.client else None

    def denied() -> Response:
        return JSONResponse({"detail": "neplatný kód průvodce (najdeš ho: sudo mtc-admin setup-code)"}, 403)

    def _busy() -> bool:
        try:
            st = json.loads((sd / "status.json").read_text())
        except (OSError, ValueError):
            return False
        return st.get("state") in ("queued", "applying") and time.time() - st.get("at", 0) < 900

    def _queue(req: dict) -> None:
        sd.mkdir(mode=0o750, parents=True, exist_ok=True)
        _status(sd, "queued", "Čekám na službu nastavení", step=0)
        _write(sd / "request.json", json.dumps(req), 0o640)

    async def page(request: Request) -> Response:
        return FileResponse(static / "setup.html", headers=_HDR)

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ("setup.js", "setup.css"):
            return Response(status_code=404)
        return FileResponse(static / name, headers={"Cache-Control": "no-store"})

    async def info(request: Request) -> Response:
        if not _code_ok(request):
            return denied()
        ip = default_ip()
        return JSONResponse({"ip": ip, "hostname": socket.getfqdn(), "nets": guess_nets(ip),
                             "configured": (etc / "config.toml").exists(), "timezone": system_timezone(),
                             "timezones": timezones(),
                             "countries": [{"iso": i, "label": lbl, "ros": r, "ntp": n} for i, lbl, r, n in COUNTRIES]})

    async def apply(request: Request) -> Response:
        from .services import ServiceError

        if not _code_ok(request):
            return denied()
        if _busy():
            return JSONResponse({"detail": "nastavení už probíhá"}, 409)
        body = await request.body()
        if len(body) > 16384:
            return JSONResponse({"detail": "příliš velký požadavek"}, 413)
        try:
            d = json.loads(body)
            action = d.get("action") or "install"
            if action == "install":
                plan = SetupPlan.model_validate(d.get("plan") or {})
                admin = d.get("admin") or {}
                if str(admin.get("password", "")) != str(admin.get("password2", admin.get("password", ""))):
                    raise ServiceError("hesla se neshodují")
                res = await run_in_threadpool(create_admin, db, settings, str(admin.get("email", "")),
                                              str(admin.get("password", "")), str(d.get("tenant", ""))[:100],
                                              str(d.get("site", ""))[:100], _ip(request))
                _queue({"action": "install", "plan": json.loads(plan.model_dump_json()),
                        "admin": str(admin.get("email", "")).strip().lower()})
                return JSONResponse({"ok": True, "admin": res})
            if action == "restore":
                rest = d.get("restore") or {}
                pw = str(rest.get("passphrase", "")).strip()
                if len(pw) < 12:
                    raise ServiceError("zadej heslo záloh")
                if not (sd / "upload.mtcbak").is_file():
                    raise ServiceError("nejdřív nahraj soubor zálohy")
                form = d.get("form") or {}
                nets = parse_nets(form.get("admin_nets") or []) if form.get("admin_nets") else []
                _queue({"action": "restore", "restore": {"passphrase": pw},
                        "form": {"admin_nets": nets, "acme_email": str(form.get("acme_email", ""))[:200]}})
                return JSONResponse({"ok": True})
            if action == "finish":
                _queue({"action": "finish"})
                return JSONResponse({"ok": True})
            raise ServiceError("neznámá akce")
        except (ValueError, ServiceError) as e:
            return JSONResponse({"detail": _msg(e)}, 400)

    async def upload(request: Request) -> Response:
        if not _code_ok(request):
            return denied()
        if _busy():
            return JSONResponse({"detail": "nastavení už probíhá"}, 409)
        sd.mkdir(mode=0o750, parents=True, exist_ok=True)
        part = sd / "upload.part"
        size, first = 0, b""
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in request.stream():
                    if len(first) < len(sysbackup.MAGIC):
                        first += chunk[: len(sysbackup.MAGIC) - len(first)]
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise ValueError("soubor je větší než 8 GiB")
                    fh.write(chunk)
            if not first.startswith(sysbackup.MAGIC):
                raise ValueError("to není záloha controlleru (.mtcbak)")
        except ValueError as e:
            part.unlink(missing_ok=True)
            return JSONResponse({"detail": str(e)}, 400)
        os.replace(part, sd / "upload.mtcbak")
        return JSONResponse({"ok": True, "size": size})

    async def status(request: Request) -> Response:
        if not _code_ok(request):
            return denied()
        try:
            st = json.loads((sd / "status.json").read_text())
        except (OSError, ValueError):
            st = {"state": "idle"}
        if st.get("state") == "ready":
            try:
                k = json.loads((sd / "kit.json").read_text())
                st["kit"] = k
                st["kit_text"] = kit_text(k)
            except (OSError, ValueError):
                pass
        return JSONResponse(st)

    routes = [Route("/", page), Route("/setup/static/{name}", asset), Route("/setup/api/info", info),
              Route("/setup/api/apply", apply, methods=["POST"]),
              Route("/setup/api/upload", upload, methods=["PUT"]), Route("/setup/api/status", status)]
    return Starlette(routes=routes)


_HDR = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                                   "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}


def _msg(e: Exception) -> str:
    errs = getattr(e, "errors", None)
    if callable(errs):
        try:
            return "; ".join(str(x.get("msg", "")).removeprefix("Value error, ") for x in errs())[:400]
        except Exception:  # noqa: BLE001
            pass
    return str(e)[:400]


def main() -> None:
    import uvicorn

    from .config import get_settings
    from .db import get_db

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run(create_setup_app(get_db(), s), host="127.0.0.1", port=SETUP_PORT, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", server_header=False)


if __name__ == "__main__":
    main()
