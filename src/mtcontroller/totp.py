"""TOTP (RFC 6238) pro MFA – SHA-1, 6 číslic, 30 s, kompatibilní s běžnými
aplikacemi (Google Authenticator, Aegis, FreeOTP, Bitwarden)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30


def new_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.upper().replace(" ", "")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp(secret: str, at: float | None = None) -> str:
    return hotp(secret, int((time.time() if at is None else at) // PERIOD))


def verify(secret: str, code: str, at: float | None = None, window: int = 1) -> int | None:
    """Vrátí použitý časový krok (pro ochranu proti opakovanému použití), nebo None."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    step = int((time.time() if at is None else at) // PERIOD)
    for s in range(step - window, step + window + 1):
        if hmac.compare_digest(hotp(secret, s), code):
            return s
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "MikroTik Controller") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")
