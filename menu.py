"""Menu: scrollable drill-in menu panel - a scene renderable.

draw(surface, t) + visible, added to a Scene like any other thing on
screen (demo: left panel; boiler/monitoring live beside it).

Input is abstracted: the app feeds small-int events (UP, DOWN, OK,
BACK, SOFT1, LONG_BACK, LONG_OK below) from any source (input.py or
its own code). The menu never touches hardware.

Content is data (menu.json): stable 'id' per item is the app's
callback key (bind()); 'label' is a pure display string (swap the
file for translation). badge: 'count' = children count, a literal,
or live text the app pushes with set_value(id, text).

'type': 'bool' items are checkboxes: no edit mode - OK toggles the
state, drawn as a square with an inner fill (gap ring) when on.
The bound action fires after the toggle and reads the new value
with get_bool(id). SOFT1 fires the action WITHOUT toggling (a pure
trigger). set_bool(id, v) pushes external state in (glyph follows).

'edit': 'text' | 'int' | 'float' items open a modal editor
(editors.py) on OK: the menu draws the chrome (plate, item label,
hints - per-item 'hints', falling back to the 'editor' section) and
routes ALL events to it.
LONG_OK commits - the value lands in the item (badge shows it,
get_value(id)) and the bound action fires with the NEW value (same
symmetry as bools). LONG_BACK and SOFT1 both discard, no action.
Editor params are item data too: charset/max (text), min/max (int),
min/max/dp (float, dp = fixed digits after the point); numeric ranges
are mandatory and validated at init. The app binds actions and never
touches the editor.

ACTION CONTRACT: bound actions fire fn(id, value) - value is the
item's CURRENT value at fire time: bool for 'type: bool', the
committed/seeded string for 'edit' items, None for plain triggers.
No closure factories needed. Menu.pop() and Menu.close_edit() are
the app's intent-named navigation calls; input stays on_event's
path (apps never synthesize events).

CONFIG BINDING (D9: the engine has no config system): a menu binds the
framework's ConfigManager with bind_config(cfg, section). The section is
a dict of id -> value inside that file; every editor commit and every
bool OK toggle calls cfg.set(section, id, value) BEFORE the bound action
fires (UI -> config), and cfg.set saves and notifies. bind_config also
SUBSCRIBES each id (cfg.subscribe('SECTION.id', cb)), so a write from
outside the UI - device shell, autotest, another task - lands on the
visible widget without a reload. The config owns the write: actions
react/report, they never save again (double write).

Fonts: Menu warms the glyph caches IT draws with, at init (row
font at gs_sel/gs_row/gs_hint, edit font at gs_edit) - the app
cannot know this gs set and must not warm menu shades for the
menu. Only the glyphs the content actually contains are baked
(set_value warms pushed values' chars; editor charsets convert
lazily at edit time). Title font stays lazy (few glyphs, short
strings).

Layout in the panel rect (x, y, w, h): title row + 'n/m' indicator,
separator, list viewport (rows x pitch, clipped), scrollbar, and the
selection pill - a gradient rounded bar baked ONCE at startup, then
one blit per frame. Scroll is smooth: the cursor steps per detent,
the list slides in Q6 fixed-point px (~75 ms/row). Labels too wide
for the panel marquee-scroll on the selected row (car-radio: hold,
pixel-smooth slide, hold, slide back); non-selected rows show the
head, clipped clear of badges/checkboxes.
"""
from .surface import Surface
from .events import UP, DOWN, OK, BACK, SOFT1, LONG_BACK, LONG_OK   # noqa: F401
from . import editors
from .clock import tdiff

# Event vocabulary (UP/DOWN/OK/BACK/SOFT1/LONG_BACK/LONG_OK) lives in
# events.py and is re-exported above: input sources produce it, widgets
# consume it. (LONG_BACK/LONG_OK = a button held past a threshold, the
# input.Buttons 'long' option: editors commit on LONG_OK, discard on
# LONG_BACK.)

# Marquee: labels too wide for the panel scroll car-radio style on
# the SELECTED row - hold, slide pixel-by-pixel, hold at the tail,
# slide back. Non-selected rows show the head, clipped clear of the
# badge/checkbox column.
MQ_HOLD = 800       # ms pause at each end
MQ_SPEED = 24       # px per second (Q6 internally: pixel-smooth)


