import os
import stat

import pytest

from mtcontroller.security import (
    SecretBox, hash_password, new_token, sign_checkin, token_hash, verify_password,
)


def test_password_roundtrip():
    h = hash_password("spravne-dlouhe-heslo")
    assert h.startswith("scrypt$")
    assert verify_password("spravne-dlouhe-heslo", h)
    assert not verify_password("spatne-dlouhe-heslo", h)


def test_password_too_short():
    with pytest.raises(ValueError):
        hash_password("kratke")


def test_password_hash_is_salted():
    assert hash_password("stejne-heslo-123") != hash_password("stejne-heslo-123")


def test_verify_rejects_garbage():
    assert not verify_password("cokoliv-dlouheho", "neni-to-hash")


def test_tokens_unique_and_hashed():
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert token_hash(a) == token_hash(a)
    assert len(token_hash(a)) == 64 and token_hash(a) != a


def test_checkin_signature_binds_all_fields():
    key = os.urandom(32)
    base = sign_checkin(key, "dev1", 1000, b"{}")
    assert base == sign_checkin(key, "dev1", 1000, b"{}")
    assert base != sign_checkin(key, "dev2", 1000, b"{}")
    assert base != sign_checkin(key, "dev1", 1001, b"{}")
    assert base != sign_checkin(key, "dev1", 1000, b"{ }")


def test_secretbox_roundtrip_and_context_binding():
    box = SecretBox(os.urandom(32))
    sealed = box.seal(b"heslo-k-zarizeni", b"device:1:sstp")
    assert b"heslo-k-zarizeni" not in sealed.ciphertext
    assert box.open(sealed, b"device:1:sstp") == b"heslo-k-zarizeni"
    with pytest.raises(Exception):
        box.open(sealed, b"device:2:sstp")  # blob nejde přesunout k jinému záznamu


def test_secretbox_wrong_master_key_fails():
    sealed = SecretBox(os.urandom(32)).seal(b"x", b"ctx")
    with pytest.raises(Exception):
        SecretBox(os.urandom(32)).open(sealed, b"ctx")


def test_master_key_file_permissions(tmp_path):
    p = tmp_path / "master.key"
    p.write_text(SecretBox.generate_master_key())
    p.chmod(0o640)
    assert SecretBox.from_file(p).key_version == 1
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        SecretBox.from_file(p)
