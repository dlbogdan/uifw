"""QR code: compact from-scratch encoder + scene renderable.

Sized for this panel: byte mode, versions 1-10 (v10 = 57 modules
still fits the 64 px screen at scale 1), EC L/M. Everything is
bytearray / packed bytes - no classes, no regex, no list-of-lists
grids, no mode optimization. ~11 KB resident vs ~44 KB for the uQR
port (uQR.py is kept in the repo as the tests' reference oracle, not
imported here). qr.unload() frees even that while no QR is on screen.

The renderer bakes the grid ONCE into a GS4_HMSB buffer (grayscale
baked in, like SpriteStore sprites), so every frame is a pure row-copy
blit - a static QR costs no per-pixel work per frame. Re-encode with
set_data() only when the payload changes.

Hardware-free (pure Python): runs in the host tests too.
"""

# --- GF(256), prim polynomial 0x11d ------------------------------------
_EXP = bytearray(512)   # 512 entries: no mod in the multiply
_LOG = bytearray(256)


def _gf():
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_gf()

# --- tables (extracted from the QR spec via uQR, v1-10) ----------------
# Per version 1-10, EC M then L (index = (version-1)*2 + ec, ec =
# format-info bits: M=0, L=1), 5 bytes each:
# (ecc codewords/block, numBlocks1, dataPerBlock1, numBlocks2, dataPerBlock2)
_BLOCKS = bytes((
    10, 1, 16, 0, 0, 7, 1, 19, 0, 0,     # v1  M, L
    16, 1, 28, 0, 0, 10, 1, 34, 0, 0,    # v2
    26, 1, 44, 0, 0, 15, 1, 55, 0, 0,    # v3
    18, 2, 32, 0, 0, 20, 1, 80, 0, 0,    # v4
    24, 2, 43, 0, 0, 26, 1, 108, 0, 0,   # v5
    16, 4, 27, 0, 0, 18, 2, 68, 0, 0,    # v6
    18, 4, 31, 0, 0, 20, 2, 78, 0, 0,    # v7
    22, 2, 38, 2, 39, 24, 2, 97, 0, 0,   # v8
    22, 3, 36, 2, 37, 30, 2, 116, 0, 0,  # v9
    26, 4, 43, 1, 44, 18, 2, 68, 2, 69,  # v10
))

# Alignment pattern centers per version 2-10 (v1: none)
_ALIGN = (b'\x06\x12', b'\x06\x16', b'\x06\x1a', b'\x06\x1e', b'\x06"',
          b'\x06\x16&', b'\x06\x18*', b'\x06\x1a.', b'\x06\x1c2')

_EC = {'L': 1, 'M': 0}   # format-info bits


def _bch_fmt(d):
    v = d << 10
    for k in range(14, 9, -1):          # v < 2^15, generator deg 10
        if (v >> k) & 1:
            v ^= 0x537 << (k - 10)
    return ((d << 10) | v) ^ 0x5412


def _bch_ver(v0):
    v = v0 << 12
    for k in range(15, 12, -1):         # v < 2^16, generator deg 12
        if (v >> k) & 1:
            v ^= 0x1F25 << (k - 12)
    return (v0 << 12) | v


