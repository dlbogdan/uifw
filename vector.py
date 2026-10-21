"""Vector renderer: data-driven parametric animated shapes (MicroPython).

The GPU-standard layer name is VectorRenderer; the shapes themselves
are plain data ("cartoons"): nested heightfield layers (polyline
profiles, u = 0 at tip .. 256 at base) driven by named signals (sums
of sines plus shared filtered-noise / drift / flare-pop random state).
All the per-frame cost stays in an allocation-free small-int row loop;
the definitions buy artistic freedom at setup time only.

Also provides fill_polygon, a general fixed-point scanline polygon
filler for rigid props (icons, bubbles, bars).
"""
from math import sin  # type: ignore
from random import random, uniform  # type: ignore
from array import array  # type: ignore
import micropython  # type: ignore  (device + host fakes provide it)


def _ptr_id(b):
    return b


# viper pointer builtins: inside the @micropython.viper body below the
# COMPILER resolves these names itself (on the board the module
# globals are never read; micropython.viper is a compile-time
# directive there, not a runtime attribute - do NOT alias the
# decorator, the compiler only recognizes the literal form). On the
# host the identity shims let the SAME kernel body run as plain
# Python over bytearray/array - one body, no twin to drift (parity
# is pinned by tests/test_vector_parity).
ptr8 = _ptr_id
ptr16 = _ptr_id
ptr32 = _ptr_id

_BIAS = 268435456         # 2^28: buffer bias (see _hf_rows). MUST
                          # stay under RP2's 2^30 small-int boundary:
                          # a bigger literal is typed 'object' and
                          # the viper compiler rejects the kernel.


@micropython.viper
def _hf_rows(buf, bw: int, width: int, height: int, top: int, h: int,
             xcb: int, r: int, off: int, ws6: int, w64: int, gs: int,
             sk, sd, fs):
    """One heightfield layer straight into a GS4_HMSB buffer: the
    whole per-row loop (bend parabola, profile walk, span clip,
    nibble writes) runs as typed native code on the board - the
    Python path paid ~160 us per row in bytecode + an fb.hline call.

    Bit-exact twin of VectorRenderer._layer (the Python reference):
    same ints, same order, same >> arithmetic (viper emits arithmetic
    shift; Python >> floors - they agree on negatives). Buffers hold
    BIASed values (sd, fs carry +BIAS: viper pointer reads are
    UNSIGNED, so a negative int32 would read as a huge positive);
    the bias cancels in the setup diffs and is subtracted on read.

    Guards (caller): h > 0; |off| * h * h * r <= 2^30 - 1 (viper
    ints are machine words - Python ints are not); profile values
    in range at compile time (else the layer takes the ref path).
    """
    b = ptr8(buf)
    skv = ptr32(sk)
    sdv = ptr32(sd)
    fuv = ptr32(fs)
    g = off * h * h       # bend numerator, forward diffs
    e1 = off * (1 - 2 * h)
    e2 = off << 1
    hw = 0
    si = 0
    k_next = skv[0]
    dh = sdv[0] - 268435456
    gsgs = (gs << 4) | gs
    for k in range(1, h):  # k = 0 at tip .. h-1 at base
        g += e1
        e1 += e2
        if k > k_next:  # crossed a vertex: snap, then new slope
            si += 1
            k_next = skv[si]
            dh = sdv[si] - 268435456
            hw = ((fuv[si] - 268435456) * w64) >> 6
        hw += dh
        xc = xcb + ((g * r) >> 10)
        if hw > 128:  # wobble gate (>2 px): no offset at the tip
            xc += ws6
        xa = (xc - hw) >> 6
        xb = (xc + hw) >> 6
        if xb > xa:
            y = top + k
            if y >= height:
                break            # rows only grow: done
            if y >= 0:
                if xa < 0:
                    xa = 0
                if xb > width:
                    xb = width
                if xb > xa:
                    # GS4_HMSB nibble span, exactly what fb.hline
                    # would paint: odd head pixel, full bytes, even
                    # tail pixel
                    j = y * bw + (xa >> 1)
                    if xa & 1:
                        b[j] = (b[j] & 240) | gs   # keep even (high) px
                        j += 1
                        xa += 1
                    while xa + 1 < xb:
                        b[j] = gsgs
                        j += 1
                        xa += 2
                    if xa < xb:
                        b[j] = (gs << 4) | (b[j] & 15)


