"""Kryptografické primitivy controlleru.

- Hesla uživatelů: scrypt (stdlib), formát `scrypt$n$r$p$salt$hash`.
- Tokeny (adopce, check-in): náhodné, v DB jen SHA-256 hash.
- Tajemství zařízení: envelope encryption – každý záznam má vlastní
  datový klíč (AES-256-GCM), ten je zabalený master klíčem.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- hesla -----------------------------------------------------------------

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("heslo musí mít alespoň 12 znaků")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=64 * 1024 * 1024, dklen=32,
    )
    return "$".join([
        "scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        _b64(salt), _b64(digest),
    ])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
        maxmem=64 * 1024 * 1024, dklen=32,
    )
    return hmac.compare_digest(candidate, _unb64(digest))


# --- tokeny ----------------------------------------------------------------


def new_token() -> str:
    """Token pro adopci / check-in (256 bitů entropie)."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Do DB se ukládá jen hash; token má dost entropie, salt není potřeba."""
    return hashlib.sha256(token.encode()).hexdigest()


def sign_checkin(key: bytes, device_id: str, timestamp: int, body: bytes) -> str:
    """HMAC podpis check-in požadavku (ochrana proti replay přes timestamp)."""
    msg = f"{device_id}\n{timestamp}\n".encode() + hashlib.sha256(body).digest()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# --- envelope encryption ---------------------------------------------------


@dataclass(frozen=True)
class SealedSecret:
    """Uložená podoba tajemství: zabalený datový klíč + šifrovaný obsah."""

    wrapped_key: bytes  # nonce(12) || AESGCM(master, dek)
    ciphertext: bytes  # nonce(12) || AESGCM(dek, plaintext)
    key_version: int


class SecretBox:
    def __init__(self, master_key: bytes, key_version: int = 1) -> None:
        if len(master_key) != 32:
            raise ValueError("master klíč musí mít 32 bajtů")
        self._master = AESGCM(master_key)
        self.key_version = key_version

    @classmethod
    def from_file(cls, path: Path, key_version: int = 1) -> "SecretBox":
        mode = path.stat().st_mode & 0o777
        if mode & 0o007:
            raise PermissionError(f"{path} nesmí být čitelný pro ostatní (mode {mode:o})")
        return cls(base64.b64decode(path.read_text().strip()), key_version)

    @staticmethod
    def generate_master_key() -> str:
        return base64.b64encode(os.urandom(32)).decode()

    def seal(self, plaintext: bytes, context: bytes) -> SealedSecret:
        """`context` (např. b"device:<id>:sstp") se váže jako AAD –
        šifrovaný blob nejde přesunout k jinému záznamu."""
        dek = AESGCM.generate_key(bit_length=256)
        n1, n2 = os.urandom(12), os.urandom(12)
        wrapped = n1 + self._master.encrypt(n1, dek, context)
        ct = n2 + AESGCM(dek).encrypt(n2, plaintext, context)
        return SealedSecret(wrapped, ct, self.key_version)

    def open(self, sealed: SealedSecret, context: bytes) -> bytes:
        w, c = sealed.wrapped_key, sealed.ciphertext
        dek = self._master.decrypt(w[:12], w[12:], context)
        return AESGCM(dek).decrypt(c[:12], c[12:], context)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
