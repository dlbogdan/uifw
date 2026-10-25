"""Swap chain: owns the back buffers, presents them to the panel.

GPU-standard naming (DirectX): a SwapChain holds a chain of back
buffers; you draw into the current back_buffer, then Present() sends
it to the display and moves to the next buffer. async_double=True
(the default) runs the SPI transfer on core 1 while core 0 draws the
next frame (pattern proven on-board; old bench.py in git history).
Cost: +8 KB second surface,
+8 KB thread stack.

partial=True adds render-on-change: before the handoff the back
buffer is diffed against the last-presented buffer, only the dirty
ROW SPAN goes over SPI, and an identical frame sends nothing at all
(no swap either - the panel already shows it, so the buffers stay
put; that is what keeps the diff target valid). async_double=True
diffs against the other surface (+0 RAM); async_double=False keeps a
private prev copy (+8 KB, opt-in only). present() returns the last
row sent, or -1 when the frame was identical and nothing went out.

SYNCHRONISATION (W2): submitted is not confirmed. A failed transfer
leaves pixels on a surface the panel never received, so from that
instant the diff target no longer describes the panel. The chain
goes OUT OF SYNC (chain.in_sync False) and every later present()
sends the WHOLE current frame until one whole-frame transfer
lands - an identical desired image never suppresses the repair, and
a partial span of a desynced frame is never trusted. The sync
verdict is published by the WORKER at transfer end, which is what
makes a failure that surfaces AFTER the next frame was published
safe. Initial baseline: Display.__init__ ends with blank()
(ssd1322.py:222) and both surfaces start zeroed, so a new chain
starts IN SYNC with a known-blank panel; the assumption is that
nothing else wrote to the panel between Display() and SwapChain().
Cost: one 8 KB frame per fault, not per frame.

Naming: async_double is the honest axis - it asks WHO ABSORBS THE
SPI TIME, your loop or a core-1 thread. The buffer count (2 vs 1)
is a consequence, not the choice.

Lifecycle: present() returns at CLAIM - the instant core 1 has
taken the WHOLE frame descriptor (bytes + rows as one value); the
transfer itself finishes on core 1. The completion edge is public:
wait_idle() blocks until the panel verifiably holds every claimed
frame. A failed transfer never hangs present() and is readable as
chain.error (a frame whose present() returned is SUBMITTED, not
confirmed), and it puts the chain out of sync until a whole frame
lands (chain.in_sync). close() waits for the final transfer AND the
worker's exit acknowledgement and returns True only then -
display.cleanup() may only touch the SPI after a True return.
present() once the worker has stopped raises RuntimeError instead
of spinning.

Usage:
    chain = SwapChain(display, True)
    while True:
        chain.clear()
        renderer.draw(chain.back_buffer, t)  # draw into back buffer
        chain.present()                      # send to panel, swap
    ...
    if chain.close():                        # worker acked exit...
        display.cleanup()                    # ...only now is SPI free
"""
import _thread
import gc
from utime import sleep_us, sleep_ms  # type: ignore
from .surface import Surface


