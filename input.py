"""Input: event queue + poll-driven sources -> the menu event vocabulary.

Sources never touch the menu: they push small-int events (the
vocabulary events.py defines: UP, DOWN, OK, BACK, SOFT1, LONG_BACK,
LONG_OK) into a shared EventQ. The app polls every source once per
loop, then routes
q.drain() wherever it likes:

    q = EventQ()
    enc = Encoder(q, Pin(2), Pin(3), Pin(4))     # A, B, push
    btn = Buttons(q, ((Pin(5), BACK), (Pin(6), SOFT1)))
    while True:
        now = ticks_ms()
        enc.poll(now); btn.poll(now)
        for ev in q.drain():
            menu.on_event(ev)

Hardware-free by duck-typing: sources take pin objects with .value()
- real machine.Pin, sim fakes, or an MCP23017/MCP23008 reader shim
(an expander source is just another poll() reading i2c instead of
GPIO and pushing the same events; a rotary on expander pins works
as-is). poll(now) takes utime.ticks_ms(); ALL internal deltas are
wrap-safe (2^30 window, ticks_diff semantics) - a boiler
controller runs for weeks and must not go button-dead at the wrap.
"""
from .events import UP, DOWN, OK, BACK, SOFT1, LONG_BACK, LONG_OK  # noqa: F401 (re-exported)

# Wrap-safe time delta (utime.ticks_diff semantics, 2^30 window):
# canonical home is clock.py (the shared time kernel, PLAN-app-layer
# P1); imported under the old private name as a COMPAT SHIM for the
# existing call sites below. Sources stay hardware-free (no utime
# import) yet survive the ticks_ms wrap (~12.3 days - a boiler
# controller does NOT reboot). Do not add new importers of
# input._tdiff: import clock.
from .clock import tdiff as _tdiff


class EventQ:
    """Bounded FIFO of event ints: push never blocks, drops overflow."""

    def __init__(self, max_len=32):
        self.q = []
        self.max_len = max_len

    def push(self, ev):
        if len(self.q) < self.max_len:
            self.q.append(ev)

    def drain(self):
        """Take all queued events (returns list, queue left empty)."""
        q = self.q
        self.q = []
        return q


class Buttons:
    """GPIO buttons -> events: debounce + auto-repeat on held keys.

    Poll at <= debounce/2 ms for crisp edges. Buttons-only config:
    map UP/DOWN here too - repeat makes held keys scroll.

    DEBOUNCE MEANING (C1): the level must SETTLE - stay - for
    debounce ms before the state changes. The settle clock starts at
    the FIRST POLL that observes the new level (the poller cannot know
    when the metal actually moved), so acceptance lands at
    debounce + one poll interval after the edge. Before C1 the gate
    timed from the last ACCEPTED transition instead, which inverted
    the rule: a 1 ms glitch fired an event at every poll rate measured
    (1, 5, 10 ms) as long as the previous accepted edge was older
    than debounce, and two legitimate 40 ms taps 40 ms apart produced
    ONE event - the second swallowed. One clock for the CANDIDATE raw
    level (b[9]/b[10]) and one for the ACCEPTED state (b[2]/b[3]) is
    what makes the window a window.

    Per-button gesture options (3rd tuple element, dict):
        release (bool): fire ev on RELEASE instead of press (tap).
        long ((ev, ms)): held past ms -> fire ev once per hold;
            suppresses the release-fire and disables repeat for
            that button. Combine with release for a clean split:
            tap = ev, hold = long ev, nothing destructive in between.
        The long ev is ANY vocabulary code, not a fixed one: the
        reference wiring holds OK -> LONG_OK (editors commit) and
        BACK -> LONG_BACK (editors discard). Encoder has no gesture
        options: a key that needs a hold belongs on Buttons, not on
        the rotary's push pin.
    A button held at boot counts as "held since t=0" for long timers.

    Args:
        q (EventQ): destination queue.
        mapping (tuple): ((pin, ev[, opts]), ...); pins duck-typed
            .value().
        debounce (int): ms the level must settle to count.
        repeat_first (int): ms held before auto-repeat starts.
        repeat (int): ms between repeats (0 = no repeat).
        active_low (bool): button pulls to GND / pull-up: True.
    """

    def __init__(self, q, mapping, debounce=30, repeat_first=400,
                 repeat=150, active_low=True):
        self.q = q
        self.debounce = debounce
        self.repeat_first = repeat_first
        self.repeat = repeat
        self.active_low = active_low
        # per button: [pin, ev, state, last_change_ms, last_fire_ms,
        # release, long_ev, long_ms, long_fired, cand, cand_t].
        # b[2]/b[3] are the ACCEPTED state and its edge; b[9]/b[10] the
        # CANDIDATE raw level and when a poll first saw it (C1).
        # Seed state from the pins: a button held at boot is already
        # pressed and must NOT fire a phantom event on first poll
        # (unseeded 0 + last_change 0 means "released, settled").
        self.btns = []
        for entry in mapping:
            opts = entry[2] if len(entry) > 2 else {}
            long_ev, long_ms = opts.get('long', (None, 0))
            seed = 1 - entry[0].value() if active_low else entry[0].value()
            self.btns.append(
                [entry[0], entry[1], seed,
                 0, 0, bool(opts.get('release')), long_ev, long_ms,
                 False, seed, 0])

    def poll(self, now):
        for b in self.btns:
            raw = b[0].value()
            v = 1 - raw if self.active_low else raw
            if v != b[9]:
                # candidate level: its settle clock STARTS here (C1).
                # Nothing is decided from it until it holds still for
                # debounce ms.
                b[9] = v
                b[10] = now
            elif v != b[2] and _tdiff(now, b[10]) >= self.debounce:
                # settled: the candidate becomes the accepted state
                b[2] = v
                b[3] = now
                if v:
                    b[8] = False       # new hold: re-arm long
                    if not b[5]:
                        self.q.push(b[1])
                        b[4] = now
                elif b[5] and not b[8]:
                    self.q.push(b[1])  # tap: release-fire
            elif v and b[2]:
                # long/repeat run off the ACCEPTED hold only (b[2]): a
                # candidate that has not settled must not earn a repeat.
                if b[6] is not None:
                    if not b[8] and _tdiff(now, b[3]) >= b[7]:
                        b[8] = True        # once per hold
                        self.q.push(b[6])
                elif self.repeat and _tdiff(now, b[3]) >= self.repeat_first \
                        and _tdiff(now, b[4]) >= self.repeat:
                    self.q.push(b[1])
                    b[4] = now


