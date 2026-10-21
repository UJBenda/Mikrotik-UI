"""První spuštění po instalaci balíčku: webový průvodce a jeho aplikace.

Dvě části s oddělenými právy:

- **průvodce** (`python3 -m mtcontroller.firstrun`, služba `mtc-setup`, uživatel
  `mtcontroller`): webová stránka za nginx, přístup jen s jednorázovým kódem z
  `/etc/mtcontroller/setup-code` (vypíše ho instalace a `mtc-admin setup-code`).
  Založí prvního superadmina, firmu a lokalitu (DB, role aplikace) a zapíše požadavek
  `setup/request.json` – nic privilegovaného sám nedělá.
- **aplikace** (`mtc-admin setup-apply`, root, spouští ji `mtc-setup-apply.path`):
  požadavek znovu ověří (nevěří průvodci), zapíše `config.toml`, certifikáty
  (vlastní CA s otiskem pro zařízení, nebo Let's Encrypt), nginx a přepne služby z
  průvodce na controller.
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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from .config import _hostname

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
        out = []
        for n in v:
            n = n.strip()
            if n:
                out.append(str(ipaddress.ip_network(n, strict=False)))
        if not out or len(out) > 16:
            raise ValueError("sítě pro správu: 1–16 rozsahů (CIDR)")
        return out

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


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def render_config(plan: SetupPlan) -> str:
    """config.toml z ověřeného plánu (hodnoty jsou jen hostname/IP, ale stejně v uvozovkách JSON)."""
    j = json.dumps
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Vytvořil průvodce prvního spuštění {now}. Úpravy: viz /usr/share/mtcontroller/config.toml.example",
         "", "[web]", f"ui_host = {j(plan.ui_host)}", f"checkin_host = {j(plan.checkin_host)}",
         "", "[security]", 'device_check_certificate = "yes-without-crl"']
    if plan.certs == "own_ca":
        L.append("# vlastní CA controlleru: zařízení si ji stáhnou a ponechají jen při shodě otisku")
        L.append(f"device_ca_file = {j(str(DEVICE_CA))}")
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


def render_nginx(plan: SetupPlan, cert: str, key: str, share: Path = SHARE) -> str:
    allow = "\n".join(f"    allow {n};" for n in plan.admin_nets)
    if plan.mode == "local":
        tpl = (share / "nginx" / "mtcontroller-local.conf").read_text()
        out = tpl.replace("mtc.example.lan", plan.ui_host)
        out = out.replace("__CERT__", cert).replace("__KEY__", key)
    else:
        tpl = (share / "nginx" / "mtcontroller.conf").read_text()
        out = (tpl.replace("mtc-admin.example.cz", plan.ui_host).replace("mtc-checkin.example.cz", plan.checkin_host)
               .replace("__ADMIN_CERT__", cert).replace("__ADMIN_KEY__", key)
               .replace("__CHECKIN_CERT__", cert).replace("__CHECKIN_KEY__", key))
    out = re.sub(r"^\s*allow [0-9a-fA-F.:/]+;\s*# mtc-admin-net\s*$", allow + "  # mtc-admin-net", out, count=1,
                 flags=re.M)
    return out


# --- aplikace požadavku (root) -------------------------------------------------------


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    log.info("spouštím: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=300)


def _status(sd: Path, state: str, detail: str = "", **extra) -> None:
    data = {"state": state, "detail": detail, "at": int(time.time()), **extra}
    try:
        _write(sd / "status.json", json.dumps(data), 0o640, ("mtcontroller", "mtcontroller"))
    except (LookupError, PermissionError):  # testy bez uživatele mtcontroller
        _write(sd / "status.json", json.dumps(data), 0o644)


def apply_request(etc: Path = ETC, data: Path = DATA, share: Path = SHARE, ssl_dir: Path = SSL,
                  nginx_dir: Path = Path("/etc/nginx"), run=_run, switch_delay: float = 4.0) -> int:
    """Provede požadavek průvodce (root). Návratový kód pro systemd."""
    sd = setup_dir(data)
    req = sd / "request.json"
    if not req.exists():
        return 0
    try:
        raw = req.read_bytes()
        req.unlink()  # jen jednou; při chybě průvodce pošle nový
        if (etc / "config.toml").exists():
            raise RuntimeError("controller je už nastavený (config.toml existuje) – průvodce se znovu nepoužije")
        if len(raw) > 16384:
            raise ValueError("požadavek je příliš velký")
        plan = SetupPlan.model_validate(json.loads(raw))
        _status(sd, "applying", "zapisuji konfiguraci a certifikáty")
        hosts = [plan.ui_host, plan.checkin_host, plan.controller_ip]
        device_ca = etc / DEVICE_CA.name
        if plan.certs == "own_ca":
            crt, key = ensure_own_ca(hosts, ssl_dir, device_ca, ssl_dir.parent / "mtc-public" / "ca.crt")
        else:
            r = run("certbot", "certonly", "--webroot", "-w", "/var/www/html", "--non-interactive", "--agree-tos",
                    "-m", plan.acme_email, "-d", plan.ui_host, "-d", plan.checkin_host, "--cert-name",
                    "mtcontroller", check=False)
            if r.returncode != 0:
                raise RuntimeError("certbot selhal (DNS obou jmen musí mířit sem, port 80 otevřený): "
                                   + (r.stderr or r.stdout)[-400:])
            crt = Path("/etc/letsencrypt/live/mtcontroller/fullchain.pem")
            key = Path("/etc/letsencrypt/live/mtcontroller/privkey.pem")
        cfg = render_config(plan)
        if plan.certs == "own_ca":
            cfg = cfg.replace(json.dumps(str(DEVICE_CA)), json.dumps(str(device_ca)))
        conf = render_nginx(plan, str(crt), str(key), share)
        avail, enabled = nginx_dir / "sites-available", nginx_dir / "sites-enabled"
        avail.mkdir(parents=True, exist_ok=True)
        enabled.mkdir(parents=True, exist_ok=True)
        _write(avail / "mtcontroller.conf", conf, 0o644)
        try:
            _write(etc / "config.toml", cfg, 0o640, ("root", "mtcontroller"))
        except (LookupError, PermissionError):
            _write(etc / "config.toml", cfg, 0o640)
        _status(sd, "done", "hotovo – přepínám na controller", url=f"https://{plan.ui_host}/")
        time.sleep(switch_delay)  # průvodce si stav ještě přečte, než zmizí
        for name in ("mtcontroller-setup.conf", "default"):
            (enabled / name).unlink(missing_ok=True)
        link = enabled / "mtcontroller.conf"
        link.unlink(missing_ok=True)
        link.symlink_to(avail / "mtcontroller.conf")
        t = run("nginx", "-t", check=False)
        if t.returncode != 0:
            raise RuntimeError("nginx -t: " + (t.stderr or "")[-400:])
        run("systemctl", "enable", "--now", "mtc-web", "mtc-worker", "mtc-syslog")
        run("systemctl", "reload", "nginx")
        (etc / CODE_FILE.name).unlink(missing_ok=True)
        run("systemctl", "disable", "--now", "mtc-setup.service", "mtc-setup-apply.path", check=False)
        return 0
    except Exception as e:  # noqa: BLE001 – stav musí dostat průvodce
        log.exception("aplikace průvodce selhala")
        _status(sd, "error", str(e)[:600])
        return 1


# --- průvodce (web, uživatel mtcontroller) ---------------------------------------------


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

    async def page(request: Request) -> Response:
        return FileResponse(static / "setup.html", headers=_HDR)

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ("setup.js", "style.css"):
            return Response(status_code=404)
        return FileResponse(static / name, headers={"Cache-Control": "no-store"})

    async def info(request: Request) -> Response:
        if not _code_ok(request):
            return denied()
        ip = default_ip()
        return JSONResponse({"ip": ip, "hostname": socket.getfqdn(), "nets": guess_nets(ip),
                             "configured": (etc / "config.toml").exists()})

    async def apply(request: Request) -> Response:
        from .services import ServiceError

        if not _code_ok(request):
            return denied()
        body = await request.body()
        if len(body) > 16384:
            return JSONResponse({"detail": "příliš velký požadavek"}, 413)
        try:
            d = json.loads(body)
            plan = SetupPlan.model_validate(d.get("plan") or {})
            admin = d.get("admin") or {}
            res = await run_in_threadpool(create_admin, db, settings, str(admin.get("email", "")),
                                          str(admin.get("password", "")), str(d.get("tenant", ""))[:100],
                                          str(d.get("site", ""))[:100], _ip(request))
        except (ValueError, ServiceError) as e:
            return JSONResponse({"detail": _msg(e)}, 400)
        sd.mkdir(mode=0o750, parents=True, exist_ok=True)
        _status(sd, "queued", "čekám na aplikaci nastavení")
        _write(sd / "request.json", plan.model_dump_json(), 0o640)
        return JSONResponse({"ok": True, "admin": res})

    async def status(request: Request) -> Response:
        if not _code_ok(request):
            return denied()
        try:
            return JSONResponse(json.loads((sd / "status.json").read_text()))
        except (OSError, ValueError):
            return JSONResponse({"state": "idle"})

    routes = [Route("/", page), Route("/setup/static/{name}", asset), Route("/setup/api/info", info),
              Route("/setup/api/apply", apply, methods=["POST"]), Route("/setup/api/status", status)]
    return Starlette(routes=routes)


_HDR = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
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
