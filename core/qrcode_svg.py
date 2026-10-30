"""
Minimal pure-Python QR Code generator (no dependencies).

* Byte mode, error-correction level M, versions 1-6 (up to 106 bytes).
* Returns the module matrix or a ready-to-use SVG string.

Enough for batch / verification codes. Verified by decoding the output with an
independent QR reader (see core/tests.py).
"""

# ---------------------------------------------------------------- tables (level M)
# version: (total codewords, EC codewords per block, number of blocks)
VERSIONS_M = {
    1: (26, 10, 1),
    2: (44, 16, 1),
    3: (70, 26, 1),
    4: (100, 18, 2),
    5: (134, 24, 2),
    6: (172, 16, 4),
}
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
MAX_BYTES = 106


# ---------------------------------------------------------------- Reed-Solomon
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


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree):
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _EXP[i])
        poly = nxt
    return poly


def _rs_remainder(data, degree):
    gen = _rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(degree):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


# ---------------------------------------------------------------- encoding
def _choose_version(n_bytes):
    for version, (total, ec, blocks) in VERSIONS_M.items():
        if n_bytes <= total - ec * blocks - 2:
            return version
    raise ValueError(f"Text too long for QR (max {MAX_BYTES} bytes).")


def _codewords(data_bytes, version):
    total, ec, blocks = VERSIONS_M[version]
    data_cw = total - ec * blocks

    bits = "0100" + format(len(data_bytes), "08b")
    bits += "".join(format(b, "08b") for b in data_bytes)
    bits += "0" * min(4, data_cw * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)

    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(words) < data_cw:
        words.append(pad[(len(words) - len(bits) // 8) % 2])

    per_block = data_cw // blocks
    data_blocks = [words[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    ec_blocks = [_rs_remainder(b, ec) for b in data_blocks]

    out = []
    for i in range(per_block):
        for block in data_blocks:
            out.append(block[i])
    for i in range(ec):
        for block in ec_blocks:
            out.append(block[i])
    return out


# ---------------------------------------------------------------- matrix
def _format_bits(mask):
    data = (0b00 << 3) | mask            # level M = 00
    rem = data << 10
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= 0x537 << (i - 10)
    return ((data << 10) | rem) ^ 0x5412


def _build(version, codewords, mask):
    size = 17 + 4 * version
    m = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def put(r, c, dark, fixed=True):
        if 0 <= r < size and 0 <= c < size:
            m[r][c] = dark
            if fixed:
                reserved[r][c] = True

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                ring = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                put(r, c, inside and (ring or core))

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(8, size - 8):                      # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)

    pos = ALIGNMENT[version]
    for r0 in pos:
        for c0 in pos:
            if reserved[r0][c0]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r0 + dr, c0 + dc, max(abs(dr), abs(dc)) != 1)

    put(size - 8, 8, True)                            # dark module

    for i in range(9):                                # reserve format areas
        reserved[8][i] = reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    # data bits, zig-zag
    bits = []
    for w in codewords:
        bits.extend((w >> s) & 1 for s in range(7, -1, -1))
    masks = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ]
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if reserved[r][c]:
                    continue
                bit = bits[idx] if idx < len(bits) else 0
                idx += 1
                m[r][c] = bool(bit) ^ masks[mask](r, c)
        upward = not upward
        col -= 2

    fmt = _format_bits(mask)

    def bit(i):
        return bool((fmt >> i) & 1)

    # copy 1: around the top-left finder
    for i in range(6):
        m[i][8] = bit(i)
    m[7][8] = bit(6)
    m[8][8] = bit(7)
    m[8][7] = bit(8)
    for i in range(9, 15):
        m[8][14 - i] = bit(i)

    # copy 2: split between the top-right and bottom-left finders
    for i in range(8):
        m[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        m[size - 15 + i][8] = bit(i)
    return [[bool(x) for x in row] for row in m]


def _penalty(m):
    n = len(m)
    score = 0
    for grid in (m, [list(r) for r in zip(*m)]):          # rule 1 (rows + cols)
        for row in grid:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
    for r in range(n - 1):                                 # rule 2
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = pat1[::-1]
    for grid in (m, [list(r) for r in zip(*m)]):          # rule 3
        for row in grid:
            line = [1 if x else 0 for x in row]
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    score += 40
    dark = sum(sum(1 for x in row if x) for row in m)       # rule 4
    percent = dark * 100 / (n * n)
    score += int(abs(percent - 50) // 5) * 10
    return score


def make_matrix(text):
    """Return the QR module matrix (list of rows of booleans)."""
    data = str(text).encode("utf-8")
    version = _choose_version(len(data))
    cw = _codewords(data, version)
    candidates = [_build(version, cw, mask) for mask in range(8)]
    return min(candidates, key=_penalty)


def make_svg(text, scale=8, border=4, dark="#000000", light="#ffffff"):
    """Return a standalone SVG document for ``text``."""
    m = make_matrix(text)
    n = len(m)
    size = (n + 2 * border) * scale
    rects = []
    for r, row in enumerate(m):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                rects.append(
                    f'<rect x="{(start + border) * scale}" y="{(r + border) * scale}" '
                    f'width="{(c - start) * scale}" height="{scale}"/>'
                )
            else:
                c += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="{light}"/>'
        f'<g fill="{dark}">{"".join(rects)}</g></svg>'
    )
