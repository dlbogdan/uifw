"""Scene: composition of renderables presented through a SwapChain.

A renderable is anything with draw(surface, t): a VectorRenderer, a UI
widget (text, menu, icons), a group of driver draw calls. Add
order = z-order.

The app owns the loop: call frame(t) from it. Input polling, frame
budget, idle policy and the exit path (chain.close(), then
display.cleanup()) live in the app, not here.

Render-on-change: with a partial=True chain, frame() learns from the
chain's diff verdict - the ONE sensor. An empty verdict (nothing went
over the wire) puts the scene in quiescent mode: no clear, no draw,
no present until an invalidate() or a recheck is due. Rechecks are
synchronized with the loop rate: 1, 2, 4, 8 frame periods after the
last draw, then the recheck_ms cap. Motion needs no cooperation: a
non-empty diff keeps the scene at full rate by itself. The scene
reads exactly two inputs - the chain's verdict and the clock t -
never anything a renderable declares. Quiescence REQUIRES a
partial chain: no diff, no quiescence (the scene stays full-rate).

The clock unit is INT MILLISECONDS (Phase 8): frame(t) takes the
same ticks_ms() value the app already passes to input poll and the
scheduler, and hands it unchanged to every draw(surface, t). One
unit at the whole API boundary - no * 0.001 at call sites, and the
quiescence arithmetic is exact int.

The ticks_ms counter WRAPS (2^30 ms, 12.4 days on the supported
port). Every gap this module computes goes through clock.tdiff, so a
wrap is just another frame boundary: a quiescent scene keeps its
recheck ladder across it, and the learned loop period stays the real
loop period. Pure-Python module: clock.py imports no hardware, so
this stays host-testable (tests/test_wrap.py).
"""
from .clock import tdiff


class Scene:
    """Ordered composition of renderables.

    Args:
        chain (SwapChain): double-buffered swap chain instance.
        frame_ms (int): the app's loop period in ms (33 = 30 fps).
            Optional: when unset the scene LEARNS it from t between
            awake frames - the clock is already an input, nothing
            new to declare. (Same name, same quantity as the app
            layer's 'frame_ms' cfg key.)

    Knobs:
        recheck_ms (int): max trust bound in ms (default 100).
            A quiescent scene rechecks at 1/2/4/8 frame periods,
            then never waits longer than recheck_ms. The bound is
            on the SCENE's side: a change lands within recheck_ms
            PLUS one loop period, because nothing is sampled until
            the app calls frame() again (frame_ms=33, recheck_ms=100
            -> 133 ms, never "100 ms" as a stand-alone promise).
        dirty (bool): app wake-up flag - set it with invalidate(),
            never for correctness (the rechecks are the safety net).
    """

    def __init__(self, chain, frame_ms=None):
        self.chain = chain
        self.items = []
        self.post = None  # optional post(surface) pass, before present
        self.recheck_ms = 100
        self.frame_ms = frame_ms
        self.dirty = True
        self._quiescent = False
        self._last_draw = None
        self._dt = 0                      # learned loop period (ms)
        self._quiet = 0                   # recheck back-off stage

    def add(self, item):
        """Append a renderable (drawn last = on top)."""
        self.items.append(item)
        self.invalidate()
        return item

    def remove(self, item):
        self.items.remove(item)
        self.invalidate()

    def invalidate(self):
        """App wake-up: force the next frame() to draw. Immediacy
        only (input response) - without it the rechecks still catch
        every change within recheck_ms plus one loop period."""
        self.dirty = True

    def frame(self, t):
        """One frame: clear, render all visible items, post, present.

        t (int): milliseconds - the app's ticks_ms() clock, the same
        unit input poll and the scheduler take; passed unchanged to
        every item's draw(surface, t).

        Returns True if it drew, False if the frame was skipped
        (quiescent scene: the panel already shows this screen, and
        the app may sleep longer - input polling stays every loop).
        """
        # int-ms clock: an exact-T gap is exactly T, rechecks fire
        # AT T - no epsilon (the float-seconds era needed one).
        # tdiff, never a raw subtraction: the counter wraps at
        # 2^30 ms and a raw gap goes negative - a quiescent scene
        # would then sit out the whole wrap (P3, test_wrap)
        if self._quiescent and not self.dirty \
                and tdiff(t, self._last_draw) < self._wait():
            return False
        self.dirty = False
        if self._last_draw is not None and not self._quiescent:
            # awake back-to-back frames = the real loop period
            dt = tdiff(t, self._last_draw)
            if 0 < dt < self.recheck_ms:
                self._dt = dt if self._dt == 0.0 \
                    else 0.75 * self._dt + 0.25 * dt
        chain = self.chain
        chain.clear()
        surface = chain.back_buffer
        for item in self.items:
            if getattr(item, 'visible', True):
                item.draw(surface, t)
        if self.post is not None:
            self.post(surface)
        self._last_draw = t
        # the chain's verdict is the ONLY quiescence sensor: one
        # diff per frame, computed inside present(), never a second
        # scan here (-1 = nothing went over the wire)
        verdict = chain.present()
        if verdict < 0:
            self._quiescent = True
            self._quiet += 1
        else:
            self._quiescent = False
            self._quiet = 0
        return True

    def _wait(self):
        """Escalating recheck, synced to the loop: 1, 2, 4, 8 frame
        periods after the last draw, capped at recheck_ms.

        ONE empty diff is weak evidence of stillness - slow motion
        (the flame's sub-pixel drift) rasterizes identically for a
        frame or two - so a scene that just went quiet is re-measured
        on the NEXT frame: a wrong quiescence costs one frame, not
        up to recheck_ms (that fixed-T version froze resumed motion
        and juddered the flame). Trust grows only while rechecks
        keep coming back empty; static screens settle to the
        recheck_ms cadence and cost ~0. Learned dt, or the app's
        frame_ms; clock only."""
        dt = self.frame_ms if self.frame_ms else self._dt
        if dt <= 0:
            return self.recheck_ms        # nothing learned yet
        q = self._quiet - 1
        w = dt * (1, 2, 4, 8)[q if q < 4 else 3] \
            if self._quiet else dt
        return w if w < self.recheck_ms else self.recheck_ms