def fill_polygon(surface, pts, gs):
    """Fill a polygon using even-odd scanlines and surface.hline (C speed).

    Fixed-point (1/64 px) active-edge walk: the per-scanline loop is
    integer adds only - no float objects (software floats on RP2040
    allocate and murder the GC), no per-edge re-evaluation.

    Args:
        surface (Surface): Target surface (anything with hline).
        pts (list): Vertices as (x, y) tuples in order; polygon is
            implicitly closed. Coordinates may be floats (converted
            once here, never in the row loop).
        gs (int): Grayscale fill value.
    """
    edges = []
    ymin = 1 << 30
    ymax = -(1 << 30)
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        if y0 < y1:
            ya, yb, xa, xb = y0, y1, x0, x1
        elif y1 < y0:
            ya, yb, xa, xb = y1, y0, x1, x0
        else:
            continue  # horizontal edge never crosses a scanline center
        ya64 = int(ya * 64)
        yb64 = int(yb * 64)
        edges.append([int(xa * 64), int((xb - xa) * 64 / (yb - ya)),
                      ya64, yb64])
        if ya64 < ymin:
            ymin = ya64
        if yb64 > ymax:
            ymax = yb64
    edges.sort(key=lambda e: e[2])
    n = len(edges)
    active = []
    ei = 0
    # row bounds from the Q6 span (float pts allowed): first/last row
    # whose center (y*64+32) can touch [ymin, ymax). Arithmetic >>6
    # floors negatives - int() truncates toward 0 and would drop the
    # top row when a float vertex sits exactly on one (dy < 0 is the
    # normal converter case: above the anchor).
    for y in range((ymin + 31) >> 6, (ymax + 31) >> 6):
        yy = (y << 6) + 32  # scanline center, Q6
        while ei < n and edges[ei][2] <= yy:
            e = edges[ei]
            e[0] += (e[1] * (yy - e[2])) >> 6
            active.append(e)
            ei += 1
        xs = []
        i = 0
        while i < len(active):
            e = active[i]
            if e[3] <= yy:
                active[i] = active[-1]
                active.pop()
                continue
            xs.append(e[0])
            e[0] += e[1]
            i += 1
        if len(xs) < 2:
            continue
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            xa = (xs[i] + 32) >> 6
            xb = (xs[i + 1] + 32) >> 6
            if xb > xa:
                surface.hline(xa, y, xb - xa, gs)


