"""selftest: POST + device-shell hook pack for micropy-system guests.

The framework launcher runs a power-on self-test BEFORE a candidate
slot may confirm: the guest's ``post_checks()`` returns
``(name, ok, detail)`` tuples (framework contract:
``lib/coresys/post.py``); a failing candidate is rolled back and
quarantined, never promoted. The device shell takes app commands via
``shell.add(command, handler, description)`` (``telnet_service.py``).
This module supplies the panel side of both so the guest wires them
in one line each:

    # app/main.py (slot-local app_entry.py)
    from uifw import selftest

    def post_checks():
        return selftest.panel_checks(spi_sck=18, spi_mosi=19,
                                     cs=17, dc=20, rst=21)

    async def main():
        app = App(...)                      # the panel is up for real now
        selftest.add_command(shell.add, app)   # shell command 'ui'

IMPORTS: nothing at module level — the probe imports machine/utime/gc
and the engine INSIDE panel_checks, so importing selftest cannot
disturb the measured host import map. panel_checks needs real (or
picosim) hardware when CALLED.

Timing: POST runs inside the framework watchdog window (12 s); the
probe costs one init sequence + one full-frame push (measured 2.6 ms
per frame at 25 MHz + init ~50 ms).
"""


def _release(spi, display, chain):
    """Tear down exactly what panel_checks ACQUIRED, in shutdown order.

    Returns a list of teardown faults (strings); it never raises, so a
    teardown fault can neither mask the probe verdict nor lose the POST
    report. App.__init__'s rollback (app.py:239-247) is the model, and
    the same rule: each resource is released only if its constructor
    returned, so a failure at ANY step leaves nothing behind.

    Order: chain first (EXIT ACK), then the panel (blank + sleep + SPI
    deinit as one transaction), then the bare bus when there is no
    Display object to deinit it. A chain that does not ack leaves the
    panel awake and the bus initialized ON PURPOSE — the P1 rule:
    never deinit the SPI under a worker that may hold the wire. That
    case is returned as a fault, not kept quiet, because POST must not
    report a pass over an unclean device.
    """
    bad = []
    try:
        if chain is not None:
            if chain.close():
                display.cleanup()          # blank, sleep, spi.deinit
            else:
                bad.append('chain no-exit-ack')
        elif display is not None:
            display.cleanup()              # also deinits the bus
        elif spi is not None:
            spi.deinit()                   # no Display object: bus only
    except Exception as e:
        bad.append(repr(e))
    return bad


def panel_checks(spi_id=0, spi_baud=25000000, spi_sck=None, spi_mosi=None,
                 cs=None, dc=None, rst=None, heap_floor=24576):
    """Bring the panel chain up through the REAL wire and tear it down.

    Checks, in order:
      ui-heap   gc.mem_free() >= heap_floor. Frame = w*h/2 = 8192 B
                for 256x64 GS4; the async chain holds two surfaces.
                heap_floor default 24576 (3 frames) is a PLACEHOLDER —
                set the figure measured on the deployed board.
      ui-panel  Display init + one full-white frame over a SYNCHRONOUS
                chain (no core-1 worker during POST — the presenter
                thread is the app's business, not the bootloader's) +
                clean teardown in App.close order: chain exit ack, then
                display cleanup (sleep + SPI deinit). Teardown runs on
                EVERY path, including the paths where a constructor
                failed: SPI, Display and SwapChain are tracked apart
                and each one that came up is released (_release), so a
                failed POST never leaves the bus initialized or the
                panel awake. The panel is left powered only when the
                worker does not ack (the P1 rule), and that is a
                FAILED check, not a passing one.

    A heap-floor failure skips the panel probe (OOM mid-probe is not a
    check, it is a crash) and returns just the heap tuple — the
    framework rolls the candidate back either way.

    Nothing here propagates an exception to the framework: a probe
    fault, a constructor fault and a teardown fault all come back as
    (name, ok, detail), detail naming which one it was ('teardown ...'
    for the release path). A teardown fault forces ok False even when
    the frame itself landed — a POST that passes while the device is
    left half-initialized is a false pass.
    """
    out = []
    import gc
    free = gc.mem_free()
    ok = free >= heap_floor
    out.append(('ui-heap', ok, 'free %d floor %d' % (free, heap_floor)))
    if not ok:
        return out

    from machine import SPI
    from utime import ticks_us, ticks_diff
    from .app import _pin
    from .ssd1322 import Display
    from .swapchain import SwapChain

    spi = display = chain = None
    verdict = (False, 'probe not run')
    try:
        try:
            spi = SPI(spi_id, baudrate=spi_baud,
                      sck=_pin(spi_sck), mosi=_pin(spi_mosi))
            display = Display(spi, cs=_pin(cs), dc=_pin(dc), rst=_pin(rst))
            chain = SwapChain(display, False)
            b = chain.back_buffer
            b.clear(0)
            b.fill_rect(0, 0, b.width, b.height, 15)
            t0 = ticks_us()
            chain.present()
            dt = ticks_diff(ticks_us(), t0)
            err = chain.error
            verdict = (err is None,
                       'frame %dms @%d%s' % (dt // 1000, spi_baud,
                                             '' if err is None else ' ' + repr(err)))
        except Exception as e:
            verdict = (False, repr(e))
    finally:
        bad = _release(spi, display, chain)
        if bad:
            verdict = (False, verdict[1] + ' teardown ' + '; '.join(bad))
        out.append(('ui-panel',) + verdict)
    return out


def ui_status(app):
    """One-line panel-chain status for the device shell."""
    import gc
    chain = getattr(app, 'chain', None)
    if chain is None:
        c = 'none'
    else:
        st = getattr(chain, 'state', None)
        if st is None:
            c = 'sync'
        else:
            c = 'closed' if st['stopped'] else 'live'
    err = None if chain is None else chain.error
    line = 'chain=%s heap=%d' % (c, gc.mem_free())
    if err is not None:
        line += ' err=%s' % repr(err)
    return line


def add_command(add, app, name='ui'):
    """Register the 'ui' device-shell command: add is shell.add."""
    add(name, lambda args: ui_status(app), 'UI panel-chain status')