class SwapChain:
    """Double-buffered swap chain.

    Args:
        display (Display): SSD1322 panel instance (ssd1322.py).
        async_double (bool): True (default) = double buffering +
            ASYNC wire: a core-1 thread streams each frame over SPI
            while core 0 draws the next one - frame cost is the draw
            alone, the ~2.6 ms full-frame transfer is hidden.
            WARNING, RAM-hungry: +8 KB second surface and +8 KB
            thread stack, plus ~15% slower XIP reads on core 0.
            False = single buffer, present() blocks until the bytes
            land: saves 16 KB, pays the wire inline, no threads.
        partial (bool): True = render-on-change present (row-span
            diff, identical frames skipped). Default False: the
            classic full-frame wire, byte-identical.
    """

    def __init__(self, display, async_double=True, partial=False):
        self.d = display
        self.async_double = async_double
        self.partial = partial
        w = display.width
        h = display.height
        self.back_buffer = Surface(w, h)
        self._last_row = h - 1
        if not async_double:
            self._sync = 1              # W2: see in_sync
            if partial:
                # no second surface to diff against: keep the
                # last-presented pixels (+8 KB, opt-in only)
                self._prev = bytearray(self.back_buffer.buffer_length)
                self._prev_mv = memoryview(self._prev)
            return
        self.surfaces = [self.back_buffer, Surface(w, h)]
        self.idx = 0
        # Handoff protocol states and transitions (P1). One
        # producer (core 0, the thread that built the chain) and
        # one worker (core 1). All keys pre-allocated here: the
        # dict never resizes at runtime, so a store is a value
        # swap in a fixed slot.
        #   run     1 = worker keeps running; 0 = stop requested
        #   desc    pending frame descriptor (mv, y0, y1): ONE
        #           tuple in ONE slot, never read field-by-field
        #   ready   1 = desc pending; 0 = claimed - the transition
        #           ready 1->0 IS the claim ack present() waits on
        #   idle    1 = no transfer in flight; 0 = transferring
        #   sent    the claimed desc, pinned core-0-reachable for
        #           the whole transfer; None when idle
        #   stopped 1 = worker acknowledged ACTUAL thread exit
        #   err     first transfer/worker exception, else None
        #   sync    1 = the panel's contents are known (the diff
        #           target describes them); 0 = unknown, every
        #           later present() sends a WHOLE frame until a
        #           whole-frame transfer lands. Only the worker
        #           writes it, at TRANSFER END (see W2 above).
        # Publish (core 0): write desc, then ready=1.
        # Claim (core 1, atomic by construction): read desc in ONE
        # lookup, pin it in sent, publish busy (idle=0), and only
        # THEN ack (ready=0). No observer can ever see "claimed
        # but not busy", and the descriptor cannot split into
        # frame N's buffer with frame N+1's rows: the pre-P1 code
        # acked the claim BEFORE reading y0/y1, so a frame
        # published in that window paired the pinned buffer of
        # frame N with frame N+1's rows (silent pixel corruption;
        # PLAN-review-hardening.md P1, pinned by
        # tests/test_handoff.py). A single dict slot is a value
        # swap on both the GIL (host) and the GIL-off RP2 port, so
        # no lock primitive - whose acquire(timeout=...) is
        # unmeasured on the installed firmware - is needed.
        self.state = {'run': 1, 'ready': 0, 'desc': None, 'sent': None,
                      'idle': 1, 'stopped': 0, 'err': None, 'sync': 1}
        state = self.state
        p_mv = display.present_mv  # bind once: no alloc in presenter
        h1 = h - 1
        partial = self.partial

        def presenter():
            """Runs on core 1. Allocation-free: the claim is a
            single dict lookup on 'desc', and the transfer takes
            its bytes and rows from the claimed tuple, never
            re-read from state."""
            try:
                while state['run']:
                    if state['ready']:
                        d = state['desc']
                        # Pin in-flight descriptor: core 0's GC
                        # does not scan core 1's stack, so a mv
                        # held only in a local could be swept
                        # mid-transfer
                        state['sent'] = d
                        state['idle'] = 0    # busy published...
                        state['ready'] = 0   # ...before the ack
                        try:
                            if partial:
                                # d[0] is already the row-span
                                # slice (built on core 0 at
                                # handoff): exactly rows d[1]..d[2]
                                p_mv(d[0], d[1], d[2])
                            else:
                                p_mv(d[0], 0, h1)
                        except Exception as e:
                            # off-panel failure: the worker stays
                            # claim-capable (present() can never
                            # hang), first error kept for the app.
                            # The panel now holds LESS than the
                            # surfaces do: the diff target stopped
                            # describing it, so the chain is out of
                            # sync until a WHOLE frame lands.
                            if state['err'] is None:
                                state['err'] = e
                            state['sync'] = 0
                        else:
                            # only a transfer that covered EVERY row
                            # can re-establish the baseline: a span
                            # success says nothing about the rows the
                            # panel is still missing. Published here,
                            # at transfer end, so a failure that
                            # surfaces after the next frame was
                            # published is never lost.
                            if d[1] == 0 and d[2] == h1:
                                state['sync'] = 1
                        state['sent'] = None
                        state['idle'] = 1    # transfer completed
                    else:
                        sleep_us(50)
            except Exception as e:
                # worker death: never leave present() or
                # wait_idle() spinning on a dead thread. A queued
                # frame can never land now, so panel contents are
                # unknown to the chain.
                if state['err'] is None:
                    state['err'] = e
                state['sync'] = 0
                state['sent'] = None
                state['idle'] = 1
            state['stopped'] = 1             # explicit exit ack

        # Thread stacks come from system RAM, NOT the MicroPython heap:
        # a heap alloc failure never triggers auto-GC here, so sweep
        # import-time garbage before claiming the 8 KB stack.
        gc.collect()
        _thread.stack_size(8 * 1024)
        _thread.start_new_thread(presenter, ())

    def clear(self):
        """Clear the back buffer being drawn into."""
        self.back_buffer.clear()

    def present(self):
        """Send the drawn back buffer to the panel, swap to the next.

        Returns the last row sent (y1), or -1 when partial=True found
        the frame identical to the last-presented one: nothing went
        over the wire and NO swap happened - the panel already shows
        this frame, so the buffers stay put (that is what keeps the
        other surface a valid diff target). A DESYNCED chain never
        returns -1: after a failed transfer every present() publishes
        the WHOLE frame, so an identical desired image cannot suppress
        the repair.

        async: returns at CLAIM - core 1 holds the whole descriptor;
        the transfer completes later (wait_idle() is the completion
        edge). Raises RuntimeError when the presenter thread has
        stopped (present after close, or a dead worker) - a dead
        worker can never leave this call spinning.

        sequential: a transfer failure marks the chain out of
        sync and RE-RAISES (the caller owns the wire); the next
        present() sends the whole frame.
        """
        if not self.async_double:
            if not self.partial:
                try:
                    self.d.present_mv(self.back_buffer.mv, 0, self._last_row)
                except Exception:
                    self._sync = 0     # W2/R6: this frame did not land whole
                    raise
                self._sync = 1         # whole frame confirmed on the panel
                return self._last_row
            bb = self.back_buffer
            bw = bb.byte_width
            if self._sync:
                span = bb.diff_rows(self._prev)
                if span is None:
                    return -1
                y0, y1 = span
            else:
                # W2: the panel is missing rows this diff cannot see
                y0, y1 = 0, self._last_row
            lo = y0 * bw
            hi = (y1 + 1) * bw
            try:
                self.d.present_mv(bb.mv[lo:hi], y0, y1)
            except Exception:
                self._sync = 0          # those rows were never shown
                raise
            self._prev_mv[lo:hi] = bb.mv[lo:hi]   # rows just shown
            if y0 == 0 and y1 == self._last_row:
                self._sync = 1         # whole frame confirmed on the panel
            return y1
        st = self.state
        if st['stopped']:
            raise RuntimeError('swap chain: presenter stopped')
        if self.partial and st['sync']:
            # ONE scan per frame, here: the chain is the only diff
            # owner, Scene only reads the verdict this returns.
            # The other surface holds the last-presented frame (the
            # claim-spin below guarantees the handoff happened);
            # core 1 only ever READS it -> read/read, safe.
            idx = self.idx
            span = self.surfaces[idx].diff_rows(self.surfaces[idx ^ 1].buf)
            if span is None:
                return -1
            y0, y1 = span
            bw = self.back_buffer.byte_width
            # slice + rows travel as ONE value: the presenter can
            # never pair this frame's bytes with another frame's
            # rows (one small view + one small tuple, core 0)
            desc = (self.surfaces[idx].mv[y0 * bw:(y1 + 1) * bw], y0, y1)
        elif self.partial:
            # W2 resync: a failed transfer means the diff target no
            # longer describes the panel, so EVERY row of this frame
            # goes out (no slice, no scan) until one such frame lands.
            idx = self.idx
            desc = (self.surfaces[idx].mv, 0, self._last_row)
        else:
            desc = (self.surfaces[self.idx].mv, 0, self._last_row)
        st['desc'] = desc
        st['ready'] = 1
        while st['ready'] and not st['stopped']:
            pass  # core-1 thread claims the WHOLE descriptor, then swap
        if st['ready']:
            # worker stopped between publish and claim: this frame
            # never reached the panel and NO swap happened
            raise RuntimeError('swap chain: presenter stopped')
        self.idx ^= 1
        self.back_buffer = self.surfaces[self.idx]
        return desc[2] if self.partial else self._last_row

    def wait_idle(self, timeout_ms=0):
        """Block until the last claimed transfer COMPLETED: the
        panel verifiably holds every claimed frame when this
        returns True (check error for transfer failures, in_sync
        for the panel-contents verdict). The public completion
        edge - present() returns at CLAIM, this waits for the
        transfer itself. Sequential chains: no-op, True.
        timeout_ms > 0 bounds the wait (False on timeout)."""
        if not self.async_double:
            return True
        st = self.state
        if timeout_ms > 0:
            for _ in range(timeout_ms):
                if st['idle']:
                    return True
                sleep_ms(1)
            return False
        while not st['idle']:
            pass
        return True

    @property
    def error(self):
        """First failed transfer or worker death (Exception), or
        None. A frame whose present() returned is SUBMITTED, not
        confirmed: this is how submitted images are told apart
        from confirmed panel contents. The worker survives a
        failed transfer, so later frames still go out. HISTORY
        only - it never clears, so read in_sync for the CURRENT
        panel-contents state."""
        return self.state['err'] if self.async_double else None

    @property
    def in_sync(self):
        """W2: True = the chain knows what the panel shows (its diff
        target describes the panel, and the last frame present()
        accepted is on the glass). False = a transfer failed (or the
        worker died) and the panel holds LESS than the surfaces do;
        every later present() sends a whole frame until one lands, so
        this is a diagnostic, not something the app has to act on.

        Deliberately separate from error: error is the first
        historical failure and stays set, while in_sync returns to
        True after a successful whole-frame transfer. Sequential
        chains: a failed present() re-raises and leaves the chain out
        of sync."""
        if self.async_double:
            return self.state['sync'] == 1
        return self._sync == 1

    def close(self, timeout_ms=100):
        """Stop the presenter thread (before display.cleanup()).

        True = the final CLAIMED transfer finished AND the worker
        acknowledged actual exit ('stopped'): only then is the SPI
        free for cleanup(). False = timeout, the worker is alive
        and may hold the wire: never call display.cleanup() on a
        False return; retry or accept the failure. Every frame
        whose present() returned was claimed, so it IS transferred
        before the exit. Idempotent; sequential chains have no
        thread and return True.

        Pre-P1 this guessed 100 ms on 'idle' alone - idle neither
        proved thread exit nor covered the claim window (the ack
        was out while busy was not yet published, so cleanup could
        deinit the SPI as a just-claimed transfer was starting).
        """
        if not self.async_double or self.state['stopped']:
            return True
        st = self.state
        st['run'] = 0
        # bounded join: 100 ms >> one full-frame transfer at
        # 25 MHz (2.6 ms); waits for the EXIT ACK, not for idle
        for _ in range(timeout_ms):
            if st['stopped']:
                return True
            sleep_ms(1)
        return False
