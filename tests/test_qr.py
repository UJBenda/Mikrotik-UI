"""QR kód pro nastavení MFA (bez závislostí)."""

import pytest

from mtcontroller import qr, totp


def test_structure_and_versions():
    m = qr.matrix("A")
    assert len(m) == 21 and all(len(r) == 21 for r in m)
    # vyhledávací vzor: rám 7×7, střed 3×3, kolem bílý oddělovač
    for r0, c0 in ((0, 0), (0, 14), (14, 0)):
        assert all(m[r0][c0 + i] and m[r0 + 6][c0 + i] and m[r0 + i][c0] and m[r0 + i][c0 + 6] for i in range(7))
        assert all(m[r0 + r][c0 + c] for r in range(2, 5) for c in range(2, 5))
        assert not any(m[r0 + 1][c0 + i] for i in range(1, 6))
    assert not any(m[7][i] for i in range(8)) and m[13][8] is True          # oddělovač, tmavý modul
    assert [m[6][i] for i in range(8, 13)] == [True, False, True, False, True]  # časování
    # velikost roste s daty; kapacity úrovně M
    assert [qr.capacity(v) for v in (1, 2, 5, 9, 10)] == [14, 26, 84, 180, 213]
    assert len(qr.matrix("x" * 14)) == 21 and len(qr.matrix("x" * 15)) == 25 and len(qr.matrix("x" * 213)) == 57
    with pytest.raises(ValueError):
        qr.matrix("x" * 214)
    # verze ≥ 7 nese informaci o verzi (dvě zrcadlené kopie)
    m7 = qr.matrix("x" * 110)
    n = len(m7)
    assert n == 45 and all(m7[i // 3][n - 11 + i % 3] == m7[n - 11 + i % 3][i // 3] for i in range(18))


def test_known_vector_and_uri():
    # Reed–Solomon: známý příklad „HELLO WORLD“ 1-M (10 opravných slov)
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert qr._rs(data, 10) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
    assert qr._bch(0b00101, 10, 0x537) ^ 0x5412 == 0b100000011001110       # formát M, maska 5
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", "spravce@firma.example")
    rows = qr.rows(uri)
    assert len(rows) == len(rows[0]) and set("".join(rows)) == {"0", "1"} and rows == qr.rows(uri)
