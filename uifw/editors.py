"""editors.py: the editor family - modal value editors driven by the
same encoder events as the menu. Each editor is a self-contained
widget: hidden by default, draws one line at (x, y, w), consumes
events, and hands the value back as a STRING through on_done (the
widget never touches storage; the caller decides what a string means).

Menu-native use (the normal path): items declare "edit": "text" /
"int" / "float" in menu.json and Menu opens/closes them - the app
binds an action and reads the new value with get_value(id). Standalone
use also works: instantiate, set_text(), visible = True, route events.

Shared lifecycle (EditorBase):
    LONG_OK     commit: on_done(get_text()), hide
    LONG_BACK   discard and leave: on_cancel(), hide, value untouched
    SOFT1       discard: on_cancel(), hide, value untouched (LONG_BACK
                is the held-BACK spelling of that same exit)
TextEditor (charset cycling):
    UP / DOWN   cycle the char under the cursor through charset
                (wrapping); on an empty string the first detent
                creates the char.
    OK          cursor right; at the end appends charset[0] (max_len
                caps growth). Rotation mutates live - no draft state.
    BACK        delete char under cursor, cursor left.
ColumnEditor / IntEditor / FloatEditor (digit columns, car-radio):
    UP / DOWN   spin the digit under the cursor (0-9 wrap)
    OK          cursor right (wraps to the first column)
    BACK        zero the digit under the cursor
    commit      value is clamped IMMEDIATELY at the range edges (at
                max the digit stops up, at min it stops down; digits
                still wrap mid-range). Display is zero-padded to the
                width of max; no negatives (lo < 0 raises at init).
    FloatEditor is a ColumnEditor editing the SCALED digits (scale =
    10**dp) and drawing the '.' column. In menu.json the fixed-point
    form is declared as 'dp' = digits after the point (scale is the
    internal representation); numeric min/max are mandatory for both
    int and float - see build().
ChoiceEditor (preset options, the 'which mode?' control):
    UP / BACK   previous option (wraps)
    DOWN / OK   next option (wraps)
    The value is NEVER free text: it can only ever be one of the
    declared 'options' (mandatory, non-empty - raise at init), so an
    out-of-domain commit is structurally impossible, not validated
    after the fact. LONG_OK commits the shown option verbatim;
    nothing else mutates stored state. Display: '< option  i/n >' --
    the arrows telegraph the spin, the counter bounds the hunt.
    Masked TextEditor note: the mask covers every cell EXCEPT the
    one under the cursor -- a fully masked line cannot be edited
    (you cannot see what you are changing); the cursor cell shows
    the live character, everything past it stays masked. The layout
    therefore rebuilds on cursor MOVES too, not only on char edits
    (still user-paced, never per frame).

Charset is just a string - the editor never hardcodes policy. The
WIFI_* presets below cover what Wi-Fi actually accepts (SSID:
printable ASCII incl. space, <=32; PSK: same minus space, 8-63),
ordered by hunt frequency: lowercase, digits, common symbols,
uppercase. In menu.json, "charset" is 'ssid' / 'psk' (presets) or a
literal cycle string.
"""
from .events import UP, DOWN, OK, BACK, SOFT1, LONG_BACK, LONG_OK

