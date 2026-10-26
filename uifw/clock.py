"""clock.py: the shared time kernel — wrap-safe delta + a tiny
scheduler (PLAN-app-layer P1).

tdiff(now, then): utime.ticks_diff semantics (2^30 window).
Canonical home: input.py imports it as _tdiff (compat shim, the
events.py pattern) — do not add new importers of input._tdiff.
Raw `now - then` goes negative at the ticks_ms wrap (~12.3 days)
and every timer built on it stalls DEAD until now climbs back —
a boiler controller does NOT reboot.

Sched: app-paced timers on the app's own clock — the RTOS
periodic-task pattern: the app declares WORK AT A PERIOD (task) and
ONE-SHOT ALARMS (once); the loop's clock runs them. The app passes
its loop's now (utime.ticks_ms()) to tick(); this module never
imports utime — pure ints in, so host tests and wrap-safety come
free (all deltas go through tdiff):

    s = Sched()
    t1 = s.once(5000, ignite)         # one-shot alarm
    t2 = s.task(1000, blink)          # periodic task
    t2.cancel()                       # anytime, even from another
                                      # timer's callback
    while True:
        now = ticks_ms()
        ...
        s.tick(now)                   # fires everything due, in due
                                      # order; returns the count

(Naming: task/once are APP work at a period - they never pace the
display: that is the loop's own frame_ms. t for visuals, sched for
behavior.)

Arming: once/task take effect relative to the FIRST tick() after
they are created — the app's clock is the only clock, so a timer
created between ticks arms at the next one (never early, never late
by more than a loop period). A missed period fires ONCE: a missed
poll wants "now", not a backlog; the next due is advanced past the
gap arithmetically (a huge gap costs no loop). Callback exceptions
propagate — the app's loop decides what a failing timer means;
timer state (one-shot spent / next due) advances BEFORE the call,
so a raising callback cannot storm the loop.

Delays and gaps must fit the tdiff window (|delta| < 2^29 ms =
148.6 h, 6.2 days) — the same honest bound input.py's timers have
always carried; beyond it a delta reads with the wrong sign, by
design. Every elapsed-time consumer in the engine obeys it: a
timestamp is ONLY ever paired with another through tdiff, never with
a raw subtraction (the P3 rule, tests/test_wrap.py).

ONE function, BOTH clock domains: tdiff is the arithmetic of
utime.ticks_diff, whose window is 2^30 whatever the counter is. The
scene/menu clock (ticks_ms) and the stats clock (ticks_us) therefore
take the SAME function: a port that masks its counter at 2^32 is
still exact, because -2^32 is a whole multiple of 2^30 and the
modulo hides the borrow either way. What binds is the half-range per
domain: 2^29 ms (6.2 days) for the ms clock, 2^29 us (8.9 min) for
the us clock — never time a longer interval with one delta. Absolute
animation PHASE (a cursor blink, a sine argument) is NOT an elapsed
delta and does not go through tdiff: see the continuity contract in
README "Clock domains".
"""

_T_HALF = 1 << 29
_T_MASK = (1 << 30) - 1


def tdiff(now, then):
    return ((now - then + _T_HALF) & _T_MASK) - _T_HALF


class _Timer:
    """Handle returned by Sched.once/task: .cancel() any time.
    [delay, period, fn, due, alive] — due is an absolute ticks
    value, compared ONLY through tdiff (never raw <, >)."""

    def __init__(self, delay, period, fn):
        self.delay = delay
        self.period = period              # 0 = one-shot
        self.fn = fn
        self.due = None                   # armed at the first tick
        self.alive = True

    def cancel(self):
        self.alive = False


class Sched:
    """once/task/cancel/tick — see the module docstring."""

    def __init__(self):
        self.timers = []

    def once(self, ms, fn):
        """One-shot alarm: fires ms after the next tick(). Returns
        the handle."""
        if ms < 0:
            raise ValueError('once: ms >= 0 required')
        t = _Timer(ms, 0, fn)
        self.timers.append(t)
        return t

    def task(self, ms, fn):
        """Periodic task: first fire ms after the next tick(), then
        every ms. Missed periods collapse to ONE fire."""
        if ms <= 0:
            raise ValueError('task: ms > 0 required')
        t = _Timer(ms, ms, fn)
        self.timers.append(t)
        return t

    def tick(self, now):
        """Fire everything due at now, in due order (earliest due
        first). Returns the number of callbacks run."""
        if not self.timers:
            return 0                      # the common case: zero work
        # arm + find due (no allocation while nothing is due)
        n = 0
        dead = False
        for t in self.timers:
            if not t.alive:
                dead = True
                continue
            if t.due is None:
                t.due = now + t.delay
            if tdiff(now, t.due) >= 0:
                n += 1
        if not n:
            if dead:
                self._prune()             # cancelled timers must not
            return 0                      # linger until the next fire
        due = [t for t in self.timers
               if t.alive and t.due is not None
               and tdiff(now, t.due) >= 0]
        # earliest due first: most overdue = largest tdiff(now, due)
        due.sort(key=lambda t: tdiff(now, t.due), reverse=True)
        fired = 0
        for t in due:
            if not t.alive:               # cancelled by an earlier
                continue                  # callback this same tick
            if t.period:
                # advance past now arithmetically: a huge gap (deep
                # sleep, clock jump) fires ONCE, no catch-up loop
                d = tdiff(now, t.due)
                t.due += t.period * (d // t.period + 1)
            else:
                t.alive = False           # one-shot spent BEFORE the
            t.fn()                        # call: a raise cannot
            fired += 1                    # re-fire storm
        self._prune()
        return fired

    def _prune(self):
        """Drop spent/cancelled timers; only rewrites when needed."""
        timers = self.timers
        for i in range(len(timers) - 1, -1, -1):
            if not timers[i].alive:
                del timers[i]