class Menu:
    """Scrollable menu panel renderable.

    Args:
        defn (dict): parsed menu.json ({'title', 'items': [...]}).
        x, y, w, h (int): panel rect on the screen.
        title_text, row_text (TextRenderer): fonts for title / rows.
        edit_text (TextRenderer): font for editors + chrome (defaults
            to row_text).
        pitch (int): row pitch in px.
        radius (int): pill corner radius.
        pill_top, pill_bot (int): pill gradient gs ends.
        pill_dy (int): pill vertical offset in px, added to the -2 the
            bar sits at by default. The bar is anchored to the row BOX
            (height pitch + 2), not to the font's ink, so a font whose
            glyphs fill the pitch box (Terminus 6x12 at pitch 12) reads
            as the bar riding high: measured on menu.json labels, the
            bar centre sits 1.0 px ABOVE the ink centre at 0 and lands
            dead on it at pill_dy=1 (+1.0 px at 2). Row text,
            separators and the glide offsets do NOT move. Needs the
            viewport clip widened by the same amount (done in
            __init__) or the bar loses its bottom edge on the last
            visible row.
        gs_* (int): shades (title, selected row, normal row,
            separators, scrollbar track/thumb).
        spacing (int): letter spacing, both fonts.
        scroll_rate (int): slide speed in Q6 px per ms: a row crosses
            in (pitch << 6) / rate ms (pitch 12, rate 6 -> 768/6 =
            128 ms). Needs a fast app loop to look smooth: at 50 fps
            that is ~2 px steps; at 20 fps the ramp is ~3 frames.
        gs_edit (int): editor value-line shade (passed to editors).
        gs_hint (int): edit-screen chrome shade (item label, hints).

    STATE MAP (internal: the app's surface is events + bind()/
    bind_config() + set_value/set_items/get_value/
    get_bool; the fields below are what
    on_event()/draw() move between frames. n = len(levels[-1]) rows in
    the live level; rows = whole rows that fit the viewport):

      Integer truth (one entry per open level):
        idx[-1]  selected row, 0 <= idx[-1] <= n-1
        top      first visible row, 0 <= top <= max(0, n - rows)
      Q6 fixed-point px (64 units = 1 px) - the animated pair:
        scroll     list offset under the viewport top,
                   0 <= scroll <= (n - rows) * pitch << 6
        selq       pill row position,
                   0 <= selq <= (n - 1) * pitch << 6
        target     where scroll glides to; sel_target where selq does
        Set together in _select: target = (top * pitch) << 6 and
        sel_target = (idx[-1] * pitch) << 6. draw() uses
        top_px = scroll >> 6 and puts the pill at row
        (selq - scroll) >> 6. scroll and selq move ONLY through
        _glide(), once each per frame with ONE shared step - that
        shared step is the phase lock (pinned in tests/test_menu 3).
      ms clocks (draw()'s t IS ms - never utime, never converted):
        _t       ms of the previous frame; None before the first draw
                 AND re-set to None by _select whenever a NEW move
                 starts from the at-rest pair - the gate that stops
                 frame 1 jumping to the target (a stale _t from a
                 quiescent screen is not this move's animation time)
        mq_t     ms the current marquee phase started
        mq_last  ms of the previous frame (the marquee moves by dt)
      Marquee machine (the SELECTED row's label only):
        mq       label offset, Q6 px, 0 <= mq <= (label_w - vis_w) << 6
        mq_phase 0 hold@head, 1 slide to tail, 2 hold@tail, 3 back
        mq_id    id the state belongs to; a fresh selection resets it
      Ranges are checked in tests/test_menu section 14 - change a
      clamp there and that suite, not a comment, tells you.
    """

    def __init__(self, defn, x, y, w, h, title_text, row_text,
                 edit_text=None,
                 pitch=12, radius=4, pill_top=12, pill_bot=3, pill_dy=0,
                 gs_title=15, gs_sel=15, gs_row=10, gs_sep=6,
                 gs_track=3, gs_thumb=11, spacing=1, scroll_rate=6,
                 gs_edit=15, gs_hint=10):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.visible = True
        self.title_text = title_text
        self.row_text = row_text
        self.edit_text = edit_text or row_text
        self.spacing = spacing
        self.pitch = pitch
        self.pill_dy = pill_dy            # pill offset vs the row box
        self.scroll_rate = scroll_rate
        self.gs_title = gs_title
        self.gs_sel = gs_sel
        self.gs_row = gs_row
        self.gs_sep = gs_sep
        self.gs_track = gs_track
        self.gs_thumb = gs_thumb
        self.gs_edit = gs_edit
        self.gs_hint = gs_hint
        # geometry
        self.title_h = title_text.font.height
        self.row_h = row_text.font.height
        self.y0 = self.title_h + 2          # list top (sep line at title_h)
        self.sb_w = 4                       # scrollbar column width
        self.list_w = w - self.sb_w - 2
        self.list_h = h - self.y0
        self.rows = self.list_h // pitch
        # exact viewport: whole rows only. The leftover < pitch px
        # would otherwise expose a sliver of the off-viewport row at
        # the panel bottom (blit clipping draws its top rows in).
        self.list_h = self.rows * pitch
        # content + nav state
        self.levels = [defn['items']]
        self.titles = [defn.get('title', '')]
        self.idx = [0]
        self.top = 0
        self.scroll = 0                     # Q6 fixed-point px
        self.target = 0
        self.selq = 0                       # pill row pos, Q6 px (glides)
        self.sel_target = 0
        self._t = None
        self.values = {}                    # id -> live badge string
        self.actions = {}                   # id -> fn (app binds)
        self._cfg = None                    # ConfigManager (bind_config)
        self._section = 'UI'                # its section (bind_config)
        self.bools = {}                     # id -> bool ('type: bool')
        self._fed = set()                   # ids feed() GENERATED (S4:
                                            # a prune drops their values/
                                            # bools; static ids keep theirs)
        self._collect_bools(defn['items'])
        self._editors = {}                  # id -> editor ('edit: ...')
        self._ehints = {}                   # id -> per-item hints list
        self._edit = None                   # open editor (modal)
        self._edit_it = None                # its item (title source)
        self._hints = defn.get('editor', {}).get('hints', [])
        self._build_editors(defn['items'])
        self._lw = {}                       # id -> label width px
        self._measure(defn['items'])
        # per-frame caches (playbook rule 6: draw() allocates
        # nothing): badge text + width + right gutter + label clip,
        # one entry per id, rebuilt by set_value/set_items; the n/m
        # indicator (rebuilt only when sel/n change); the two
        # constant viewport clips
        self._bcache = {}
        self._ind = None                    # (sel, n, text, w)
        self._clip_p = (x, y + self.y0 - 2, self.list_w,
                        self.list_h + 2 + pill_dy)
        self._clip_t = (x, y + self.y0, self.list_w, self.list_h)
        # marquee state (belongs to the selected row's label)
        self.mq = 0                         # Q6 px offset
        self.mq_phase = 0                   # 0 hold 1 slide 2 hold 3 back
        self.mq_t = 0                       # ms: current phase start
        self.mq_last = 0                    # ms: previous frame
        self.mq_id = None                   # id the state belongs to
        # selection pill: baked once, blit forever
        self.pill_w = self.list_w - 2
        self.pill_h = pitch + 2
        self.pill = self._bake_pill(radius, pill_top, pill_bot)
        # Warm the glyph caches the MENU draws with, at init (once):
        # the app cannot know this gs set, and lazy baking spikes the
        # first frames. Row font: selected/plain rows + the edit-
        # screen chrome (drawn with the row font at gs_hint); edit
        # font: the editor value line. ONLY the glyphs this menu's
        # content actually contains are baked: the full 96-glyph
        # charset x shades cost ~47 KB on-board and the menu knows
        # its working set. Values pushed later (set_value) warm their
        # chars then; editor charsets convert lazily at edit time
        # (user-paced, one glyph ~1 ms). Idempotent per cache entry,
        # so a shared row/edit font just re-walks. Title stays lazy.
        chars = self._glyph_set()
        self.row_text.font.warm_cache(gs=self.gs_sel, chars=chars)
        self.row_text.font.warm_cache(gs=self.gs_row, chars=chars)
        self.row_text.font.warm_cache(gs=self.gs_hint, chars=chars)
        self.edit_text.font.warm_cache(gs=self.gs_edit,
                                       chars='0123456789.-, ')

    def _glyph_set(self):
        """Every char the row font will draw for this content:
        labels, literal badges, count digits, the '>' marker, the
        n/m indicator, and the hint lines (edit chrome, row font at
        gs_hint)."""
        s = set('>/0123456789')
        stack = [self.levels[0]]
        seen = {}
        while stack:
            items = stack.pop()
            if seen.get(id(items)):
                continue
            seen[id(items)] = 1
            for it in items:
                s.update(it['label'])
                b = it.get('badge')
                if b == 'count':
                    s.update('0123456789')
                elif isinstance(b, str):
                    s.update(b)
                ch = it.get('children')
                if ch:
                    stack.append(ch)
        for line in self._hints:
            s.update(line)
        for lines in self._ehints.values():
            for line in lines:
                s.update(line)
        return s

    def _bake_pill(self, r, gs0, gs1):
        """Gradient rounded bar + left cursor block -> one GS4 buf."""
        pw, ph = self.pill_w, self.pill_h
        s = Surface(pw, ph)
        ins = Surface._corner(r)
        for i in range(ph):
            gs = gs0 + (gs1 - gs0) * i // (ph - 1)
            if i < r:
                k = ins[i]
            elif ph - i <= r:
                k = ins[ph - 1 - i]
            else:
                k = 0
            s.hline(k, i, pw - 2 * k, gs)
        s.fill_rect(1, 3, 3, ph - 6, 15)    # cursor block (mock's left bar)
        return s.buf

    # ---- app API ---------------------------------------------------------

    def bind(self, item_id, fn):
        """Attach the app callback for an item id (menu.json 'id')."""
        self.actions[item_id] = fn

    def bind_config(self, cfg, section='UI', seed=True):
        """Bind the framework's ConfigManager. D9: config has ONE owner
        (micropy-system); the engine writes through it and keeps no
        config of its own. cfg is a manager_config.ConfigManager (the
        store is a file of its own, e.g. ConfigManager('/app-config.json')
        — /system-config.json is framework-owned and re-rendered at every
        provisioning, so device-committed UI values do not belong there).

        UI -> config: every editor commit (LONG_OK / close_edit(True))
        and every bool OK toggle calls cfg.set(section, id, value) BEFORE
        the bound action fires. The action therefore sees the value
        already persisted (cfg.set saves and notifies) and is for
        react/report only - never save again inside it. Discards and
        plain triggers write nothing: no state changed.

        config -> UI, two ways: seed=True seeds now from the loaded
        section, and cfg.subscribe keeps every persisted id live - a
        write from the device shell, an autotest or another task lands on
        the visible widget without a reload.

        Measured surface (manager_config.py): .config is a dict of
        sections, get(section, key, default) RAISES ValueError on a
        missing key with no default (lines 63-70), set(section, key,
        value) saves + notifies, subscribe('SECTION.key', cb). The UI
        therefore reads the SECTION DICT, never get()."""
        self._cfg = cfg
        self._section = section
        if seed:
            self._seed_config()
        for iid in list(self.bools) + list(self._editors):
            cfg.subscribe('%s.%s' % (section, iid),
                          lambda v, iid=iid: self._config_in(iid, v))

    def _seed_config(self):
        """Seed the UI from the bound section (config -> UI, one call at
        boot). Iterates the SECTION's ids, not the menu tree: the section
        may hold keys no widget shows, and unknown ids land on
        set_value's raw-badge path - harmless. Edit items go through the
        editor round-trip (ranges clamp, floats normalize), so a corrupt
        config can never garbage the UI. Bool STATE seeds here too (S2,
        owner decision: config is the truth - external paths write it and
        UI toggles write it - so a bound section WINS over menu.json
        'value', which is only the FACTORY DEFAULT for ids the section is
        silent on). Seeds fire no action: a seed is not a user commit."""
        if self._cfg is None:
            return
        sec = self._cfg.config.get(self._section)
        if not isinstance(sec, dict):
            return
        for iid, v in sec.items():
            if iid in self.bools:
                if v is not None:
                    self.bools[iid] = bool(v)
            else:
                self.set_value(iid, v)

    def _config_in(self, iid, v):
        """A config write from OUTSIDE the UI (cfg.subscribe path): the
        widget follows immediately, no action fires - an external write
        is not a user commit. An id the UI no longer has (a feed prune
        dropped the row, the subscription stays on the manager) is
        ignored: a stale key must not resurrect a value entry."""
        if iid not in self.bools and iid not in self._editors:
            return
        if iid in self.bools:
            self.set_bool(iid, v)
        else:
            self.set_value(iid, v)

    def set_value(self, item_id, text):
        """Push a value from OUTSIDE the UI. For an 'edit' item this
        is the config manager seeding (flash -> UI): the editor's own
        set_text/get_text round-trip validates it - ranges clamp,
        floats take their dp format, text obeys max - so a corrupt
        config can never garbage the UI. For any other item it is a
        live badge string (sensor readouts), stored raw. Seeds fire
        no action: a seed is not a user commit (rule of set_bool)."""
        ed = self._editors.get(item_id)
        self._bcache.pop(item_id, None)     # badge text/width changed
        if ed is None:
            self.values[item_id] = text          # live badge text
            self._warm_badge(text)
            return text
        ed.set_text(str(text))
        self.values[item_id] = ed.get_text()     # validated/normalized
        self._warm_badge(self.values[item_id])
        return self.values[item_id]

    def _warm_badge(self, text):
        """Warm the row font's shades for a pushed badge string (the
        event-paced moment, so the first frame that shows it never
        pays a lazy glyph conversion)."""
        if not isinstance(text, str) or not text:
            return
        f = self.row_text.font
        f.warm_cache(gs=self.gs_sel, chars=text)
        f.warm_cache(gs=self.gs_row, chars=text)

    def get_bool(self, item_id):
        """Current state of a 'type: bool' item (None = not one)."""
        return self.bools.get(item_id)

    def set_bool(self, item_id, v):
        """Push external state into a bool item (badge follows)."""
        self.bools[item_id] = bool(v)

    def set_items(self, item_id, items):
        """Replace an item's children at runtime - the app feeds lists
        the DEVICE learns (wifi scans, sensor trees); such content is
        the driver's to give, not menu.json's. New labels are measured
        (marquee), bool/edit kinds collected, caches of REMOVED ids
        are pruned, and nav stays sane: inside the replaced level
        (clamped; emptied -> reset to top) or - when the replacement
        ORPHANS a deeper level - nav TRUNCATES into the new list
        (S3, owner decision: the user lands where the content
        changed)."""
        it = self._find(item_id, self.levels[0])
        if it is None:
            return
        old = it.get('children')
        it['children'] = items
        self._bcache.clear()                # content changed: badge
                                            # caches are stale
        self._measure(items)
        self._collect_bools(items)
        self._build_editors(items)
        if old:
            self._prune(old, items)     # caches of removed ids must
                                        # not leak across refreshes
        for k, lv in enumerate(self.levels):
            if lv is old:               # a live level shows this list
                self.levels[k] = items
                if k < len(self.levels) - 1:
                    # S3: every live level DEEPER than k was the old
                    # subtree's children - unreachable from the tree
                    # now, yet still drawn and navigated (the ghost
                    # level). Truncate the stack to the replaced
                    # level: the user lands IN the new list.
                    del self.levels[k + 1:]
                    del self.titles[k + 1:]
                    del self.idx[k + 1:]
                if not items:
                    # empty level: _select bails on n=0, so reset
                    # nav by hand - else idx/targets stay stale
                    # and the pill floats mid-list over nothing
                    self.idx[-1] = 0
                    self.target = self.sel_target = 0
                    self.selq = self.scroll = 0
                else:
                    was = (self.target, self.sel_target)
                    self._select(self.idx[-1])  # clamp idx + targets
                    if (self.target, self.sel_target) != was:
                        # The replacement MOVED the geometry (rows came or
                        # went, the index got clamped): land at once, a
                        # glide would cross rows that no longer exist. A
                        # STEADY re-feed (same rows, refreshed badges - an
                        # app refreshing readings every sample) leaves the
                        # pair ALONE: the unconditional sync below used to
                        # teleport an in-flight glide to the target on every
                        # refresh, which reads as the selection running far
                        # too fast. Measured in demo-boiler 'Rooms' (fed at
                        # the app's 200 ms sample cadence): the last 7.58 px
                        # of a 14 px row arrived in ONE write, where the
                        # frames before it stepped 2.16 px.
                        self.selq = self.sel_target
                        self.scroll = self.target
                break

    def feed(self, item_id, rows, label, badge=None, key=None,
             on_pick=None):
        """Device-fed lists in ONE call: rows the DRIVER learned
        (wlan.scan(), a sensor tree) become this item's children -
        label(row) is the display string, badge(row) the badge text
        (None = no badge for that row). key(row) gives a STABLE id
        per row (default '%s.<n>' % item_id: stable across re-feeds,
        so nav and bindings survive a refresh). When on_pick is
        given, every generated id is bound to a framework-side
        closure firing on_pick(row) with the ORIGINAL row (not the
        dict) - the closure factory lives HERE, once; the app never
        writes one. Row lifetime = until the next feed: re-feed
        prunes vanished ids WITH their bindings (_prune) and their
        values/bools (generated ids are RECORDED in self._fed - the
        default '%s.<n>' ids are positional, so generation, not
        shape, marks them), and rebinds survivors to the new rows.
        Does NOT invalidate: Menu never sees the chain - call
        scene.invalidate() after."""
        items = []
        for i, row in enumerate(rows):
            iid = key(row) if key else '%s.%d' % (item_id, i)
            self._fed.add(iid)            # S4: generated -> prunable
            it = {'id': iid, 'label': label(row)}
            if badge is not None:
                b = badge(row)
                if b is not None:
                    it['badge'] = b
            items.append(it)
            if on_pick is not None:
                self.actions[iid] = lambda _iid, v, row=row: on_pick(row)
        self.set_items(item_id, items)

    def _ids(self, items, s):
        """All ids in a subtree (accumulated into s)."""
        for it in items:
            iid = it.get('id')
            if iid is not None:
                s.add(iid)
            ch = it.get('children')
            if ch:
                self._ids(ch, s)
        return s

    def _prune(self, old, new):
        """Drop per-id caches (editors, hints, label widths, and the
        action bindings feed() made) for ids that vanished with the
        replaced subtree: app-fed lists churn per refresh, the dicts
        must not grow with them - and a stale feed closure would keep
        the old row alive (documented feed lifetime). S4 (owner
        decision): the id's open editor is CLOSED WITHOUT COMMIT (the
        menu must never sit modal for a nonexistent item; a ghost
        commit would write the config and fire NO action - the binding
        is pruned with the id); values/bools follow the prune ONLY
        for feed-GENERATED ids (rows churn), while static menu.json
        ids keep theirs (they are config - bind_config doctrine). The
        bound config is never touched: a dropped feed row does not
        delete its key (deleting persisted keys is the app's call). A
        late external write for a pruned id is ignored (_config_in
        checks the id still exists)."""
        keep = self._ids(new, set())
        for iid in self._ids(old, set()):
            if iid not in keep:
                self._editors.pop(iid, None)
                self._ehints.pop(iid, None)
                self._lw.pop(iid, None)
                self.actions.pop(iid, None)
                if iid in self._fed:
                    self._fed.discard(iid)
                    self.values.pop(iid, None)
                    self.bools.pop(iid, None)
                if self._edit_it is not None and \
                        self._edit_it.get('id') == iid:
                    self._close_edit()

    def _find(self, iid, items):
        for it in items:
            if it.get('id') == iid:
                return it
            ch = it.get('children')
            if ch:
                r = self._find(iid, ch)
                if r is not None:
                    return r
        return None

    def _collect_bools(self, items):
        for it in items:
            if it.get('type') == 'bool':
                self.bools[it['id']] = self._bool_seed(it)
            ch = it.get('children')
            if ch:
                self._collect_bools(ch)

    def _cfg_set(self, iid, value):
        """One write path, UI -> config (bind_config): ConfigManager.set
        persists and notifies subscribers. Unbound menus keep their state
        in RAM only (the app may persist it itself)."""
        if self._cfg is not None:
            self._cfg.set(self._section, iid, value)

    def _bool_seed(self, it):
        """State seed for a bool item (S2, owner decision 2026-10-20):
        the CONFIG section is the truth - external paths (device shell, a
        telnet protocol, flash) write it, and UI toggles write it
        (_cfg_set) - so a bound section WINS; menu.json 'value' is
        only the FACTORY DEFAULT for ids the section is silent on
        (None). A re-feed therefore cannot revert a persisted
        toggle. Coercion: bool(v), the same rule as set_bool (push
        real bools/numbers - not 'false', which is truthy)."""
        if self._cfg is not None:
            v = self._cfg.config.get(self._section, {}).get(it['id'])
            if v is not None:
                return bool(v)
        return bool(it.get('value', False))

    def get_value(self, item_id):
        """Current value of an 'edit' item (None = never set). Badges
        show it; bound actions read it after a commit."""
        return self.values.get(item_id)

    def _build_editors(self, items):
        """Instantiate the editors declared by 'edit' items ONCE at
        init (rule 6: no modal setup work when the modal opens). The
        editor rect is derived from the panel; params are item data."""
        for it in items:
            kind = it.get('edit')
            if kind:
                iid = it['id']
                ed = editors.build(kind, it, self.edit_text,
                                   self.x + 6, self.y + 30, self.w - 12,
                                   gs=self.gs_edit)
                if ed is None:
                    raise ValueError('unknown edit kind: %r' % kind)
                ed.on_done = lambda text, iid=iid: self._commit(iid, text)
                ed.on_cancel = self._close_edit
                self._editors[iid] = ed
                if 'hints' in it:
                    self._ehints[iid] = it['hints']
                if 'value' in it:
                    self.values[iid] = str(it['value'])
            ch = it.get('children')
            if ch:
                self._build_editors(ch)

    def _commit(self, item_id, text):
        """Editor done: the value lands (badge shows it), the config
        persists it (bind_config), then the bound action fires with
        (id, NEW value) - the same symmetry as bool toggles."""
        self.values[item_id] = text
        self._edit = None
        self._edit_it = None
        self._cfg_set(item_id, text)
        fn = self.actions.get(item_id)
        if fn is not None:
            fn(item_id, text)

    def _close_edit(self):
        self._edit = None
        self._edit_it = None

    def _measure(self, items):
        """Label widths once at init: the marquee must never measure
        per frame (playbook rule 6)."""
        for it in items:
            iid = it.get('id')
            if iid is not None:
                self._lw[iid] = self.row_text.font.measure_text(
                    it['label'], self.spacing)
            ch = it.get('children')
            if ch:
                self._measure(ch)

    def _marquee(self, it, vis_w, now):
        """Advance the scroll of the selected label; returns px offset.

        Fits (or no id) -> pinned 0. State machine: hold@head, slide
        to tail, hold, slide back - Q6 accumulator, so the slide is
        one pixel at a time regardless of frame rate. Called once per
        frame, for the selected row only."""
        iid = it.get('id')
        over = ((self._lw.get(iid, 0) - vis_w) << 6) if iid else 0
        if over <= 0 or iid != self.mq_id:
            self.mq_id = iid           # fresh selection (or fits): head
            self.mq = 0
            self.mq_phase = 0
            self.mq_t = now
            self.mq_last = now
            return 0
        d = tdiff(now, self.mq_t)
        mv = tdiff(now, self.mq_last)   # move by dt, not by age of phase
        self.mq_last = now
        # tdiff: the ticks_ms wrap is a normal frame boundary, the
        # hold keeps counting and the slide keeps moving (a raw
        # subtraction went NEGATIVE there and these two clamps froze
        # the marquee for the rest of the wrap - P3, test_wrap).
        # They now cover only a gap beyond the half-range (2^29 ms),
        # where a delta reads with the wrong sign by clock.py's rule.
        if d < 0:
            d = 0
        if mv < 0:
            mv = 0
        ph = self.mq_phase
        if ph == 0 or ph == 2:
            if d >= MQ_HOLD:
                self.mq_phase = ph + 1
                self.mq_t = now
        else:
            if ph == 1:
                self.mq += mv * MQ_SPEED * 64 // 1000
            else:
                self.mq -= mv * MQ_SPEED * 64 // 1000
            if self.mq >= over:
                self.mq = over
                self.mq_phase = 2
                self.mq_t = now
            elif self.mq <= 0:
                self.mq = 0
                self.mq_phase = 0
                self.mq_t = now
        return self.mq >> 6

    def on_event(self, ev):
        """Consume one input event (UP/DOWN/OK/BACK/SOFT1/LONG_BACK/
        LONG_OK). While an editor is open it owns ALL input: the menu
        is blind until the editor closes itself (commit/discard)."""
        if self._edit is not None:
            self._edit.on_event(ev)
            return
        items = self.levels[-1]
        i = self.idx[-1]
        if ev == UP:
            self._select(i - 1)
        elif ev == DOWN:
            self._select(i + 1)
        elif ev == OK and items:
            it = items[i]
            if it.get('type') == 'bool':
                k = it.get('id')        # checkbox: OK flips the state
                self.bools[k] = not self.bools.get(k, False)
                self._cfg_set(k, self.bools[k])
            ed = self._editors.get(it.get('id'))
            if ed is not None:          # editable: OK opens the editor
                v = self.values.get(it.get('id'))
                ed.set_text('' if v is None else v)
                ed.visible = True
                self._edit = ed
                self._edit_it = it
                return
            ch = it.get('children')
            if ch:
                self.levels.append(ch)
                self.titles.append(it.get('title', it['label']))
                self.idx.append(0)
                self.top = 0
                self.scroll = self.target = 0
                self.selq = self.sel_target = 0   # no cross-level glide
            fn = self.actions.get(it.get('id'))
            if fn is not None:
                fn(it.get('id'), self._action_value(it))
        elif ev == BACK:
            self.pop()
        elif ev == SOFT1 and items:
            it = items[i]
            fn = self.actions.get(it.get('id'))
            if fn is not None:
                fn(it.get('id'), self._action_value(it))

    def pop(self):
        """Leave one level - what BACK does at the WIDGET level, the
        app's intent-named call (apps never synthesize events): pop
        to the parent with the selection remembered; at root the
        panel hides (the app re-shows it). An open editor is
        discarded first - a level under a live editor is nonsense."""
        if self._edit is not None:
            self._close_edit()
        if len(self.levels) > 1:
            self.levels.pop()
            self.titles.pop()
            self.idx.pop()
            self._select(self.idx[-1])  # scroll to remembered row
            self.selq = self.sel_target  # no cross-level glide
        else:
            self.visible = False        # root: panel away (app re-shows)

    def close_edit(self, commit=False):
        """Close an open editor from app code: True lands the value
        and fires the action (LONG_OK semantics), False discards
        (LONG_BACK/SOFT1 semantics). No-op when no editor is open."""
        if self._edit is None:
            return
        if commit:
            self._commit(self._edit_it.get('id'), self._edit.get_text())
        else:
            self._close_edit()

    def _action_value(self, it):
        """The value an action fires with: the bool state for
        'type: bool', the current value string for edit/badge items,
        None for a plain trigger that never had one."""
        if it.get('type') == 'bool':
            return self.bools.get(it.get('id'))
        return self.values.get(it.get('id'))

    def _select(self, i):
        n = len(self.levels[-1])
        if not n:
            return  # empty level (e.g. empty root): nothing to select
        # at-rest BEFORE the move decides whether this is a FRESH
        # start (see the _t gate below)
        at_rest = (self.scroll == self.target and
                   self.selq == self.sel_target)
        if i < 0:
            i = 0
        elif i > n - 1:
            i = n - 1
        self.idx[-1] = i
        if i < self.top:
            self.top = i
        elif i >= self.top + self.rows:
            self.top = i - self.rows + 1
        max_top = n - self.rows
        if max_top < 0:
            max_top = 0
        if self.top > max_top:
            self.top = max_top
        self.target = (self.top * self.pitch) << 6
        self.sel_target = (i * self.pitch) << 6
        if at_rest and (self.target != self.scroll or
                        self.sel_target != self.selq):
            # Motion starting from a DISPLAYED standstill: the gap
            # since the previous draw is not animation time - on a
            # still screen quiescence ages _t by up to recheck_ms +
            # one loop period, and charging that to frame 1 jumps
            # the pill 80-100% of a row in ONE write (the "skipped
            # glide"). Re-baseline with the same gate the first draw
            # uses: the glide integrates from the press frame. A
            # pair already IN FLIGHT keeps _t - a mid-glide target
            # change (key repeat, retarget) still catches up across
            # a blocked loop, pinned by test_menu 15.
            self._t = None

    def _badge(self, it):
        if it.get('type') == 'bool':
            return ''          # bool: graphical checkbox, not text
        v = self.values.get(it.get('id'))
        if v is not None:
            return v
        b = it.get('badge')
        if b == 'count':
            ch = it.get('children')
            return str(len(ch)) if ch else ''
        return '' if b is None else str(b)

    def _badge_w(self, it):
        """(badge, bw, rp, clip_l) cached per id: badge text, its
        width, the gutter it reserves, and the label clip built for
        that gutter - the per-frame row path reads the cache and
        allocates nothing. Invalidated by set_value (that id) and
        set_items (content changed)."""
        iid = it.get('id')
        c = self._bcache.get(iid) if iid is not None else None
        if c is None:
            badge = self._badge(it)
            bw = (self.row_text.font.measure_text(badge, self.spacing)
                  if badge else 0)
            rp = 12 if it.get('type') == 'bool' else (bw + 3 if bw else 2)
            c = (badge, bw, rp,
                 (self.x + 16, self.y + self.y0,
                  self.list_w - 16 - rp, self.list_h))
            if iid is not None:
                self._bcache[iid] = c
        return c

    def _draw_bool(self, surface, it, bx, ry, gs, vy0, vy1):
        """Checkbox glyph at the badge spot: 9px square; when on, a
        5px inner fill leaves a 1px black gap ring (filled square,
        not a check - and circles stay reserved for radio
        selectors). No clip path on these primitives, so it draws
        only while fully inside the list viewport."""
        by = ry + (self.row_h - 9) // 2
        if by < vy0 or by + 9 > vy1:
            return
        surface.rect(bx, by, 9, 9, gs)
        if self.bools.get(it.get('id')):
            surface.fill_rect(bx + 2, by + 2, 5, 5, gs)

    # ---- renderable -------------------------------------------------------

    def _draw_edit(self, surface, t):
        """Edit screen: black plate over the panel, the item's label
        as title, the editor line, hint lines bottom-anchored. The
        chrome strings are content: per-item 'hints' falling back to
        menu.json's 'editor' section; the title is the item's label."""
        x, y, w, h = self.x, self.y, self.w, self.h
        hints = self._ehints.get(self._edit_it.get('id')) or self._hints
        surface.fill_rect(x, y, w, h, 0)
        rt = self.row_text
        sp = self.spacing
        rt.draw(surface, x + 6, y + 6, self._edit_it['label'],
                gs=self.gs_hint, spacing=sp)
        self._edit.draw(surface, t)
        hy = y + h - 10 * len(hints)
        for line in hints:
            rt.draw(surface, x + 6, hy, line, gs=self.gs_hint,
                    spacing=sp)
            hy += 10

    @staticmethod
    def _glide(cur, target, step):
        """One Q6 approach step toward target; never overshoots.
        draw() calls this twice with the SAME step: equal steps are
        what keep the pill locked to the sliding list (the pill row is
        (selq - scroll) >> 6, so the offset stays fixed mid-glide)."""
        d = target - cur
        if -step <= d <= step:
            return target
        if d > 0:
            return cur + step
        return cur - step

    def draw(self, surface, t):
        # smooth scroll: integer Q6 slides (t IS the int-ms scene
        # clock - no conversion): list offset + pill
        now = t
        if self._t is not None:
            # tdiff, not a subtraction: the scroll/glide must survive
            # the ticks_ms wrap like the marquee does (P3)
            dt = tdiff(now, self._t)
            if dt > 0:
                step = dt * self.scroll_rate
                self.scroll = self._glide(self.scroll, self.target, step)
                self.selq = self._glide(self.selq, self.sel_target, step)
        self._t = now

        if self._edit is not None:
            self._draw_edit(surface, t)
            return

        x, y, w = self.x, self.y, self.w
        items = self.levels[-1]
        n = len(items)
        sel = self.idx[-1]
        top_px = self.scroll >> 6
        rt = self.row_text
        sp = self.spacing

        # title + 'n/m' indicator + separator (title clipped: never
        # past the panel edge, never under the indicator). Indicator
        # text, width and the title clip are cached until sel/n move
        # (draw() allocates nothing at rest - playbook rule 6).
        ind = self._ind
        if ind is None or ind[0] != sel or ind[1] != n:
            itext = '%d/%d' % (sel + 1 if n else 0, n)
            iw = rt.font.measure_text(itext, sp)
            ind = (sel, n, itext, iw,
                   (x + 2, y, max(self.list_w - iw - 6, 0), self.title_h))
            self._ind = ind
        self.title_text.draw(surface, x + 2, y, self.titles[-1],
                             gs=self.gs_title, spacing=sp, clip=ind[4])
        rt.draw(surface, x + self.list_w - ind[3],
                y + (self.title_h - self.row_h) // 2, ind[2],
                gs=self.gs_row, spacing=sp)
        surface.hline(x, y + self.title_h, w - self.sb_w, self.gs_sep)

        # rows: separators first, then the gliding pill (it interrupts
        # separators at rest), then all text on top; everything clipped
        # to the list viewport (partial rows at the edges while sliding
        # - blit_gs4 clip path)
        clip_p = self._clip_p
        clip_t = self._clip_t
        first = top_px // self.pitch
        last = (top_px + self.list_h - 1) // self.pitch + 1
        if last > n:
            last = n
        for j in range(first, last):
            it = items[j]
            if it.get('sep') and j > 0:
                ry = y + self.y0 + j * self.pitch - top_px
                if y + self.y0 <= ry - 2 < y + self.y0 + self.list_h:
                    surface.hline(x + 2, ry - 2, self.list_w - 4,
                                  self.gs_sep)
        # pill: own Q6 glide between rows (sub-pixel aligned to the
        # sliding list: both offsets are Q6); no selection, no pill
        if n:
            py = y + self.y0 - 2 + self.pill_dy + ((self.selq - self.scroll) >> 6)
            surface.blit_gs4(self.pill, x + 2, py, self.pill_w,
                             self.pill_h, clip=clip_p)
        lx = x + 16
        for j in range(first, last):
            it = items[j]
            ry = y + self.y0 + j * self.pitch - top_px
            gs = self.gs_sel if j == sel else self.gs_row
            # badge text, width, right gutter and the label clip:
            # cached per id (rebuilt by set_value/set_items only)
            badge, bw, rp, clip_l = self._badge_w(it)
            off = self._marquee(it, clip_l[2], now) if j == sel else 0
            rt.draw(surface, x + 5, ry, '>', gs=gs, spacing=sp, key=True,
                    clip=clip_t)
            rt.draw(surface, lx - off, ry, it['label'], gs=gs, spacing=sp,
                    key=True, clip=clip_l)
            if it.get('type') == 'bool':
                self._draw_bool(surface, it, x + self.list_w - 10, ry,
                                gs, y + self.y0, y + self.y0 + self.list_h)
            elif badge:
                rt.draw(surface, x + self.list_w - bw, ry, badge, gs=gs,
                        spacing=sp, key=True, clip=clip_t)

        # scrollbar: track always, thumb only when the list scrolls
        sx = x + w - self.sb_w
        surface.fill_rect(sx + 1, y + self.y0, 2, self.list_h,
                          self.gs_track)
        if n > self.rows:
            th = self.list_h * self.rows // n
            if th < 6:
                th = 6
            ty = y + self.y0 + top_px * (self.list_h - th) \
                // ((n - self.rows) * self.pitch)
            surface.fill_rect(sx, ty, self.sb_w, th, self.gs_thumb)
