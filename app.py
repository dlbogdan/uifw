"""App: the OPT-IN app layer - bring-up, heartbeat, routing, policies.

The kernel (surface -> swapchain -> scene) owns ZERO policy and that
stays true: this module COMPOSES the kernel, it never wraps it in
hidden behavior. Every kernel object stays directly reachable -
app.display, app.chain, app.scene, app.q, app.sources - composition,
not encapsulation. Importing this module is the app's choice
(demo-tour.py stays raw: the kernel showcase, the counter-example).

The app still owns the loop (D9): step(now) is ONE heartbeat,
byte-faithful to the README "one app style" - poll every source,
drain + route, frame, sleep the remainder (drew or not: a skipped
tick costs ~1 us less, never a different cadence). The app's loop:

    app = App(spi_sck=18, spi_mosi=19,          # SPI0 @ 25 Mbaud (defaults)
              cs=17, dc=20, rst=21,
              partial=True,                     # render-on-change
              frame_ms=16.666,
              encoder=(2, 3),                   # GP2/3 rotary
              buttons=((4, OK, {'release': True,
                                'long': (LONG_OK, 1200)}),
                       (5, BACK, {'release': True,
                                  'long': (LONG_BACK, 1200)}),
                       (6, SOFT1)))
    app.focus = menu
    while True:
        now = ticks_ms()
        ...the app's own work, at its own rate...
        app.step(now)         # poll, route, sched, frame - NEVER
        app.pace(now)         #   sleeps; the README remainder
                              #   sleep, called (or skipped) by the app

There is deliberately NO run(), and step() never sleeps: the app's
main loop is the app's, and it may carry any other work of the
project between ticks at ANY rate - the scaffold cannot throttle
it (owner decision after P5; D9 made literal: even the opt-in
layer does not own the loop, it hands you one tick-wide primitive
and lets you pace it).

Routing: app.focus (e.g. the Menu) gets every drained event's
on_event; with no focus, app.on_event(ev) is the fallback. The
panel-summon block and friends stay APP policy: write them in the
fallback or wrap focus - the scaffold removes the poll/drain/
plumbing repetition, nothing else.

Policies are opt-in (None default, zero cost when off):
    idle_dim(idle_ms, full, dim)  hardware dim on idle, back on input
    stat(period_ms)               the README playbook stats line
    app.sched = clock.Sched()     once/task ticked inside step()

NOTE: imports machine/utime at module level - on the host this
module needs picosim.hw installed first (the accepted pattern of
the swapchain/present tests).
"""
from machine import Pin, SPI  # type: ignore
from utime import ticks_ms, ticks_diff, ticks_us, sleep_ms  # type: ignore
from .ssd1322 import Display
from .swapchain import SwapChain
from .scene import Scene
from .input import EventQ, Encoder, Buttons
from .clock import tdiff
import gc


def _pin(p):
    """cfg pin NUMBERS become machine.Pin; objects (sim fakes,
    expander shims) pass through - the duck-typing rule input.py
    already follows."""
    return p if hasattr(p, 'value') else Pin(p)


class DictStore:
    """Store protocol (menu.py) over a plain dict: the app's CFG
    becomes a config store in one line - menu.load_store(DictStore(CFG))
    seeds, menu.bind_store(DictStore(CFG)) persists."""

    def __init__(self, d):
        self.d = d

    def keys(self):
        return self.d.keys()

    def load(self, iid):
        return self.d.get(iid)

    def save(self, iid, value):
        self.d[iid] = value