class VectorRenderer:
    """Render a vector animation defined as plain data.

    Definition (plain literals - define in a .py module, freeze it,
    or literal_eval from a file; nothing is parsed per frame). The
    definition is the art itself: shape + motion, NO position.
    Placement is instance state (constructor args, move(), .height):

        {
          'rand': {...},                   # optional random state
          'sig': {name: (base, term, ...), ...},   # named signals
          'layers': (layer, ...),          # drawn outer -> inner
        }

    Signal terms (evaluated per frame, shared across layers; freqs
    rad/s - draw() converts its int-ms t to seconds once):
        ('s', freq, amp, phase)  amp * sin(freq*t + phase)
        ('n', amp)               filtered noise (body wander)
        ('d', amp)               slow drift (lean)
        ('p', amp)               flare pop (height/lean kick)
        ('q', freq, amp)         pop shiver: amp * pop * sin(freq*t)

    Heightfield layer (default kind):
        {'prof': ((u256, f64), ...),  # polyline: u 0=tip..256=base,
                                       # f = half-width in 1/64 w
         'w': float,                  # max half-width px
         'hs': float,                 # fraction of instance height
         'gs': int,                   # grayscale
         'scale': 'flick',            # signal scaling height
         'bend': 'bend',              # signal bending in screen space
         'wob': ((freq, amp, phase), ...)}  # per-layer wobble sines

    Static polygon layer (SVG-like converted art, paper-cut edges):
        {'kind': 'poly',
         'gs': int,
         'pts': ((x, y), ...)}        # px (int or float: the
                                      # converter emits floats),
                                      # relative to the instance
                                      # anchor (x, y = bottom
                                      # center); implicitly closed,
                                      # even-odd fill; compiled once
                                      # into a Q6 edge table,
                                      # allocation-free frames

    Nesting (inner layers never escaping the darker ones behind)
    holds by construction when layers share scale/bend signals, have
    strictly ordered w and hs, and wob stays under the width margin.
    """

    def __init__(self, defn, x=128, y=63, height=None):
        """Args:
            defn (dict): the animation itself - shape + signals, no
                position (format below).
            x (int): anchor column (the shape's centerline).
            y (int): anchor row (the row the shape stands on).
                (x, y) is the bottom-center anchor point.
            height (int): shape height in px at the base row
                (default: y - 1, i.e. fills up to the top).
        """
        self.x = x
        self.y = y
        self.height = y - 1 if height is None else height
        self.visible = True  # Scene skips invisible renderables
        r = defn.get('rand', {})
        self.n_keep, self.n_step = r.get('noise', (0.9, 0.1))
        self.d_keep, self.d_step = r.get('drift', (0.985, 0.05))
        self.p_decay, self.p_prob = r.get('pop', (0.86, 0.03))[:2]
        self.p_lo, self.p_hi = 0.4, 1.0
        self.noise = 0.0
        self.drift = 0.0
        self.pop = 0.0
        self.sigs = defn['sig']
        # (name, base, terms) flattened once: the per-frame signal
        # path must not slice spec lists (spec[1:] allocates a list
        # per signal per frame - GC churn on-board)
        self._sig_spec = [(name, spec[0], tuple(spec[1:]))
                          for name, spec in defn['sig'].items()]
        self.sig = {}
        # Compile layers: hf = heightfield (default kind), poly = static
        # polygon (SVG-like converted art), compiled once into Q6 edge
        # tables so frames stay allocation-free.
        # hf layers ALSO compile to int32 arrays (us/fs profile, sk/sd
        # per-frame scratch) for the _hf_rows viper kernel; kf marks
        # art inside the kernel's int32 envelope (else the layer takes
        # the Python reference path - same pixels either way).
        self.layers = []
        self.lnames = []
        self.on = []
        self._ref = False        # test hook: force the Python path
        nmax = 0
        for L in defn['layers']:
            self.lnames.append(L.get('name'))
            self.on.append(1)
            if L.get('kind') == 'poly':
                edges, ymin, ymax = self._compile_poly(L['pts'])
                self.layers.append((1, edges, len(edges), ymin, ymax,
                                    L['gs']))
                if len(edges) > nmax:
                    nmax = len(edges)
            else:
                prof = L['prof']
                nseg = len(prof) - 1
                w64 = int(L['w'] * 64)
                # kernel envelope: keeps every viper int well inside
                # a signed machine word (sd = 4*df*w64/((du)*h) must
                # stay under the 2^28 BIAS, see _hf_rows)
                kf = 1 if w64 <= 8192 else 0
                for p in prof:
                    if p[0] > 1024 or p[1] > 4096 or p[1] < -4096:
                        kf = 0
                        break
                self.layers.append((0,
                                    array('i', [p[0] for p in prof]),
                                    array('i', [p[1] + _BIAS
                                               for p in prof]),
                                    nseg,
                                    array('i', [0] * nseg),
                                    array('i', [0] * nseg),
                                    w64, L['hs'], L['gs'], kf,
                                    tuple(L.get('wob', ())),
                                    L.get('scale', 'flick'),
                                    L.get('bend', 'bend')))
        # Shared scanline scratch for poly layers (never reallocated)
        self._act = [0] * nmax
        self._xs = [0] * nmax
        self._sig_on = self._any_hf()   # gates per-frame signal eval

    def move(self, x, y):
        """Re-anchor on screen - live, can change every frame.

        (x, y) is the bottom-center anchor: centerline column, base
        row. Size is untouched (it lives in .height).
        """
        self.x = x
        self.y = y

    def show(self, on=True, name=None):
        """Toggle visibility at runtime.

            boiler.show(False)             # whole artwork off
            boiler.show(False, 'flame')    # only layers named 'flame'
                                           # (burner hardware stays)
            boiler.show(True)              # back on

        Hidden layers skip their whole rasterization - near-zero
        cost - and when NO heightfield layer is on, the whole signal
        evaluation (sines, noise, pop: ~8 ms of software floats on
        RP2040) is skipped too. Static poly-only frames cost the
        poly loops; the shared random state simply pauses until
        motion resumes (invisible: it is noise).
        """
        if name is None:
            self.visible = on
            return
        v = 1 if on else 0
        on_list = self.on
        for i, ln in enumerate(self.lnames):
            if ln == name:
                on_list[i] = v
        self._sig_on = self._any_hf()

    def _any_hf(self):
        """1 if any heightfield (animated) layer is on: gates the
        per-frame signal evaluation (poly layers read no signals)."""
        on = self.on
        for i, L in enumerate(self.layers):
            if L[0] == 0 and on[i]:
                return 1
        return 0

    def draw(self, surface, t):
        """Render one frame at time t (int ms - the scene clock).
        Signal/wob freqs are rad/s (ART_FORMAT): ONE ms->s
        conversion here, per frame - the JSON art stays seconds.

        Clock wrap (P3): the sine arguments are ABSOLUTE phase, so
        the wave jumps at the 2^30 ms wrap (the elapsed ms are not a
        whole number of periods at the hand-tuned frequencies). No
        tdiff fix exists without an epoch, and an epoch retunes the
        art. noise/drift/pop are per-DRAW, so their rate follows the
        frame rate (A3). Both accepted as art policy: this module
        takes the clock as it is handed."""
        if not self.visible:
            return
        layers = self.layers
        if self._sig_on:
            t = t / 1000.0
            # Shared random state: exponential-filtered, never jittery
            self.noise = self.noise * self.n_keep \
                + uniform(-1.0, 1.0) * self.n_step
            self.drift = self.drift * self.d_keep \
                + uniform(-1.0, 1.0) * self.d_step
            self.pop *= self.p_decay
            if random() < self.p_prob:
                self.pop = uniform(0.4, 1.0)
            # Named signals
            noise, drift, pop = self.noise, self.drift, self.pop
            sig = self.sig
            for name, base, terms in self._sig_spec:
                v = base
                for term in terms:
                    k = term[0]
                    if k == 's':
                        v += term[2] * sin(term[1] * t + term[3])
                    elif k == 'n':
                        v += term[1] * noise
                    elif k == 'd':
                        v += term[1] * drift
                    elif k == 'p':
                        v += term[1] * pop
                    else:  # 'q'
                        v += term[2] * pop * sin(term[1] * t)
                sig[name] = v
            # First heightfield layer sets the shared bend reference
            # height
            hr = self.height
            h0 = 0
            for L in layers:
                if L[0] == 0:
                    h0 = int(hr * L[7] * sig[L[11]])
                    break
            r = 65536 // (h0 * h0) if h0 else 0
        ax = self.x
        ay = self.y
        on_list = self.on
        # viper fast path: a real Surface exposes .buf (the GS4
        # nibble buffer) - the kernel writes spans into it directly.
        # Test fakes / non-Surface targets keep the hline reference.
        if self._ref:
            buf = None
            bw = 0
        else:
            buf = getattr(surface, 'buf', None)
            bw = surface.byte_width if buf is not None else 0
        for i, L in enumerate(layers):
            if not on_list[i]:
                continue
            if L[0]:
                self._poly(surface, L, ax, ay)
                continue
            (_, us, fs, nseg, sk, sd, w64, hs, gs, kf, wob, sc, bn) = L
            h = int(hr * hs * sig[sc])
            if h <= 0:
                continue   # scale signal dipped to/below zero
            w = 0.0
            for (f, a, ph) in wob:
                w += a * sin(f * t + ph)
            off = int(sig[bn])   # shared: quantize once
            ws6 = round(w) << 6
            # Segment setup: nseg cheap int ops, shared by both
            # raster paths. sd is stored BIASed (the kernel's ptr32
            # reads are unsigned); the bias cancels in the fs diff.
            for j in range(nseg):
                u2 = us[j + 1]
                sk[j] = (u2 * h) >> 8
                sd[j] = (4 * (fs[j + 1] - fs[j]) * w64
                         // ((u2 - us[j]) * h)) + _BIAS
            if (buf is not None and kf
                    and r * (off if off >= 0 else -off) * h * h
                    <= 1073741823):
                _hf_rows(buf, bw, surface.width, surface.height,
                         self.y - h, h, ax << 6, r, off, ws6, w64, gs,
                         sk, sd, fs)
            else:
                self._layer(surface, us, fs, nseg, sk, sd, w64, h, r,
                            off, ws6, gs)

    @staticmethod
    def _compile_poly(pts):
        """Compile a polygon into a Q6 edge table (once, at startup).

        Edge: [x0_q6, dx_q6, y0_q6, y1_q6, x_cur_q6], sorted by y0;
        x_cur is re-seeded from x0 on activation each frame.
        Returns INCLUSIVE row bounds (ymin_row, ymax_row) derived from
        the same Q6 ints the edges carry, so float pts are first-class
        (the converter emits them): >>6 floors negatives, where int()
        would truncate toward 0 and drop a boundary row.
        """
        edges = []
        ymin = 1 << 30
        ymax = -(1 << 30)
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            if y0 < y1:
                ya, yb, xa, xb = y0, y1, x0, x1
            elif y1 < y0:
                ya, yb, xa, xb = y1, y0, x1, x0
            else:
                continue  # horizontal edge never crosses a scanline center
            ya64 = int(ya * 64)
            yb64 = int(yb * 64)
            edges.append([int(xa * 64), int((xb - xa) * 64 / (yb - ya)),
                          ya64, yb64, 0])
            if ya64 < ymin:
                ymin = ya64
            if yb64 > ymax:
                ymax = yb64
        edges.sort(key=lambda e: e[2])
        # first/last row whose center (y*64+32) can touch [ymin, ymax)
        return edges, (ymin + 31) >> 6, ((ymax + 31) >> 6) - 1

    def _poly(self, surface, L, ax, ay):
        """Rasterize one compiled polygon: even-odd scanlines, Q6.

        Same active-edge walk as fill_polygon but fully pre-allocated:
        edge/active/x tables are built at startup, the per-row loop is
        integer adds only. Coordinates are anchor-relative; (ax, ay)
        translates at draw time, so move() moves the whole artwork.
        """
        edges = L[1]
        n = L[2]
        gs = L[5]
        act = self._act
        xs = self._xs
        hl = surface.hline
        na = 0
        ei = 0
        for yr in range(L[3], L[4] + 1):
            yy = (yr << 6) + 32
            while ei < n and edges[ei][2] <= yy:
                e = edges[ei]
                e[4] = e[0] + ((e[1] * (yy - e[2])) >> 6)
                act[na] = ei
                na += 1
                ei += 1
            nx = 0
            i = 0
            while i < na:
                e = edges[act[i]]
                if e[3] <= yy:
                    na -= 1
                    act[i] = act[na]
                    continue
                xs[nx] = e[4]
                nx += 1
                e[4] += e[1]
                i += 1
            for i in range(1, nx):  # insertion sort (few crossings)
                v = xs[i]
                j = i - 1
                while j >= 0 and xs[j] > v:
                    xs[j + 1] = xs[j]
                    j -= 1
                xs[j + 1] = v
            k = 0
            while k < nx - 1:
                xa = ((xs[k] + 32) >> 6) + ax
                xb = ((xs[k + 1] + 32) >> 6) + ax
                if xb > xa:
                    hl(xa, yr + ay, xb - xa, gs)
                k += 2

    def _layer(self, surface, us, fs, nseg, sk, sd, w64, h, r, off, ws6,
               gs):
        """Python reference for _hf_rows: identical ints, identical
        order, fb.hline per row. Used for non-Surface targets (test
        fakes) and art outside the kernel's int32 envelope. sk/sd/fs
        are pre-filled int32 arrays (sd/fs BIASed, see _hf_rows).

        Piecewise-linear profile: one add per row within a segment,
        snapped to exact vertex values at crossings. Bend evaluated
        in screen space (r = 65536/h0^2) so all layers share the
        same centerline at a given row. Small-int only, no allocs.
        """
        top = self.y - h
        hl = surface.hline
        xcb = self.x << 6
        g = off * h * h  # bend numerator, forward diffs
        e1 = off * (1 - 2 * h)
        e2 = off << 1
        hw = 0
        si = 0
        k_next = sk[0]
        dh = sd[0] - _BIAS
        for k in range(1, h):  # k = 0 at tip .. h-1 at base
            g += e1
            e1 += e2
            if k > k_next:  # crossed a vertex: snap, then new slope
                si += 1
                k_next = sk[si]
                dh = sd[si] - _BIAS
                hw = ((fs[si] - _BIAS) * w64) >> 6
            hw += dh
            xc = xcb + ((g * r) >> 10)
            if hw > 128:  # wobble gate (>2 px): no offset at the tip
                xc += ws6
            xa = (xc - hw) >> 6
            xb = (xc + hw) >> 6
            if xb > xa:
                hl(xa, top + k, xb - xa, gs)
