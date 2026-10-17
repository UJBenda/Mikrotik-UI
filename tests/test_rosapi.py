"""REST klient s pinningem a provider koncentrátoru proti skutečnému lokálnímu
HTTPS serveru, který napodobuje REST API RouterOS."""

import datetime as dt
import hashlib
import json
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mtcontroller.rosapi import PinMismatch, RosApi, RosApiError
from mtcontroller.tunnel import TunnelAccount
from mtcontroller.tunnel.mikrotik import MikrotikProvider


def _self_signed(tmp: Path) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mtc-api")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))
    c, k = tmp / "c.pem", tmp / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return c, k, fp


class FakeRouterOS:
    """Napodobenina /rest/ppp/secret a /rest/ppp/active."""

    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.active: dict[str, dict] = {}
        self.auth_seen: list[str] = []
        self.seq = 0


def _server(state: FakeRouterOS, certfile: Path, keyfile: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            raw = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self):
            state.auth_seen.append(self.headers.get("Authorization", ""))
            return self.headers.get("Authorization") == "Basic bXRjLWNvbnRyb2xsZXI6c2VjcmV0"  # mtc-controller:secret

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        def do_GET(self):
            if not self._auth():
                return self._send(401, {"error": 401, "message": "Unauthorized"})
            path, _, qs = self.path.partition("?")
            params = dict(p.split("=", 1) for p in qs.split("&") if p)
            table = state.secrets if path == "/rest/ppp/secret" else state.active if path == "/rest/ppp/active" else None
            if path == "/rest/system/identity":
                return self._send(200, {"name": "vpn1"})
            if table is None:
                return self._send(404, {"error": 404, "message": "Not Found"})
            rows = [r for r in table.values() if all(r.get(k) == v for k, v in params.items())]
            self._send(200, rows)

        def do_PUT(self):
            if not self._auth():
                return self._send(401, {})
            b = self._body()
            state.seq += 1
            b[".id"] = f"*{state.seq:X}"
            state.secrets[b[".id"]] = b
            self._send(201, b)

        def do_PATCH(self):
            if not self._auth():
                return self._send(401, {})
            rid = self.path.rsplit("/", 1)[1]
            if rid not in state.secrets:
                return self._send(404, {"detail": "no such item"})
            state.secrets[rid].update(self._body())
            self._send(200, state.secrets[rid])

        def do_DELETE(self):
            rid = self.path.rsplit("/", 1)[1]
            state.active.pop(rid, None)
            self._send(204)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _setup():
    tmp = Path(tempfile.mkdtemp())
    c, k, fp = _self_signed(tmp)
    state = FakeRouterOS()
    srv = _server(state, c, k)
    url = f"https://127.0.0.1:{srv.server_address[1]}"
    return state, srv, url, fp


def test_pinned_request_and_provider_roundtrip():
    state, srv, url, fp = _setup()
    try:
        api = RosApi(url, "mtc-controller", "secret", fp)
        assert api.get("/system/identity") == {"name": "vpn1"}
        p = MikrotikProvider("vpn1", api)
        acc = TunnelAccount("mtc-SER1", "pw1", "10.200.1.5", "t/s")
        p.ensure_account(acc)
        p.ensure_account(TunnelAccount("mtc-SER1", "pw2", "10.200.1.5", "t/s"))  # idempotentní update
        assert len(state.secrets) == 1
        row = next(iter(state.secrets.values()))
        assert row["password"] == "pw2" and row["profile"] == "mtc-tunnel" and row["service"] == "sstp"
        state.active["*A"] = {".id": "*A", "name": "mtc-SER1", "service": "sstp", "address": "10.200.1.5"}
        assert p.active_sessions() == {"mtc-SER1": "10.200.1.5"}
        p.disable_account("mtc-SER1")
        assert row["disabled"] == "yes" and not state.active
    finally:
        srv.shutdown()


def test_wrong_fingerprint_never_sends_credentials():
    state, srv, url, fp = _setup()
    try:
        api = RosApi(url, "mtc-controller", "secret", "00" * 32)
        with pytest.raises(PinMismatch) as e:
            api.get("/system/identity")
        assert fp in str(e.value)  # chyba ukáže skutečný otisk pro diagnostiku
        assert state.auth_seen == []  # heslo neodešlo
    finally:
        srv.shutdown()


def test_bad_credentials_raise():
    state, srv, url, fp = _setup()
    try:
        with pytest.raises(RosApiError) as e:
            RosApi(url, "mtc-controller", "spatne", fp).get("/ppp/secret")
        assert e.value.status == 401
    finally:
        srv.shutdown()


def test_unreachable_is_rosapierror():
    with pytest.raises(RosApiError):
        RosApi("https://127.0.0.1:1", "u", "p", "00" * 32, timeout=2).get("/system/identity")


def test_keepalive_reuses_connection_and_survives_server_close():
    tmp = Path(tempfile.mkdtemp())
    c, k, fp = _self_signed(tmp)
    state = FakeRouterOS()
    srv = _server(state, c, k)
    srv.RequestHandlerClass.protocol_version = "HTTP/1.1"  # keep-alive
    url = f"https://127.0.0.1:{srv.server_address[1]}"
    try:
        with RosApi(url, "mtc-controller", "secret", fp) as api:
            for _ in range(5):
                assert api.get("/system/identity") == {"name": "vpn1"}
            assert api.connections_opened == 1
            # server spojení zavře – klient otevře nové (s novou kontrolou otisku)
            api._conn.sock.close()
            assert api.get("/system/identity") == {"name": "vpn1"}
            assert api.connections_opened == 2
    finally:
        srv.shutdown()