class StatLine:
    """The README playbook stats line: accumulates step verdicts +
    work us, prints when due. The format is FIXED by the README -
    the on-board acceptance compares against it (pinned in
    tests/test_app). SCOPE: the work us come from the scene.frame()
    window only (render + present handoff) - event poll/route,
    sched callbacks and the app's own loop work are NOT measured;
    fps counts step ticks (full-loop cadence), cpu = scene work /
    elapsed, free mem = gc.mem_free() (A5: label it for what it
    measures)."""

    def __init__(self, period_ms, sink=None):
        self.period = period_ms
        self.sink = sink if sink is not None else print
        self.frames = 0
        self.skip = 0
        self.sum = 0
        self.min = 1 << 30
        self.max = 0
        self.t0 = None

    def work(self, us, drew, now):
        if self.t0 is None:
            self.t0 = now
        self.frames += 1
        if not drew:
            self.skip += 1
        self.sum += us
        if us < self.min:
            self.min = us
        if us > self.max:
            self.max = us
        span = tdiff(now, self.t0)
        if span < self.period:
            return
        self.sink('scene %.2f fps | work avg %d us (min %d max %d) | '
                  'skipped %d | cpu %d%% | free mem %d'
                  % (self.frames * 1000.0 / span, self.sum // self.frames,
                     self.min, self.max, self.skip,
                     100 * self.sum // (span * 1000), gc.mem_free()))
        self.frames = 0
        self.skip = 0
        self.sum = 0
        self.min = 1 << 30
        self.max = 0
        self.t0 = now


class _IdleDim:
    """idle_dim policy state: last input time + current lamp side."""

    def __init__(self, idle_ms, full, dim):
        self.idle_ms, self.full, self.dim = idle_ms, full, dim
        self.last = None
        self.on = False           # True = currently dimmed


class App:
    """Bring-up + heartbeat + routing + opt-in policies.

    Args (NAMED - there is no cfg dict):
        spi_id (int), spi_baud (int): SPI bus, default 0 @ 25 000 000.
        spi_sck, spi_mosi (int): SPI pins, REQUIRED.
        cs, dc, rst (int): display control pins, REQUIRED.
        contrast, brightness (int): panel init levels (0x9F / 0x0F).
        async_double (bool), partial (bool): SwapChain knobs.
        fps (int): target heartbeat: the panel (cmd 0xB3) is set
            to refresh_mult x fps, the closest achievable rate, and
            the heartbeat then snaps to achieved/mult - the app's
            real fps divides the panel's real Hz EXACTLY (60, mult 2
            -> panel 109.8 -> app 54.9). The panel out-runs the
            content: ~55 Hz refresh flickers on-panel, ~110 Hz is
            clean. Wins over frame_ms.
        refresh_mult (int): panel rate = mult x fps (default 2)
        frame_ms (float): the ONE heartbeat rate (default 16.666)
        encoder (tuple): (a, b[, sw]) rotary pins -> UP/DOWN[/OK]
        buttons (tuple): ((pin, ev[, opts]), ...) -> input.Buttons
        Pin numbers are wrapped in machine.Pin; objects pass.
        No encoder/buttons: no sources built - app.sources stays
        open for anything with poll(now) (Scripted, expanders).

    Ctor failure (S1): a bad rate knob raises ValueError BEFORE the
    bus opens; a failure after that releases everything acquired
    (presenter stopped, panel cleaned per chain.close's ack) before
    re-raising - a failed App() never leaves an orphan worker or a
    powered panel nobody can close.

    Members (public on purpose - composition, not encapsulation):
        display, chain, scene, q, sources, frame_ms, fps, refresh_hz,
        focus (widget with on_event), on_event (fallback fn(ev)),
        sched (assign a clock.Sched to enable).
    """

    def __init__(self, spi_id=0, spi_baud=25000000, spi_sck=None,
                 spi_mosi=None, cs=None, dc=None, rst=None,
                 contrast=0x9F, brightness=0x0F,
                 async_double=True, partial=False,
                 fps=None, refresh_mult=2, frame_ms=16.666,
                 encoder=None, buttons=None):
        if spi_sck is None or spi_mosi is None:
            raise ValueError('App: spi_sck and spi_mosi are required')
        if cs is None or dc is None or rst is None:
            raise ValueError('App: cs, dc and rst are required')
        # S1 (PLAN-review-hardening, A8): the rate knobs are validated
        # BEFORE any hardware is acquired - a typo in fps must never be
        # paid for with a live presenter and an initialized panel.
        if fps is not None and not (isinstance(fps, (int, float))
                                    and fps > 0):
            raise ValueError('App: fps must be a positive number')
        if not (isinstance(refresh_mult, (int, float))
                and refresh_mult > 0):
            raise ValueError('App: refresh_mult must be a positive number')
        if fps is None and not (isinstance(frame_ms, (int, float))
                                and frame_ms > 0):
            raise ValueError('App: frame_ms must be a positive number')
        spi = SPI(spi_id, baudrate=spi_baud,
                  sck=_pin(spi_sck), mosi=_pin(spi_mosi))
        # S1 rollback: the presenter starts INSIDE SwapChain.__init__,
        # so from that line on a raising ctor would orphan the worker
        # on a dict no object can reach. Any failure from the SPI line
        # out releases every resource acquired SUCCESSFULLY, in
        # shutdown order: worker first (chain.close's exit ack), only
        # then the panel (cleanup -> sleep + SPI deinit), else just
        # the bus. The original exception still propagates. If the
        # worker does NOT ack, the panel is deliberately left powered:
        # half a cleanup would race the live wire (the P1 rule), and
        # the raised ctor IS the caller's signal.
        display = chain = None
        try:
            display = self.display = Display(
                spi, cs=_pin(cs), dc=_pin(dc), rst=_pin(rst),
                contrast=contrast, brightness=brightness)
            chain = self.chain = SwapChain(display, async_double, partial)
            self.scene = Scene(chain)
            self.q = EventQ()
            self.sources = []
            if encoder is not None:
                self.sources.append(Encoder(self.q,
                                            *[_pin(p) for p in encoder]))
            if buttons is not None:
                self.sources.append(Buttons(
                    self.q, tuple(((_pin(b[0]),) + tuple(b[1:]))
                                  for b in buttons)))
            if fps is not None:
                # Locked step: the panel gets the closest achievable
                # rate to fps x refresh_mult (on-panel sweep: ~55 Hz
                # flickers, ~110 Hz clean - the panel deliberately
                # out-runs the content), then the heartbeat SNAPS to
                # achieved/mult so the app's real fps divides the
                # panel's real Hz exactly (fps=60, mult=2 -> panel
                # 109.8 Hz -> app 54.9, never a beat between wire
                # dumps and panel scans).
                mult = refresh_mult
                self.refresh_hz = display.set_refresh(fps * mult)
                self.frame_ms = 1000.0 * mult / self.refresh_hz
            else:
                self.refresh_hz = display.refresh_hz
                self.frame_ms = frame_ms
            self.fps = 1000.0 / self.frame_ms
            self.focus = None     # widget with on_event (e.g. Menu)
            self.on_event = None  # fallback fn(ev) when no focus
            self.sched = None     # assign a clock.Sched to enable
            self._idle = None
            self._stat = None
            self._gc_t = None     # last gc-threshold re-pin tick
            self._closed = False
        except BaseException:
            if chain is not None:
                if chain.close():
                    display.cleanup()
            elif display is not None:
                display.cleanup()
            else:
                spi.deinit()
            raise

    # ---- opt-in policies -------------------------------------------------

    def idle_dim(self, idle_ms, full=0x0F, dim=0x06):
        """Hardware dim (0xC7 master current) after no input for
        idle_ms, back to full on ANY event - demo-ui's policy, moved
        here once. Wrap-safe via clock.tdiff; zero cost when unset."""
        self._idle = _IdleDim(idle_ms, full, dim)

    def stat(self, period_ms, sink=None):
        """Print the README playbook stats line every period_ms,
        from step()'s own verdicts + work us. sink(line) injects the
        output path: pass lib.coresys.logger.info (or any fn(str)) to
        route it into the framework log instead of the serial console
        (D7 alignment); default sink is print."""
        self._stat = StatLine(period_ms, sink)

    # ---- the heartbeat ----------------------------------------------------

    def step(self, now):
        """One display tick: poll every source, drain + route, tick
        the sched, frame. NEVER sleeps - pacing is the app's call
        (see pace()): background work of the project runs at its
        own rate and step cannot throttle it. Returns frame()'s
        drew/skip verdict."""
        # GC doctrine, inherited from the old scene.run player
        # (d8214a2 lost it): collect only when ~16 KB of new garbage
        # piles up - shallow heap-free sawtooth, short collections.
        # RE-PINNED periodically because the live set grows (font
        # warm, lazy caches) and a stale threshold collects too
        # often; both calls O(1), NEVER forces a collection.
        if self._gc_t is None or tdiff(now, self._gc_t) >= 2000:
            self._gc_t = now
            gc.threshold(gc.mem_alloc() + 16 * 1024)
        for src in self.sources:
            src.poll(now)
        evs = self.q.drain()
        if evs:
            self.scene.invalidate()       # immediacy only: the recheck
            if self._idle is not None:    # reacts within T
                self._idle.last = now
                if self._idle.on:
                    self.display.set_brightness(self._idle.full)
                    self._idle.on = False
            focus = self.focus
            fb = self.on_event
            for ev in evs:
                if focus is not None and hasattr(focus, 'on_event'):
                    focus.on_event(ev)
                elif fb is not None:
                    fb(ev)
        if self.sched is not None and self.sched.tick(now):
            self.scene.invalidate()       # a fired timer is an event
        if self._idle is not None:
            idle = self._idle
            if idle.last is None:
                idle.last = now
            elif not idle.on and tdiff(now, idle.last) > idle.idle_ms:
                self.display.set_brightness(idle.dim)
                idle.on = True
        if self._stat is not None:
            w0 = ticks_us()
            drew = self.scene.frame(now)
            # the US domain needs a delta too: ticks_us wraps, and a
            # raw subtraction inside the measured window hands
            # StatLine a negative work (min/avg/cpu all wrong, once
            # every ~2^29 us of runtime). clock.tdiff is the same
            # arithmetic as utime.ticks_diff: exact for both the
            # 2^30 and 2^32 counter masks (clock.py)
            self._stat.work(tdiff(ticks_us(), w0), drew, now)
        else:
            drew = self.scene.frame(now)
        return drew

    def pace(self, now):
        """The README heartbeat's sleep, as the app's OWN call:
        top the tick up to frame_ms (int()-safe: sleep_ms is
        int-only, frame_ms may be float). Call it when the loop
        has nothing left to do - one rate, drew or not (a skipped
        frame costs ~1 us less, never a different cadence). Skip
        it when real work fills the loop: the display then frames
        at the work's rate (quiescent frames are ~1 us, so an
        over-paced loop stays cheap on a still screen)."""
        rem = self.frame_ms - ticks_diff(ticks_ms(), now)
        if rem > 0:
            sleep_ms(int(rem))

    def close(self):
        """The demos' finally-block, once: chain.close() then
        display.cleanup(). Repeat calls are harmless (the flag).

        Ordered shutdown: the display is cleaned up ONLY after the
        presenter's exit ack, so SPI deinit can never race a live
        transfer. If the worker does not acknowledge in time,
        NOTHING is cleaned up and close() returns False: the app
        stays open and close() stays retryable, instead of marking
        itself closed on a half-done shutdown. True = both done."""
        if self._closed:
            return True
        if not self.chain.close():
            return False        # worker still owns the wire
        self.display.cleanup()
        self._closed = True
        return True