# Printable ASCII the fonts cover (32..126), split into ordered bands.
_WIFI_BANDS = ('abcdefghijklmnopqrstuvwxyz',
               '0123456789',
               '.-_@ !#$%&*',
               'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
WIFI_SSID = ''.join(_WIFI_BANDS)
WIFI_PSK = WIFI_SSID.replace(' ', '')   # PSK grammar has no space


class EditorBase:
    """Lifecycle shared by every editor: hidden until shown, LONG_OK
    commits, LONG_BACK and SOFT1 both discard. Subclasses implement
    _edit(ev) for UP/DOWN/OK/BACK, get_text() and draw(surface, t)."""

    def __init__(self, x, y, w, renderer, gs=15, blink_hz=2,
                 on_done=None, on_cancel=None):
        self.r = renderer
        self.font = renderer.font
        self.x, self.y, self.w = x, y, w
        self.gs = gs
        self.blink_hz = blink_hz
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.visible = False

    def on_event(self, ev):
        if ev == LONG_OK:
            self.visible = False
            if self.on_done:
                self.on_done(self.get_text())
        elif ev == SOFT1 or ev == LONG_BACK:
            self.visible = False
            if self.on_cancel:
                self.on_cancel()
        else:
            self._edit(ev)

    def _edit(self, ev):
        pass

    def get_text(self):
        return ''

    # -- shared render helpers ------------------------------------------
    # Glyph layout tables are rebuilt on EDITS (user-paced), never per
    # frame (playbook rule 6); draw() only reads them.

    def _layout(self, s):
        """[(x_off, w, glyph_buf, w_pad)] for string s."""
        f = self.font
        lay = []
        x = 0
        for ch in s:
            g = f.get_letter_gs4(ch, self.gs)
            if g is None:
                g = f.get_letter_gs4(' ', self.gs)   # not in font
            buf, gw, gpad, gh = g
            lay.append((x, gw, buf, gpad))
            x += gw + 1                              # 1px spacing
        return lay

    def _draw_line(self, surface, t, lay, scroll=0, cell=-1):
        """Opaque plate + glyphs + cursor rect on lay[cell] (blink).

        Blink is ABSOLUTE phase - a function of t, not an integrated
        delta - so it never goes through tdiff and never drifts. At
        the ticks_ms wrap the phase jumps: 2^30 ms is not a whole
        number of blink periods, so the cursor skips at most one
        half-period, once per 12.4 days. P3 contract: ACCEPTED, no
        epoch (a per-frame add on a hot path buys one frame).
        """
        x, y, w = self.x, self.y, self.w
        h = self.font.height
        surface.fill_rect(x - 2, y - 2, w + 4, h + 4, 0)
        clip = (x, y, w, h)
        for gx, gw, gbuf, gpad in lay:
            if gx - scroll > w:
                break
            surface.blit_gs4(gbuf, x + gx - scroll, y, gpad, h, clip=clip)
        if cell >= 0 and len(lay) > cell:
            if not self.blink_hz or \
                    (t * 2 * self.blink_hz // 1000) % 2 == 0:
                cx, cw = lay[cell][0], lay[cell][1]
                surface.rect(x + cx - scroll - 1, y - 1, cw + 2, h + 2, 15)


class TextEditor(EditorBase):
    """One-line editor over a working copy; hands the string back.

    Position/size: (x, y) is the TOP-LEFT of the text line, w is the
    viewport width - longer strings scroll to keep the cursor cell
    visible.

    Args:
        text (string): initial value (working copy).
        x, y (int): top-left of the text line, panel coords.
        w (int): viewport width in px (scroll window).
        renderer (TextRenderer): glyph source (its font is used).
        charset (string): cycle order; see WIFI_SSID / WIFI_PSK.
        max_len (int): hard cap on string length.
        mask (str): render this char instead of the real one
            (passwords); None = show plain.
        gs (int): glyph grayscale.
        blink_hz (int): cursor blink rate (0 = solid).
        on_done (fn): called with the edited string on LONG_OK.
        on_cancel (fn): called on SOFT1 or LONG_BACK (no string, no
            save).
    """

    def __init__(self, text='', x=0, y=0, w=160, renderer=None,
                 charset=WIFI_SSID, max_len=32, mask=None, gs=15,
                 blink_hz=2, on_done=None, on_cancel=None):
        EditorBase.__init__(self, x, y, w, renderer, gs, blink_hz,
                            on_done, on_cancel)
        self.charset = charset
        self.max_len = max_len
        self.mask = mask
        self.buf = list(text)[:max_len]
        self.idx = max(0, len(self.buf) - 1)
        self.scroll = 0
        self._layout_t = None    # rebuilt on edits, not every frame

    def set_text(self, text):
        """Load a working copy (call before showing the editor)."""
        self.buf = list(text)[:self.max_len]
        self.idx = max(0, len(self.buf) - 1)
        self.scroll = 0
        self._layout_t = None

    def get_text(self):
        return ''.join(self.buf)

    # -- input ---------------------------------------------------------

    def _edit(self, ev):
        buf = self.buf
        if ev == UP or ev == DOWN:
            if not buf:
                buf.append(self.charset[0])   # first detent creates
                self._layout_t = None
            else:
                d = 1 if ev == DOWN else -1
                try:
                    i = self.charset.index(buf[self.idx])
                except ValueError:
                    i = 0                     # foreign char: 1st lap
                buf[self.idx] = self.charset[(i + d) % len(self.charset)]
                self._layout_t = None
        elif ev == OK:
            if self.idx < len(buf) - 1:
                self.idx += 1                 # commit, edit next
                self._layout_t = None         # unmask the NEW cursor
                                              # cell, re-mask the old
                                              # one (masked editors)
            elif len(buf) < self.max_len:
                buf.append(self.charset[0])   # new cell, editing it
                self.idx = len(buf) - 1
                self._layout_t = None
        elif ev == BACK:
            if buf:
                del buf[self.idx]
                self.idx = self.idx - 1 if self.idx else 0
                self._layout_t = None

    # -- render --------------------------------------------------------

    def draw(self, surface, t):
        if not self.visible:
            return
        if self._layout_t is None:
            self._rebuild()
        lay = self._layout_t
        w = self.w
        # keep the cursor cell inside the viewport (scroll by cells)
        cx, cw = self._cell(self.idx)
        if cx - self.scroll > w - cw:
            self.scroll = cx - w + cw
        elif cx < self.scroll:
            self.scroll = cx
        self._draw_line(surface, t, lay, self.scroll, self.idx)

    def _cell(self, i):
        """(x_offset, width) of cursor cell; empty string = placeholder."""
        if self._layout_t and i < len(self._layout_t):
            gx, gw, _, _ = self._layout_t[i]
            return gx, gw
        return 0, self.font.width

    def _rebuild(self):
        """Layout table from buf (masked); built on edits (user-paced),
        read every frame: keeps draw() allocation-free (playbook rule
        6). Empty string lays out one space: the cursor sits on it.

        A masked line that hides the cursor cell CANNOT BE EDITED --
        every detent lands blind. So the mask covers everything
        EXCEPT the cell under the cursor: past the cursor the secret
        stays hidden, at the cursor you see exactly what you are
        changing."""
        if self.mask:
            s = ''.join(ch if i == self.idx else self.mask
                        for i, ch in enumerate(self.buf))
        else:
            s = ''.join(self.buf)
        self._layout_t = self._layout(s if s else ' ')


class ColumnEditor(EditorBase):
    """Digit-column editor (car-radio style): the value shows as
    zero-padded fixed-width digits, the cursor sits on one column and
    rotation spins that digit. Internal state is an INT scaled by
    `scale` (1 = int, 10 = one decimal, ...); width comes from max,
    so 5..95 is two columns. No negatives: lo < 0 raises at init
    (the scaled // and % math would otherwise go silently wrong).

    Args:
        lo, hi (number): hard range - the value can never leave it,
            editing included (floats allowed with scale); lo >= 0.
        scale (int): 1/10/100... digits after the dot = digits(scale)-1.
        value (number): initial value (clamped into range).
    """

    def __init__(self, lo=0, hi=99, scale=1, value=None, x=0, y=0,
                 w=160, renderer=None, gs=15, blink_hz=2,
                 on_done=None, on_cancel=None):
        EditorBase.__init__(self, x, y, w, renderer, gs, blink_hz,
                            on_done, on_cancel)
        self.scale = scale
        self.lo = int(round(lo * scale))
        self.hi = int(round(hi * scale))
        if self.lo < 0:
            # the "no negatives" limit is a CONTRACT: get_text's
            # scaled // and % math goes silently wrong on negatives
            # (-3.5 -> '-4.5'), so enforce it loud, at init
            raise ValueError('negative range not supported')
        if self.hi <= self.lo:
            raise ValueError('empty editor range')
        self.cols = len(str(self.hi))
        self.dp = len(str(scale)) - 1
        self._pow = self._powers(self.cols)
        v = self.lo if value is None else int(round(value * scale))
        self.val = min(max(v, self.lo), self.hi)
        self.idx = self.cols - 1        # start on the ones column
        self._lay = None

    @staticmethod
    def _powers(n):
        p = [1] * n
        for i in range(1, n):
            p[i] = p[i - 1] * 10
        return p

    def set_text(self, text):
        """Load a value from a string (badge text, config); bad text
        falls back to lo, out-of-range text clamps. 'inf'/'1e999'
        parse as floats then overflow int() - OverflowError joins
        ValueError here: a corrupt config must never crash the UI.
        Cursor restarts at the ones column."""
        try:
            v = int(round(float(text) * self.scale))
        except (ValueError, OverflowError):
            v = self.lo
        self.val = min(max(v, self.lo), self.hi)
        self.idx = self.cols - 1
        self._lay = None

    def get_text(self):
        """Committed text: plain number, NO display padding (the
        padded form is display-only, _display). _set_digit/set_text
        keep val in range; the clamp here is belt-and-braces."""
        if self.val < self.lo:
            self.val = self.lo
        elif self.val > self.hi:
            self.val = self.hi
        if self.scale == 1:
            return str(self.val)
        return '%d.%s' % (self.val // self.scale,
                          str(self.val % self.scale).rjust(self.dp, '0'))

    def _display(self):
        """Zero-padded display form (width of max, '.' column when
        dp). NO clamping: val is range-enforced (init/set_text/
        _set_digit) and lo >= 0 is enforced - the old 0..9999 cap
        made the display LIE for wider ranges (showed '09999' for
        a committed 50000)."""
        s = str(self.val).rjust(self.cols, '0')
        if self.dp:
            s = s[:-self.dp] + '.' + s[-self.dp:]
        return s

    # -- input ---------------------------------------------------------

    def _edit(self, ev):
        if ev == UP or ev == DOWN:
            self._set_digit((self._digit() + (1 if ev == DOWN else -1))
                            % 10)
        elif ev == OK:
            self.idx = (self.idx + 1) % self.cols
        elif ev == BACK:
            self._set_digit(0)

    def _digit(self):
        return self.val // self._pow[self.cols - 1 - self.idx] % 10

    def _set_digit(self, d):
        """Apply a digit change, clamped IMMEDIATELY at the range
        edges: at max the digit stops going up, at min it stops going
        down (digits still wrap mid-range - the RESULT is what must
        stay inside [lo, hi], always, not just at commit)."""
        p = self._pow[self.cols - 1 - self.idx]
        v = self.val + (d - self.val // p % 10) * p
        if v < self.lo:
            v = self.lo
        elif v > self.hi:
            v = self.hi
        self.val = v
        self._lay = None

    # -- render --------------------------------------------------------

    def draw(self, surface, t):
        if not self.visible:
            return
        if self._lay is None:
            self._lay = self._layout(self._display())
        # cursor: digit columns only, skip the '.' column
        cell = self.idx if self.idx < self.cols - self.dp else self.idx + 1
        self._draw_line(surface, t, self._lay, 0, cell)


class IntEditor(ColumnEditor):
    """Integer editor: IntEditor(lo, hi) - digit columns, clamped."""

    def __init__(self, lo=0, hi=99, value=None, x=0, y=0, w=160,
                 renderer=None, gs=15, blink_hz=2, on_done=None,
                 on_cancel=None):
        ColumnEditor.__init__(self, lo, hi, 1, value, x, y, w, renderer,
                              gs, blink_hz, on_done, on_cancel)


class FloatEditor(ColumnEditor):
    """Fixed-point editor: FloatEditor(lo, hi, scale=10) - digit
    columns plus a '.' column, value read back as 'NN.N'."""

    def __init__(self, lo=0.0, hi=9.9, scale=10, value=None, x=0, y=0,
                 w=160, renderer=None, gs=15, blink_hz=2, on_done=None,
                 on_cancel=None):
        ColumnEditor.__init__(self, lo, hi, scale, value, x, y, w,
                              renderer, gs, blink_hz, on_done, on_cancel)


class ChoiceEditor(EditorBase):
    """Preset-option editor: the value can only ever be one of the
    declared options, so no free text and no post-hoc validation --
    an out-of-domain commit is structurally impossible.

    Args:
        options (list/tuple of str): the closed set, mandatory and
            non-empty (a choice with no choices is a config error,
            raised at init where the menu builds its editors).
        value (str): initial selection; text matching no option
            leaves the index where it is (open-from-unknown shows
            option 0 -- the user still can only pick a valid one).
    Input: UP/BACK previous, DOWN/OK next, both WRAPPING (a two-tap
    on a three-option list beats hunting). LONG_OK commits the shown
    option verbatim through on_done; LONG_BACK/SOFT1 discard.
    """

    def __init__(self, options, value=None, x=0, y=0, w=160,
                 renderer=None, gs=15, on_done=None, on_cancel=None):
        EditorBase.__init__(self, x, y, w, renderer, gs, 0,
                            on_done, on_cancel)
        self.options = list(options)
        if not self.options:
            raise ValueError('choice editor needs a non-empty option list')
        self.idx = 0
        if value is not None:
            try:
                self.idx = self.options.index(value)
            except ValueError:
                pass
        self._lay = None

    def set_text(self, text):
        """Select by text (the menu opens with the item's current
        value); unknown text keeps the last position."""
        try:
            self.idx = self.options.index(text)
        except ValueError:
            pass
        self._lay = None

    def get_text(self):
        return self.options[self.idx]

    def _edit(self, ev):
        if ev == UP or ev == BACK:
            self.idx = (self.idx - 1) % len(self.options)
            self._lay = None
        elif ev == DOWN or ev == OK:
            self.idx = (self.idx + 1) % len(self.options)
            self._lay = None

    def draw(self, surface, t):
        if not self.visible:
            return
        if self._lay is None:
            # '< option  i/n >': arrows telegraph the spin, the
            # counter bounds the hunt. Built on edits, read per frame.
            s = '< %s  %d/%d >' % (self.options[self.idx],
                                   self.idx + 1, len(self.options))
            self._lay = self._layout(s)
        self._draw_line(surface, t, self._lay, 0, -1)


def build(kind, it, renderer, x, y, w, gs=None):
    """Factory for menu.py: editor instance for an item dict ('edit'
    key), or None for an unknown kind. Editor params are item data:
    text: charset ('ssid'/'psk'/literal), max, mask (single char,
    e.g. '*' - password field; the cursor cell shows the live char,
    everything past it stays masked); choice: 'options' = non-empty
    list of preset strings (spun with UP/DOWN, commit verbatim);
    int: min/max (whole
    numbers); float: min/max + 'dp' = FIXED digits after the point.
    Numeric ranges are MANDATORY - no silent defaults - and a range
    that dp cannot show is a config error: raised at init, where the
    menu builds its editors (startup, loud, never mid-frame).
    gs: value-line shade (None = the editor family's own default)."""
    gs = 15 if gs is None else gs
    if kind == 'text':
        cs = it.get('charset', 'ssid')
        if cs == 'ssid':
            cs = WIFI_SSID
        elif cs == 'psk':
            cs = WIFI_PSK
        return TextEditor(x=x, y=y, w=w, renderer=renderer,
                          charset=cs, max_len=it.get('max', 32),
                          mask=it.get('mask'), gs=gs)
    if kind == 'choice':
        opts = it.get('options')
        if not opts:
            raise ValueError("edit choice: non-empty 'options' required (%r)"
                             % it.get('id'))
        return ChoiceEditor(opts, x=x, y=y, w=w, renderer=renderer, gs=gs)
    if kind in ('int', 'float'):
        if 'min' not in it or 'max' not in it:
            raise ValueError('edit %s: min/max required (%r)'
                             % (kind, it.get('id')))
        lo, hi = it['min'], it['max']
        if kind == 'int':
            if lo != int(lo) or hi != int(hi):
                raise ValueError('edit int: whole-number range (%r)'
                                 % it.get('id'))
            return IntEditor(int(lo), int(hi), x=x, y=y, w=w,
                             renderer=renderer, gs=gs)
        if 'dp' not in it:
            raise ValueError("edit float: 'dp' required (%r)"
                             % it.get('id'))
        dp = it['dp']
        scale = 10 ** dp
        for v in (lo, hi):
            if abs(v * scale - round(v * scale)) > 1e-6:
                raise ValueError('edit float: %s not representable '
                                 'with dp=%d (%r)' % (v, dp,
                                                       it.get('id')))
        return FloatEditor(lo, hi, scale, x=x, y=y, w=w,
                           renderer=renderer, gs=gs)
    return None