def _mask(m, i, j):
    if m == 0:
        return (i + j) % 2 == 0
    if m == 1:
        return i % 2 == 0
    if m == 2:
        return j % 3 == 0
    if m == 3:
        return (i + j) % 3 == 0
    if m == 4:
        return (i // 2 + j // 3) % 2 == 0
    if m == 5:
        return (i * j) % 2 + (i * j) % 3 == 0
    if m == 6:
        return ((i * j) % 2 + (i * j) % 3) % 2 == 0
    return ((i * j) % 3 + (i + j) % 2) % 2 == 0


def _blocks(version, ec):
    t = _BLOCKS[((version - 1) * 2 + ec) * 5:((version - 1) * 2 + ec) * 5 + 5]
    return t[0], t[1], t[2], t[3], t[4]


def _fits(data, version, ec):
    if isinstance(data, str):
        data = data.encode()
    ecc, nb1, dc1, nb2, dc2 = _blocks(version, ec)
    dcw = nb1 * dc1 + nb2 * dc2
    return 4 + (8 if version < 10 else 16) + 8 * len(data) <= dcw * 8


def _codewords(data, version, ec):
    """Byte-mode bitstream -> padded data codewords."""
    if isinstance(data, str):
        data = data.encode()
    ecc, nb1, dc1, nb2, dc2 = _blocks(version, ec)
    dcw = nb1 * dc1 + nb2 * dc2
    out = bytearray(dcw)
    bi = 0

    def put(val, n):
        nonlocal bi
        for k in range(n - 1, -1, -1):
            if (val >> k) & 1:
                out[bi >> 3] |= 0x80 >> (bi & 7)
            bi += 1

    put(4, 4)                                   # byte mode
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    bi = min(bi + 4, dcw * 8)                   # terminator (zeros)
    j = (bi + 7) >> 3
    while j < dcw:                              # pad codewords
        out[j] = 0xEC if (j - ((bi + 7) >> 3)) % 2 == 0 else 0x11
        j += 1
    return out, ecc, nb1, dc1, nb2, dc2


def _rs_gen(n):
    """Generator polynomial of degree n, coeffs high->low, + log table."""
    g = bytearray([1])
    for i in range(n):
        ng = bytearray(len(g) + 1)
        for j in range(len(g)):
            ng[j] ^= g[j]
            ng[j + 1] ^= _EXP[_LOG[g[j]] + i]
        g = ng
    # log table WITHOUT the leading 1: the remainder loop multiplies f
    # by gen[1..n] (synthetic division consumes the leading term)
    return g, bytes(_LOG[c] for c in g[1:])


def _rs_rem(data, gen, gl):
    """Reed-Solomon remainder of one block."""
    n = len(gen) - 1
    r = bytearray(n)
    for b in data:
        f = b ^ r[0]
        r[0:n - 1] = r[1:n]
        r[n - 1] = 0
        if f:
            lf = _LOG[f]
            for j in range(n):
                r[j] ^= _EXP[lf + gl[j]]
    return r


def _interleave(cw, ecc, nb1, dc1, nb2, dc2):
    """Split into blocks, RS-encode, interleave per spec."""
    gen, gl = _rs_gen(ecc)
    sizes = [dc1] * nb1 + [dc2] * nb2
    dblocks = []
    eblocks = []
    off = 0
    for s in sizes:
        blk = cw[off:off + s]
        off += s
        dblocks.append(blk)
        eblocks.append(_rs_rem(blk, gen, gl))
    out = bytearray()
    maxdc = dc2 if nb2 else dc1
    for i in range(maxdc):
        for blk in dblocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ecc):
        for blk in eblocks:
            out.append(blk[i])
    return out


