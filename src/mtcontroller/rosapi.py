"""Klient REST API RouterOS v7 s pinningem certifikátu.

RouterOS zařízení a koncentrátor mají vlastní (self-signed) certifikáty pro
www-ssl. Místo důvěry v CA ověřujeme přesný SHA-256 otisk certifikátu:
- koncentrátor: otisk je v konfiguraci (tunnel.concentrators[].api_fingerprint),
- zařízení: otisk nahlásí samo přes ověřený check-in během provisioningu.
Spojení s jiným certifikátem se ukončí dřív, než se pošlou přihlašovací údaje.

Jen stdlib (http.client + ssl), bez dalších závislostí.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import socket
import ssl
from typing import Any
from urllib.parse import quote, urlencode, urlsplit


class RosApiError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"RouterOS API {status}: {message}")
        self.status = status


class PinMismatch(RosApiError):
    pass


def normalize_fp(fp: str) -> str:
    return fp.replace(":", "").strip().lower()


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, fingerprint: str, timeout: float) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # Řetěz ani hostname neověřujeme – ověřujeme přímo otisk certifikátu.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, context=ctx, timeout=timeout)
        self._fp = normalize_fp(fingerprint)

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        got = hashlib.sha256(der or b"").hexdigest()
        if not hmac.compare_digest(got, self._fp):
            self.sock.close()
            raise PinMismatch(None, f"otisk certifikátu nesouhlasí: očekáván {self._fp}, server má {got}")


class RosApi:
    def __init__(self, base_url: str, user: str, password: str, fingerprint: str,
                 timeout: float = 10.0) -> None:
        u = urlsplit(base_url)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("base_url musí být https://host[:port]")
        self.host, self.port = u.hostname, u.port or 443
        self.fingerprint = fingerprint
        self.timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    def request(self, method: str, path: str, body: Any = None,
                params: dict[str, str] | None = None) -> Any:
        url = "/rest" + quote(path, safe="/*-._~")
        if params:
            url += "?" + urlencode(params)
        conn = _PinnedConnection(self.host, self.port, self.fingerprint, self.timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Authorization": self._auth, "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, url, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, socket.timeout, http.client.HTTPException) as e:
            if isinstance(e, RosApiError):
                raise
            raise RosApiError(None, f"{type(e).__name__}: {e}") from e
        finally:
            conn.close()
        if resp.status >= 400:
            msg = raw.decode(errors="replace")[:300]
            try:
                j = json.loads(raw)
                msg = j.get("detail") or j.get("message") or msg
            except ValueError:
                pass
            raise RosApiError(resp.status, msg)
        return json.loads(raw) if raw else None

    def get(self, path: str, **params: str) -> Any:
        return self.request("GET", path, params=params or None)

    def put(self, path: str, body: dict) -> Any:
        return self.request("PUT", path, body)

    def patch(self, path: str, body: dict) -> Any:
        return self.request("PATCH", path, body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)