class Encoder:
    """Quadrature rotary -> UP/DOWN, debounced push button -> OK.

    Full detent = 4 quadrature transitions on most detent encoders;
    the decoder accumulates valid transitions and emits one event per
    steps_per_detent (set 2 for half-step encoders). Rotation CW =
    DOWN the list (invert=True to flip).

    Args:
        q (EventQ): destination queue.
        a, b: phase pins (duck-typed .value()).
        sw: push pin, pull-up (None = no push button).
        invert (bool): flip rotation direction.
        steps_per_detent (int): quadrature steps per click.
        debounce (int): ms, push button. Same settle rule as Buttons
            (C1): the level must stay put for debounce ms, clocked from
            the first poll that sees it. Quadrature transitions are
            NOT debounced this way - detent counting is the filter
            there (steps_per_detent), and a 1 ms push glitch no longer
            fires OK.
    """

    # delta from state(last)*4 + state(now), states = (a<<1)|b
    D = (0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0)

    def __init__(self, q, a, b, sw=None, invert=False,
                 steps_per_detent=4, debounce=30):
        self.q = q
        self.a, self.b, self.sw = a, b, sw
        self.invert = -1 if invert else 1
        self.spd = steps_per_detent
        self.debounce = debounce
        self.last = (a.value() << 1) | b.value()
        self.acc = 0
        # seed push state from the pin: held at boot must not fire OK
        self.sw_state = 1 - sw.value() if sw is not None else 0
        self.sw_t = 0
        # candidate level + its settle clock (C1, same rule as Buttons)
        self.sw_cand = self.sw_state
        self.sw_cand_t = 0

    def poll(self, now):
        s = (self.a.value() << 1) | self.b.value()
        if s != self.last:
            d = Encoder.D[self.last * 4 + s]
            self.last = s
            if d:
                self.acc += d * self.invert
                if self.acc >= self.spd:
                    self.acc = 0
                    self.q.push(DOWN)
                elif self.acc <= -self.spd:
                    self.acc = 0
                    self.q.push(UP)
        if self.sw is not None:
            v = 1 - self.sw.value()          # pull-up: pressed = 0
            if v != self.sw_cand:
                self.sw_cand = v
                self.sw_cand_t = now
            elif v != self.sw_state and \
                    _tdiff(now, self.sw_cand_t) >= self.debounce:
                self.sw_state = v
                self.sw_t = now
                if v:
                    self.q.push(OK)


class Scripted:
    """Replay a canned [(delay_ms, ev), ...] script - demos/sims until
    real buttons exist. Delays are relative (accumulate from first
    poll); loop=True restarts the script when done."""

    def __init__(self, q, script, loop=False):
        self.q = q
        self.evs = [ev for _, ev in script]
        self.times = []
        t = 0
        for d, _ in script:
            t += d
            self.times.append(t)
        self.loop = loop
        self.i = 0
        self.t0 = None

    def poll(self, now):
        n = len(self.evs)
        if self.i >= n and not self.loop:
            return
        if self.t0 is None:
            self.t0 = now
        while self.i < n and _tdiff(now, self.t0) >= self.times[self.i]:
            self.q.push(self.evs[self.i])
            self.i += 1
        if self.i >= n and self.loop:
            self.i = 0
            self.t0 = now