def _setup(g, res, side, version):
    """Function patterns: finders, separators, timing, alignment,
    format/version reservations. res=1 cells are skipped by data."""
    for r0, c0 in ((0, 0), (0, side - 7), (side - 7, 0)):
        for r in range(-1, 8):
            rr = r0 + r
            if rr < 0 or rr >= side:
                continue
            for c in range(-1, 8):
                cc = c0 + c
                if cc < 0 or cc >= side:
                    continue
                g[rr * side + cc] = int(
                    (0 <= r <= 6 and (c == 0 or c == 6))
                    or (0 <= c <= 6 and (r == 0 or r == 6))
                    or (2 <= r <= 4 and 2 <= c <= 4))
                res[rr * side + cc] = 1
    if version >= 2:                # alignment BEFORE timing (spec
        pos = _ALIGN[version - 2]   # order: timing skips their cells)
        for i in range(len(pos)):
            for j in range(len(pos)):
                r0, c0 = pos[i], pos[j]
                if res[r0 * side + c0]:
                    continue        # overlaps a finder
                for r in range(-2, 3):
                    for c in range(-2, 3):
                        g[(r0 + r) * side + c0 + c] = int(
                            r == -2 or r == 2 or c == -2 or c == 2
                            or (r == 0 and c == 0))
                        res[(r0 + r) * side + c0 + c] = 1
    for k in range(8, side - 8):    # timing: skip alignment cells
        if not res[k * side + 6]:
            g[k * side + 6] = (k & 1) == 0
            res[k * side + 6] = 1
        if not res[6 * side + k]:
            g[6 * side + k] = (k & 1) == 0
            res[6 * side + k] = 1
    for i in range(15):             # format areas (both copies)
        if i < 6:
            res[i * side + 8] = 1
        elif i < 8:
            res[(i + 1) * side + 8] = 1
        else:
            res[(side - 15 + i) * side + 8] = 1
        if i < 8:
            res[8 * side + side - i - 1] = 1
        elif i == 8:
            res[8 * side + 7] = 1
        else:
            res[8 * side + 15 - i - 1] = 1
    res[(side - 8) * side + 8] = 1  # dark module
    if version >= 7:                # version info areas
        for i in range(18):
            res[(i // 3) * side + i % 3 + side - 11] = 1
            res[(i % 3 + side - 11) * side + i // 3] = 1


def _place(g, res, side, cw, mask):
    """Zigzag data placement with mask (reserved cells stay 0: matches
    the reference scoring pass where format/version cells are light)."""
    bi = 0
    bit = 7
    n = len(cw)
    row = side - 1
    inc = -1
    for col in range(side - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if not res[row * side + c]:
                    d = (cw[bi] >> bit) & 1 if bi < n else 0
                    g[row * side + c] = d ^ (1 if _mask(mask, row, c) else 0)
                    bit -= 1
                    if bit < 0:
                        bi += 1
                        bit = 7
            row += inc
            if row < 0 or row >= side:
                row -= inc
                inc = -inc
                break


def _penalty(g, side):
    """Standard 4-rule mask penalty (same values as the reference)."""
    p = 0
    for k in range(side):                       # rule 1: runs >= 5
        for step in (1, side):                  # row k, then column k
            base = k * side if step == 1 else k
            prev = -1
            run = 0
            for j in range(side):
                v = g[base + j * step]
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    prev = v
                    run = 1
            if run >= 5:
                p += run - 2
    for r in range(side - 1):                   # rule 2: 2x2 blocks
        for c in range(side - 1):
            v = g[r * side + c]
            if v == g[r * side + c + 1] == g[(r + 1) * side + c] \
                    == g[(r + 1) * side + c + 1]:
                p += 3
    p1 = (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0)      # rule 3: finder-like
    p2 = (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1)
    for k in range(side):
        for j in range(side - 10):
            w = tuple(g[k * side + j + t] for t in range(11))
            if w == p1 or w == p2:
                p += 40
            w = tuple(g[(j + t) * side + k] for t in range(11))
            if w == p1 or w == p2:
                p += 40
    sq = side * side                            # rule 4: dark balance
    p += 10 * (abs(sum(g) * 100 - 50 * sq) // (5 * sq))
    return p


def _format(g, side, ec, mask, version):
    """Real format bits, dark module, version info (final pass)."""
    bits = _bch_fmt((ec << 3) | mask)
    for i in range(15):
        m = (bits >> i) & 1
        if i < 6:
            g[i * side + 8] = m
        elif i < 8:
            g[(i + 1) * side + 8] = m
        else:
            g[(side - 15 + i) * side + 8] = m
        if i < 8:
            g[8 * side + side - i - 1] = m
        elif i == 8:
            g[8 * side + 7] = m
        else:
            g[8 * side + 15 - i - 1] = m
    g[(side - 8) * side + 8] = 1
    if version >= 7:
        bits = _bch_ver(version)
        for i in range(18):
            m = (bits >> i) & 1
            g[(i // 3) * side + i % 3 + side - 11] = m
            g[(i % 3 + side - 11) * side + i // 3] = m


def _encode(data, ec, version, mask=None):
    """Full encode -> (grid bytearray, side, chosen mask)."""
    cw, ecc, nb1, dc1, nb2, dc2 = _codewords(data, version, ec)
    cw = _interleave(cw, ecc, nb1, dc1, nb2, dc2)
    side = version * 4 + 17
    g = bytearray(side * side)
    res = bytearray(side * side)
    _setup(g, res, side, version)
    if mask is None:
        best = -1
        for m in range(8):
            _place(g, res, side, cw, m)
            sc = _penalty(g, side)
            if m == 0 or sc < best:
                best = sc
                mask = m
    _place(g, res, side, cw, mask)
    _format(g, side, ec, mask, version)
    return g, side, mask


class QRRenderer:
    """Scene renderable for a QR code (encode + bake once per payload).

    Position/size follow the engine convention: (x, y) is the
    bottom-center anchor, live via move(); size is height px, snapped
    DOWN to whole-module scale (QR modules must be square integers -
    fractional scale would not scan).

    Only the baked GS4 sprite stays resident between frames (~1.3 KB
    for v2 @ scale 2): the module grid is transient, re-encoded on
    set_data()/scale change (a few ms - payloads are dynamic anyway).

    Args:
        data (str): QR payload (byte mode; max ~213 chars at v10-M).
        x, y (int): Bottom-center anchor (y = last drawn row).
        height (int): Pixel size; 0/None = native 1 px per module.
        gs (int): Module grayscale 0=Black to 15=White.
        border (int): Quiet-zone border in modules. The black screen
            around the code acts as quiet zone, so 0-2 is fine
            on-screen if nothing is drawn right next to it.
        version (int): Force QR version 1-10; None = smallest fit.
        ec (str): Error correction 'L' or 'M'.
        mask (int): Force mask 0-7; None = best penalty (rarely set).
    """

    def __init__(self, data, x=128, y=63, height=None, gs=15, border=2,
                 version=None, ec='M', mask=None):
        self.x = x
        self.y = y
        self.gs = gs
        self.border = border
        self.version = version
        self.ec = _EC[ec]
        self.mask = mask
        self.visible = True
        self._scale = 1
        self._req_h = height or 0
        self._data = data.encode() if isinstance(data, str) else data
        self._rebuild()

    def move(self, x, y):
        """Move the bottom-center anchor, like VectorRenderer.move()."""
        self.x = x
        self.y = y

    @property
    def height(self):
        """Drawn height in px (== width: a QR is square)."""
        return self._h

    @height.setter
    def height(self, px):
        self._req_h = px
        self._rebuild()        # re-encode: cheap, and keeps no grid

    def set_data(self, data):
        """Re-encode the payload and re-bake the sprite."""
        self._data = data.encode() if isinstance(data, str) else data
        self._rebuild()
        return self

    def _rebuild(self):
        v = self.version
        if v is None:
            for v in range(1, 11):
                if _fits(self._data, v, self.ec):
                    break
            else:
                raise ValueError('QR payload too long for v10')
        elif not _fits(self._data, v, self.ec):
            # forced version: fail clean BEFORE _encode walks off the
            # end of the codeword buffer (IndexError otherwise)
            raise ValueError('QR payload too long for forced v%d' % v)
        g, self._side, self._used_mask = _encode(self._data, self.ec,
                                                 v, self.mask)
        self._version_used = v
        self._modules = self._side + 2 * self.border
        s = self._req_h // self._modules if self._req_h else 1
        self._scale = s if s > 1 else 1
        self._bake(g)
        # g drops out here: only the sprite stays resident

    def _bake(self, g):
        """Rasterize the grid into a GS4_HMSB buffer (bake-time only).

        One packed row per module row, memcpy-repeated scale times;
        width padded even so blit_gs4 can take the fast path.
        """
        s = self._scale
        n = self._modules
        w = n * s
        if w & 1:
            w += 1  # pad even: blit_gs4 fast path
        self._w = w
        self._h = n * s
        fg_hi = self.gs << 4
        fg_lo = self.gs
        row_len = w >> 1
        row = bytearray(row_len)
        buf = bytearray(row_len * self._h)
        side = self._side
        b = self.border
        bi = 0
        for r in range(n):
            if b <= r < n - b:
                src = (r - b) * side
                for i in range(row_len):
                    row[i] = 0
                for c in range(side):
                    if g[src + c]:
                        for k in range(s):
                            p = (c + b) * s + k
                            row[p >> 1] |= fg_hi if p & 1 == 0 else fg_lo
            else:
                for i in range(row_len):
                    row[i] = 0
            for k in range(s):
                buf[bi:bi + row_len] = row
                bi += row_len
        self.buf = buf

    def draw(self, surface, t):
        """Blit the baked QR (static: t is unused)."""
        surface.blit_gs4(self.buf, self.x - self._w // 2,
                         self.y - self._h + 1, self._w, self._h)


def wifi(ssid, password=None, hidden=False, auth='WPA'):
    """Wi-Fi payload in the Wi-Fi Alliance Simple Config format - the
    one phones recognize and offer to join. Pair with QRRenderer:

        QRRenderer(wifi('MyNet', 's3cret'), ..., ec='L')

    Capacity: v6-M fits SSID+password up to 90 chars; the worst case
    (32+63) lands at v7-M. 'H:true' is emitted only when hidden.
    """
    def esc(s):
        out = ''
        for c in s:
            if c in '\\;,:\"':
                out += '\\'
            out += c
        return out

    p = 'WIFI:T:' + auth + ';S:' + esc(ssid) + ';'
    if password:
        p += 'P:' + esc(password) + ';'
    if hidden:
        p += 'H:true;'
    # trailing ';;' is REQUIRED by the Wi-Fi Simple Config grammar
    # (last field ';' + string terminator ';'): zxing/qifi emit it and
    # Android/iOS refuse to join without it. Not a typo, do not strip.
    return p + ';'


def unload():
    """Free the encoder's RAM (bytecode + tables, ~10 KB) while no QR
    is on screen. The app must drop EVERYTHING holding the module:
    its QRRenderer(s) AND its own 'qr' name (functions keep the
    module's globals alive). Next QR: 'import uifw.qr' again -
    recompile is a fraction of a second, payloads are dynamic anyway.

    Unregisters THIS module by __name__. The engine ships inside the
    uifw package, so the key is 'uifw.qr'; a literal 'qr' is never in
    sys.modules and raised KeyError.

        scene.remove(qr_item); del qr_item
        qr.unload()   # drops sys.modules['uifw.qr']
        del qr        # drop the app's module reference too
        gc.collect()  # ~10 KB back
    """
    import sys
    del sys.modules[__name__]
