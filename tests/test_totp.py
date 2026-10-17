import base64

from mtcontroller import totp

# RFC 6238, příloha B (SHA-1, klíč "12345678901234567890", 8 číslic)
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = {59: "94287082", 1111111109: "07081804", 1111111111: "14050471",
               1234567890: "89005924", 2000000000: "69279037", 20000000000: "65353130"}


def test_rfc6238_vectors():
    for t, code in RFC_VECTORS.items():
        assert totp.hotp(RFC_SECRET, t // 30, digits=8) == code


def test_verify_window_and_step():
    s = totp.new_secret()
    now = 1_700_000_000
    code = totp.totp(s, now)
    assert totp.verify(s, code, now) == now // 30
    assert totp.verify(s, code, now + 30) == now // 30       # o krok později ještě platí
    assert totp.verify(s, code, now + 120) is None           # dávno prošlý
    assert totp.verify(s, "12345", now) is None
    assert totp.verify(s, "abcdef", now) is None


def test_uri():
    uri = totp.provisioning_uri("ABC", "a@b.cz")
    assert uri.startswith("otpauth://totp/") and "secret=ABC" in uri
