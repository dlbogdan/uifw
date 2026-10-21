"""GS4 framebuffer surface: the pixel canvas every renderer draws on.

Surface owns one GS4_HMSB framebuffer (bytearray + FrameBuffer) and the
drawing primitives. It knows nothing about the display: a SwapChain
hands its memoryview to the panel (ssd1322.present_mv).

Renderers (cartoon, text, sprites, widgets) only need the primitive
methods below - anything with these can be a draw target.
"""
import micropython  # type: ignore
from framebuf import FrameBuffer, GS4_HMSB  # type: ignore


class Surface:
    """Drawing surface for one screenful of GS4 pixels.

    Args:
        width (int): Surface width in pixels.
        height (int): Surface height in pixels.
    """

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.byte_width = -(-width // 2)  # Ceiling division
        self.buffer_length = self.byte_width * height
        self.buf = bytearray(self.buffer_length)
        self.fb = FrameBuffer(self.buf, width, height, GS4_HMSB)
        # Pre-made full view: flip() hands this to the wire thread
        # without allocating (GIL-off safe).
        self.mv = memoryview(self.buf)[:]
        # Zero block for fast clear (bytearray slice-assign is memcpy;
        # framebuf.fill on GS4 is a per-pixel loop ~140 ns/px).
        # Tail block keeps the loop full-size even when
        # buffer_length % 512 != 0 (a short slice-assign would try to
        # RESIZE buf - BufferError, self.mv exports it).
        self._zblk = bytearray(512)
        self._ztail = bytearray(self.buffer_length % 512)

    def clear(self, gs=0):
        """Clear the surface.

        Args:
            gs (int): Grayscale 0=Black to 15=White (default grayscale table)
        """
        if gs == 0:
            buf = self.buf
            z = self._zblk
            t = self._ztail
            n = self.buffer_length - len(t)   # full blocks only
            for i in range(0, n, 512):
                buf[i:i + 512] = z
            if t:
                buf[n:] = t                   # exact-size tail, no resize
        else:
            self.fb.fill(gs)

    # FrameBuffer primitives (C speed)

    def pixel(self, x, y, gs=15):
        self.fb.pixel(x, y, gs)

    def hline(self, x, y, w, gs=15):
        self.fb.hline(x, y, w, gs)

    def vline(self, x, y, h, gs=15):
        self.fb.vline(x, y, h, gs)

    def line(self, x1, y1, x2, y2, gs=15):
        self.fb.line(x1, y1, x2, y2, gs)

    def rect(self, x, y, w, h, gs=15):
        self.fb.rect(x, y, w, h, gs)

    def fill_rect(self, x, y, w, h, gs=15):
        self.fb.fill_rect(x, y, w, h, gs)

    def circle(self, x, y, r, gs=15, fill=False):
        """Circle outline (fill=False) or filled disc: center (x, y),
        radius r (diameter 2r+1). Integer row spans - no sqrt, no
        per-row allocs; fb clips like every other primitive. Top/
        bottom arc rows draw their full span, side rows only their
        endpoints, so the outline is one pixel wide."""
        dx = r
        for dy in range(r + 1):
            while dx * dx + dy * dy > r * r:
                dx -= 1
            cap = dy >= r - 1
            self._span(x - dx, x + dx, y - dy, gs, fill, cap)
            if dy:
                self._span(x - dx, x + dx, y + dy, gs, fill, cap)

    def _span(self, x0, x1, y, gs, fill, cap):
        if fill or cap:
            self.fb.hline(x0, y, x1 - x0 + 1, gs)
        else:
            self.fb.pixel(x0, y, gs)
            self.fb.pixel(x1, y, gs)

    @micropython.viper
    def _lut(self, buf, table, n: int):
        """Map every buffer byte through a 256-byte LUT (viper)."""
        b = ptr8(buf)
        t = ptr8(table)
        for i in range(n):
            b[i] = t[b[i]]

    def apply_lut(self, table):
        """Post-process pass: remap every GS4 pixel through a LUT.

        GS4 packs two 4-bit pixels per byte, so a 256-entry byte table
        transforms both nibbles in one viper pass (~8 KB, well under
        1 ms). Build tables with Surface.lut(). Covers global dim,
        invert, posterize, contrast curves - any shade-to-shade map.
        """
        self._lut(self.buf, table, self.buffer_length)

    @staticmethod
    def lut(fn):
        """Build a 256-byte GS4 LUT from a shade->shade function.

        Example: dim = Surface.lut(lambda v: v * 3 >> 2)
                 invert = Surface.lut(lambda v: 15 - v)
        """
        t = bytearray(256)
        for i in range(256):
            t[i] = (fn(i >> 4) << 4) | (fn(i & 15) & 15)
        return t

    # Dirty diff (render-on-change): the swap chain diffs the back
    # buffer against the last-presented buffer; the diff IS the
    # animation detector - no widget declares anything.

    @micropython.viper
    def _first_dirty(self, a_buf, b_buf, nwords: int) -> int:
        """Index of the first differing 32-bit word, -1 if identical.

        Word-wide ptr32 scan: 2048 comparisons for a 256x64 GS4
        frame, not 8192 byte reads.
        """
        a = ptr32(a_buf)
        b = ptr32(b_buf)
        for i in range(nwords):
            if a[i] != b[i]:
                return i
        return -1

    @micropython.viper
    def _last_dirty(self, a_buf, b_buf, nwords: int) -> int:
        """Index of the last differing 32-bit word, -1 if identical."""
        a = ptr32(a_buf)
        b = ptr32(b_buf)
        for i in range(nwords - 1, -1, -1):
            if a[i] != b[i]:
                return i
        return -1

    def diff_rows(self, other_buf):
        """Dirty row span between this surface and another pixel copy.

        other_buf must be exactly buffer_length bytes (the swap
        chain's other surface, or its prev copy). Returns the
        inclusive row span (y0, y1) that differs, or None when the
        buffers are identical - an empty diff PROVES a present would
        change nothing.

        The scan is word-wide (viper _first_dirty/_last_dirty); a
        byte_width that is not word-aligned (never for real panels)
        falls back to a plain byte scan - correctness first.
        """
        buf = self.buf
        bw = self.byte_width
        if bw & 3:
            n = self.buffer_length
            f = n
            for i in range(n):
                if buf[i] != other_buf[i]:
                    f = i
                    break
            if f == n:
                return None
            l = n - 1
            while buf[l] == other_buf[l]:
                l -= 1
            return (f // bw, l // bw)
        nw = self.buffer_length >> 2
        f = self._first_dirty(buf, other_buf, nw)
        if f < 0:
            return None
        l = self._last_dirty(buf, other_buf, nw)
        wpr = bw >> 2
        return (f // wpr, l // wpr)

    @micropython.viper
    def _copy_rows(self, dst_buf, src_buf, start: int, sw: int, h: int,
                   bw: int):
        """Copy h rows of sw bytes into dst at byte offset start.

        Viper emitter: ptr8 pointer loads/stores, no allocations, no
        bounds checks. dst_buf/src_buf must support the buffer protocol.
        """
        dst = ptr8(dst_buf)
        src = ptr8(src_buf)
        o = start
        s = 0
        for _ in range(h):
            for i in range(sw):
                dst[o + i] = src[s + i]
            o += bw
            s += sw

    @micropython.viper
    def _blend_rows(self, dst_buf, src_buf, start: int, sw: int, h: int,
                    bw: int):
        """Color-key row merge: nonzero source nibbles overwrite dst.

        Same viper pattern as _copy_rows; a zero nibble keeps the
        destination pixel (transparent background).
        """
        dst = ptr8(dst_buf)
        src = ptr8(src_buf)
        o = start
        s = 0
        for _ in range(h):
            for i in range(sw):
                v = src[s + i]
                if v:
                    d = dst[o + i]
                    hi = v & 240
                    lo = v & 15
                    if hi == 0:
                        hi = d & 240
                    if lo == 0:
                        lo = d & 15
                    dst[o + i] = hi | lo
            o += bw
            s += sw

    @micropython.viper
    def _copy_rows_o(self, dst_buf, src_buf, start: int, sw: int,
                     nfull: int, tail: int, h: int, bw: int):
        """Byte-shifted copy for ODD x, EVEN w: source pixel 0 lands in
        the low nibble of dst byte `start`.

        Per row: byte 0 gets src[0] >> 4 (low nibble only), each full
        byte j in 1..nfull is (src[j-1] low nibble) | (src[j] high
        nibble), and the last byte (tail=1) takes src[sw-1]'s low
        nibble into its high nibble. Same no-alloc viper pattern as
        _copy_rows.
        """
        dst = ptr8(dst_buf)
        src = ptr8(src_buf)
        o = start
        s = 0
        for _ in range(h):
            d = dst[o]
            dst[o] = (d & 240) | (src[s] >> 4)
            for i in range(1, nfull + 1):
                dst[o + i] = ((src[s + i - 1] & 15) << 4) | (src[s + i] >> 4)
            if tail:
                j = nfull + 1
                d = dst[o + j]
                dst[o + j] = (d & 15) | ((src[s + sw - 1] & 15) << 4)
            o += bw
            s += sw

    @micropython.viper
    def _blend_rows_o(self, dst_buf, src_buf, start: int, sw: int,
                      nfull: int, tail: int, h: int, bw: int):
        """Byte-shifted color-key blend for ODD x, EVEN w (see
        _copy_rows_o).

        A zero nibble keeps the destination pixel; nibble masks are
        evaluated on the SHIFTED byte, so transparency survives the
        nibble boundary.
        """
        dst = ptr8(dst_buf)
        src = ptr8(src_buf)
        o = start
        s = 0
        for _ in range(h):
            v = src[s] >> 4
            if v:
                dst[o] = (dst[o] & 240) | v
            for i in range(1, nfull + 1):
                v = ((src[s + i - 1] & 15) << 4) | (src[s + i] >> 4)
                if v:
                    d = dst[o + i]
                    hi = v & 240
                    lo = v & 15
                    if hi == 0:
                        hi = d & 240
                    if lo == 0:
                        lo = d & 15
                    dst[o + i] = hi | lo
            if tail:
                j = nfull + 1
                v = (src[s + sw - 1] & 15) << 4
                if v:
                    dst[o + j] = (dst[o + j] & 15) | v
            o += bw
            s += sw

    @micropython.viper
    def _copy_nib(self, dst_buf, src_buf, x: int, y: int, w: int,
                  h: int, bw: int):
        """Nibble-stream copy for ODD w: framebuf packs GS4 rows back
        to back (row k starts at source nibble k*w), so rows are read
        nibble by nibble - any x, no allocations.

        o/s are nibble indices into dst/src; a nibble n lives in byte
        n >> 1, high half if n is even.
        """
        dst = ptr8(dst_buf)
        src = ptr8(src_buf)
        o = (y * bw << 1) + x
        s = 0
        st = bw << 1
        for _ in range(h):
            for i in range(w):
                sn = s + i
                v = (src[sn >> 1] >> (4 - ((sn & 1) << 2))) & 15
                dn = o + i
                j = dn >> 1
                b = dst[j]
                if dn & 1:
                    dst[j] = (b & 240) | v
                else:
                    dst[j] = (b & 15) | (v << 4)
            s += w
            o += st

    @micropython.viper
    def _blend_nib(self, dst_buf, src_buf, x: int, y: int, w: int,
                   h: int, bw: int):
        """Nibble-stream color-key blend for ODD w (see _copy_nib):
        a zero source nibble keeps the destination pixel."""
        dst = ptr8(dst_buf)
        src = ptr8(src_buf)
        o = (y * bw << 1) + x
        s = 0
        st = bw << 1
        for _ in range(h):
            for i in range(w):
                sn = s + i
                v = (src[sn >> 1] >> (4 - ((sn & 1) << 2))) & 15
                if v:
                    dn = o + i
                    j = dn >> 1
                    b = dst[j]
                    if dn & 1:
                        dst[j] = (b & 240) | v
                    else:
                        dst[j] = (b & 15) | (v << 4)
            s += w
            o += st

    def blit_gs4(self, buf, x, y, w, h, key=False, clip=None):
        """Blit a GS4_HMSB packed buffer (sprite/glyph) onto the surface.

        Every non-clipped blit is allocation-free: even w with even x
        rows go through the viper _copy_rows()/_blend_rows() byte
        kernels, even w with odd x through the byte-shifted
        _copy_rows_o()/_blend_rows_o(), odd w (any x) through the
        nibble-stream _copy_nib()/_blend_nib() (odd-w rows are not
        byte-aligned in the source). Partial blits (any part
        off-surface or off-clip) are clipped, not dropped: scrolling
        widgets need rows half-in/half-out of their region.

        Args:
            buf (bytearray): GS4_HMSB packed image data - framebuf
                layout, rows packed back to back as a nibble stream.
            x (int): Starting X position (may be negative).
            y (int): Starting Y position (may be negative).
            w (int): Width of drawing.
            h (int): Height of drawing.
            key (bool): True = color-key: source gs 0 is transparent,
                so text/shapes with a black background can be drawn
                over non-black pixels without punching holes.
            clip (tuple): Optional (cx, cy, cw, ch) rect: the blit is
                clipped to it as well as to the surface (viewport).
        """
        x0, y0, x1, y1 = 0, 0, self.width, self.height
        if clip is not None:
            cx, cy, cw, ch = clip
            if cx > x0:
                x0 = cx
            if cy > y0:
                y0 = cy
            if cx + cw < x1:
                x1 = cx + cw
            if cy + ch < y1:
                y1 = cy + ch
        # intersect with the sprite rect too: _blit_clip reads source
        # pixels only inside it
        if x > x0:
            x0 = x
        if y > y0:
            y0 = y
        if x + w < x1:
            x1 = x + w
        if y + h < y1:
            y1 = y + h
        if x0 >= x1 or y0 >= y1:
            return
        if x0 > x or y0 > y or x1 < x + w or y1 < y + h:
            self._blit_clip(buf, x, y, w, h, key, x0, y0, x1, y1)
            return
        if w & 1:
            # odd w: GS4_HMSB packs rows as a CONTIGUOUS nibble stream
            # (row k starts at nibble k*w - rows are not byte-aligned),
            # so byte kernels cannot be used at any x
            if key:
                self._blend_nib(self.buf, buf, x, y, w, h, self.byte_width)
            else:
                self._copy_nib(self.buf, buf, x, y, w, h, self.byte_width)
        elif x & 1:
            # odd x, even w: byte-shifted row kernels (w pixels from the
            # odd start are byte-aligned inside w+1 bytes; kernels handle
            # the nibble edges)
            sw = w // 2
            start = y * self.byte_width + x // 2
            if key:
                self._blend_rows_o(self.buf, buf, start, sw, sw - 1,
                                   1, h, self.byte_width)
            else:
                self._copy_rows_o(self.buf, buf, start, sw, sw - 1,
                                  1, h, self.byte_width)
        else:
            start = y * self.byte_width + x // 2
            if key:
                self._blend_rows(self.buf, buf, start, w // 2, h,
                                 self.byte_width)
            else:
                self._copy_rows(self.buf, buf, start, w // 2, h,
                                self.byte_width)

    def _blit_clip(self, buf, x, y, w, h, key, x0, y0, x1, y1):
        """Per-pixel blit of the visible part of a partial sprite.

        Slow path, taken only at surface/clip edges (a few glyph rows
        while scrolling) - the fast row-copy paths stay untouched.
        """
        fb = FrameBuffer(buf, w, h, GS4_HMSB)
        p = self.fb.pixel
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                v = fb.pixel(xx - x, yy - y)
                if v or not key:
                    p(xx, yy, v)

    @staticmethod
    def _corner(r):
        """Rounded-corner insets: columns skipped per corner row.

        _corner(r)[i] = columns to skip on the i-th row of a corner
        block (i=0 = outermost). Integer quarter-circle test on pixel
        centers. r <= 0 -> empty table.
        """
        ins = []
        for i in range(r):
            k = 0
            for j in range(r):
                dx = 2 * j + 1 - 2 * r
                dy = 2 * i + 1 - 2 * r
                if dx * dx + dy * dy > 4 * r * r:
                    k += 1
            ins.append(k)
        return ins

    def fill_rrect(self, x, y, w, h, r, gs=15):
        """Filled rounded rectangle (r = corner radius, 0 = square)."""
        if r <= 0:
            self.fill_rect(x, y, w, h, gs)
            return
        ins = self._corner(r)
        self.fill_rect(x, y + r, w, h - 2 * r, gs)
        for i in range(r):
            k = ins[i]
            self.hline(x + k, y + i, w - 2 * k, gs)
            self.hline(x + k, y + h - 1 - i, w - 2 * k, gs)

    def vgrad(self, x, y, w, h, gs0, gs1):
        """Vertical gs gradient: row 0 = gs0, row h-1 = gs1 (ints)."""
        if h <= 1:
            self.hline(x, y, w, gs0)
            return
        for i in range(h):
            self.hline(x, y + i, w, gs0 + (gs1 - gs0) * i // (h - 1))
