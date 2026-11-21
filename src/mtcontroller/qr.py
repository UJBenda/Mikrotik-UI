"""QR kód (model 2, bajtový režim, úroveň opravy M, verze 1–10) bez závislostí.

Slouží k zobrazení otpauth:// URI při nastavení MFA – stačí do ~210 bajtů.
`matrix(data)` vrací čtvercovou matici (seznam řádků, True = tmavý modul) bez tichého okraje.
"""

from __future__ import annotations

# verze → (EC kódových slov na blok, [(počet bloků, datových slov v bloku), …]) pro úroveň M
_BLOCKS = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}

# GF(256), polynom 0x11d
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _rs(data: list[int], n: int) -> list[int]:
    """Reed–Solomon: n opravných slov k datům."""
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _EXP[_LOG[g] + i] if g else 0
        gen = nxt
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for j in range(n):
                if gen[j + 1]:
                    rem[j] ^= _EXP[_LOG[gen[j + 1]] + _LOG[f]]
    return rem


def capacity(version: int) -> int:
    """Kolik bajtů dat se do verze vejde (bajtový režim, úroveň M)."""
    total = sum(n * k for n, k in _BLOCKS[version][1])
    return (total * 8 - 4 - (8 if version < 10 else 16)) // 8


def _codewords(data: bytes, version: int) -> list[int]:
    ec, groups = _BLOCKS[version]
    total = sum(n * k for n, k in groups)
    bits: list[int] = []

    def put(v: int, n: int) -> None:
        bits.extend((v >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, total * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    i = 0
    while len(words) < total:
        words.append(pad[i % 2])
        i += 1
    blocks, pos = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[pos:pos + k])
            pos += k
    eccs = [_rs(b, ec) for b in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec):
        out.extend(e[i] for e in eccs)
    return out


def _bch(value: int, bits: int, poly: int) -> int:
    v = value << bits
    plen = poly.bit_length()
    while v.bit_length() >= plen:
        v ^= poly << (v.bit_length() - plen)
    return (value << bits) | v


_MASKS = [
    lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _penalty(m: list[list[bool]]) -> int:
    n = len(m)
    score = 0
    lines = [row for row in m] + [[m[r][c] for r in range(n)] for c in range(n)]
    pat1 = [True, False, True, True, True, False, True, False, False, False, False]
    pat2 = pat1[::-1]
    for line in lines:
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        for i in range(n - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    score += abs(dark * 20 // (n * n) - 10) * 10
    return score


def matrix(data: bytes | str) -> list[list[bool]]:
    if isinstance(data, str):
        data = data.encode("utf-8")
    version = next((v for v in range(1, 11) if len(data) <= capacity(v)), None)
    if version is None:
        raise ValueError("data jsou na QR kód příliš dlouhá")
    n = 17 + 4 * version
    m: list[list[bool | None]] = [[None] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r: int, c: int, v: bool) -> None:
        if 0 <= r < n and 0 <= c < n:
            m[r][c] = v
            fixed[r][c] = True

    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):           # vyhledávací vzory + oddělovače
        for r in range(-1, 8):
            for c in range(-1, 8):
                inside = 0 <= r <= 6 and 0 <= c <= 6
                dark = inside and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
                put(r0 + r, c0 + c, dark)
    al = _ALIGN[version]
    for r0 in al:                                               # zarovnávací vzory
        for c0 in al:
            if fixed[r0][c0]:
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    put(r0 + r, c0 + c, max(abs(r), abs(c)) != 1)
    for i in range(8, n - 8):                                   # časování
        if not fixed[6][i]:
            put(6, i, i % 2 == 0)
        if not fixed[i][6]:
            put(i, 6, i % 2 == 0)
    put(n - 8, 8, True)                                         # tmavý modul
    for i in range(9):                                          # rezervace formátu
        for r, c in ((8, i), (i, 8)):
            if not fixed[r][c]:
                put(r, c, False)
    for i in range(8):
        for r, c in ((8, n - 1 - i), (n - 1 - i, 8)):
            if not fixed[r][c]:
                put(r, c, False)
    if version >= 7:                                            # informace o verzi
        vi = _bch(version, 12, 0x1F25)
        for i in range(18):
            bit = bool((vi >> i) & 1)
            put(i // 3, n - 11 + i % 3, bit)
            put(n - 11 + i % 3, i // 3, bit)

    words = _codewords(data, version)
    bits = [(w >> i) & 1 for w in words for i in range(7, -1, -1)]
    k = 0
    c = n - 1
    up = True
    while c > 0:                                                # klikatý zápis dat
        if c == 6:
            c -= 1
        for r in (range(n - 1, -1, -1) if up else range(n)):
            for cc in (c, c - 1):
                if not fixed[r][cc]:
                    m[r][cc] = bool(bits[k]) if k < len(bits) else False
                    k += 1
        up = not up
        c -= 2

    best, best_score = None, None
    for mi, fn in enumerate(_MASKS):
        t = [[(bool(m[r][c]) ^ fn(r, c)) if not fixed[r][c] else bool(m[r][c]) for c in range(n)] for r in range(n)]
        fmt = _bch(mi, 10, 0x537) ^ 0x5412                      # úroveň M = 00, pak maska
        for i in range(15):
            bit = bool((fmt >> i) & 1)
            # kopie 1: kolem levého horního vzoru
            if i < 6:
                t[i][8] = bit
            elif i < 8:
                t[i + 1][8] = bit
            elif i == 8:
                t[8][7] = bit
            else:
                t[8][14 - i] = bit
            # kopie 2: pravý horní a levý dolní vzor
            if i < 8:
                t[8][n - 1 - i] = bit
            else:
                t[n - 15 + i][8] = bit
        s = _penalty(t)
        if best_score is None or s < best_score:
            best, best_score = t, s
    assert best is not None
    return best


def rows(data: bytes | str) -> list[str]:
    """Matice jako řádky znaků 1/0 (pro JSON)."""
    return ["".join("1" if x else "0" for x in row) for row in matrix(data)]
