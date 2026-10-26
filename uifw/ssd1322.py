"""MicroPython SSD1322 OLED display driver (panel layer).

This module is the panel: init/reset/sleep, address windows and moving
framebuffer bytes into GDDRAM. It owns no framebuffer and draws nothing.
Drawing happens on a Surface (surface.py); getting a Surface onto the
panel is the SwapChain's job (swapchain.py).

NOTE: this panel requires command args with D/C#=1: write_cmd() sends
the command byte with D/C#=0 and its args via write_data() with
D/C#=1. Args sent with D/C#=0 corrupt the display.

TRANSACTION OWNERSHIP (PLAN-review-hardening.md P2): the presenter
thread (swapchain.py, core 1) and the app thread (core 0, idle_dim
policies, raw calls) share this panel, and the panel parser is
stateful: a control command between a frame's WRITE_RAM and its
pixel payload makes the panel swallow the payload (silent blank
rows; pinned by tests/test_txn.py). So every PUBLIC method is one
atomic transaction on a _thread lock, held across the whole
sequence, pin changes included: window+payload in present_mv,
command+args everywhere else. Rules:

  - _write_cmd/_write_data/_set_window are the raw, unlocked wire
    layer; public methods are thin locked wrappers over them.
    MicroPython locks are NOT reentrant: never call a public
    method from inside another transaction (cleanup() uses the
    raw layer, not blank()/sleep()).
  - A raw write_cmd()/write_data() PAIR is two separate
    transactions: each is atomic, the pair is not. Code that
    needs a multi-call sequence (custom window + payload) holds
    the bus explicitly: begin(), _write_cmd/_write_data calls
    ONLY, end() in a finally.
  - The lock covers WIRE access only, never rendering: with an
    async chain a main-thread control call (set_brightness and
    friends) can block up to one in-flight transfer (~2.6 ms for
    a full frame at 25 MHz); the swap chain handoff keeps that
    off the render loop.
"""
import _thread  # type: ignore
from micropython import const  # type: ignore
from utime import sleep_ms  # type: ignore


class Display(object):
    """Serial interface for the SSD1322 256x64 GS4 OLED panel.

    Note:  All coordinates are zero based.
    """

    # Command constants from display datasheet
    ENABLE_GRAY_SCALE_TABLE = const(0x00)
    SET_COLUMN_ADDRESS = const(0x15)
    WRITE_RAM = const(0x5C)
    READ_RAM = const(0x5D)
    SET_ROW_ADDRESS = const(0x75)
    SET_REMAP_DUAL_COM_LINE_MODE = const(0xA0)  # Re-map & Dual COM Line Mode
    SET_DISPLAY_START_LINE = const(0xA1)
    SET_DISPLAY_OFFSET = const(0xA2)
    SET_DISPLAY_MODE_ALL_OFF = const(0xA4)
    SET_DISPLAY_MODE_ALL_ON = const(0xA5)
    SET_DISPLAY_MODE_NORMAL = const(0xA6)
    SET_DISPLAY_MODE_INVERSE = const(0xA7)
    PARTIAL_DISPLAY_ENABLE = const(0xA8)
    PARTIAL_DISPLAY_DISABLE = const(0xA9)
    SET_FUNCTION_SELECTION = const(0xAB)
    DISPLAY_SLEEP_ON = const(0xAE)
    DISPLAY_SLEEP_OFF = const(0xAF)
    SET_PHASE_LENGTH = const(0xB1)
    SET_FRONT_CLOCK_DIVIDER = const(0xB3)
    DISPLAY_ENHANCEMENT_A = const(0xB4)
    SET_GPIO = const(0xB5)
    SET_SECOND_PRECHARGE_PERIOD = const(0xB6)
    SET_GRAY_SCALE_TABLE = const(0xB8)
    SELECT_DEFAULT_LINEAR_GRAY_SCALE_TABLE = const(0xB9)
    SET_PRECHARGE_VOLTAGE = const(0xBB)
    SET_VCOMH_VOLTAGE = const(0xBE)
    SET_CONTRAST_CURRENT = const(0xC1)
    MASTER_CURRENT_CONTROL = const(0xC7)
    SET_MULTIPLEX_RATIO = const(0xCA)
    DISPLAY_ENHANCEMENT_B = const(0xD1)
    SET_COMMAND_LOCK = const(0xFD)

    # Options for controlling VSL selection
    ENABLE_EXTERNAL_VSL = const(0x00)
    ENABLE_INTERNAL_VSL = const(0x02)

    # Options for grayscale quality
    NORMAL_GRAYSCALE_QUALITY = const(0xB0)
    ENHANCED_LOW_GRAY_SCALE_QUALITY = const(0XF8)

    # Options for display enhancement b
    RESERVED_ENHANCEMENT = const(0x00)
    NORMAL_ENHANCEMENT = const(0x02)

    # Options for command lock
    COMMANDS_LOCK = const(0x16)
    COMMANDS_UNLOCK = const(0x12)

    # Column and row maximums
    # NOTE: Unsure if addresses vary among displays
    COLUMN_ADDRESS = const(0x77)
    ROW_ADDRESS = const(0x7F)

    # Frame-rate model (datasheet 8.5): FRM = Fosc / (D * K * MUX)
    #   D   = 2**divide, divide = B3h A[3:0], valid codes 0..10
    #   K   = DCLKs per row = phase1 + phase2 + 10 + GS15
    #       = 2 + 14 + 10 + 112 = 138 (init: B1h 0xE2, B9h table)
    #   MUX = multiplex ratio (CAh) = height
    #   Fosc= oscillator level B3h A[7:4] (1..15; 0 stops the osc).
    #       The datasheet fixes only the default level 5 at 1.94 MHz
    #       typ and says the frequency grows with the level. ON-PANEL
    #       SWEEP (this board, 2026-10): divide-8 settings (0xD3,
    #       0x93) flicker heavily, 0x93 even audibly, while divide-2
    #       (0x91, 0x51) stay clean -> the osc SATURATES near the
    #       default level. Modelled capped: levels >= 5 all = 1.94 MHz.
    OSC_STEP_HZ = const(388000)   # Hz per oscillator level
    OSC_DEF_LVL = const(5)        # 1.94 MHz typ; capped above this
    K_ROW_DCLK = const(138)       # DCLKs per row at the init settings
    # NOTE: keep these names underscore-free: on MicroPython a
    # class-body assignment "_X = const(v)" silently drops the
    # attribute (host CPython does not - tests stay green).

    def __init__(self, spi, cs, dc, rst, width=256, height=64,
                 contrast=0x9F, brightness=0x0F):
        """Constructor for Display.

        Args:
            spi (Class Spi):  SPI interface for display
            cs (Class Pin):  Chip select pin
            dc (Class Pin):  Data/Command pin
            rst (Class Pin):  Reset pin
            width (Optional int): Screen width (default 256)
            height (Optional int): Screen height (default 64)
            contrast (Optional int): Set Contrast Current (cmd 0xC1),
                0..255 (default 0x9F) - per-shade segment current.
            brightness (Optional int): Master Contrast Current
                Control (cmd 0xC7), 0..15 (default 0x0F) - global
                current scale: hardware dimming that keeps all 16
                GS levels (unlike a Surface LUT crush).
        """
        self.spi = spi
        self.cs = cs
        self.dc = dc
        self.rst = rst
        self.width = width
        self.height = height
        self.contrast = contrast
        self.brightness = brightness & 0x0F
        self.byte_width = -(-width // 2)  # Ceiling division
        self.buffer_length = self.byte_width * height
        # Pre-built address-window buffers for fast present_mv()
        # (column offset of 28 matches set_address default).
        self._cmd_col = bytearray((self.SET_COLUMN_ADDRESS,))
        self._col_args = bytearray((28, self.width // 4 + 27))
        self._cmd_row = bytearray((self.SET_ROW_ADDRESS,))
        self._row_full = bytearray((0, height - 1))
        self._row_part = bytearray((0, height - 1))
        self._cmd_ram = bytearray((self.WRITE_RAM,))
        # Zero block for blank() (slice writes, no big allocation)
        self._zblk = bytearray(512)
        # P2 transaction lock (module docstring): allocated HERE,
        # before any presenter thread can exist, so the wire path
        # never allocates. Uncontended acquire is a few machine
        # instructions: init and single-threaded use pay nothing
        # measurable.
        self._lock = _thread.allocate_lock()
        # Initialize GPIO pins
        self.cs.init(self.cs.OUT, value=1)
        self.dc.init(self.dc.OUT, value=0)
        self.rst.init(self.rst.OUT, value=1)

        self.reset()
        # Send initialization commands
        self.write_cmd(self.SET_COMMAND_LOCK, self.COMMANDS_UNLOCK)
        self.write_cmd(self.DISPLAY_SLEEP_ON)
        # Panel clock: oscillator level 9, divide by 2 (~110 Hz by the
        # capped model - the osc saturates at level 5, see above; the
        # old "80 fps" comment inherited from the upstream driver was
        # wrong). set_refresh() retunes this to an app-chosen rate.
        self._b3 = 0x91
        self.write_cmd(self.SET_FRONT_CLOCK_DIVIDER, self._b3)
        self.refresh_hz = self._frm(self._b3)
        # Set multiplex ratio to 1/64
        self.write_cmd(self.SET_MULTIPLEX_RATIO, 0x3F)
        self.write_cmd(self.SET_DISPLAY_OFFSET, 0x00)
        self.write_cmd(self.SET_DISPLAY_START_LINE, 0x00)
        # Column address 0 mapped to SEG0
        # Disable nibble remap
        # Scan from COM[N-1] to C0M0
        # Disable COM split between odd and even
        # Enable dual COM line mode
        self.write_cmd(self.SET_REMAP_DUAL_COM_LINE_MODE, 0x14, 0x11)  # 10.1.6
        # Disable GPIO pins input
        self.write_cmd(self.SET_GPIO, 0x00)
        # Enable internal VDD regulator
        self.write_cmd(self.SET_FUNCTION_SELECTION, 0x01)
        # Enable external VSL
        self.write_cmd(self.DISPLAY_ENHANCEMENT_A,
                       self.ENABLE_EXTERNAL_VSL | 0xA0,
                       self.ENHANCED_LOW_GRAY_SCALE_QUALITY | 0x05)
        # Set segment output current
        self.write_cmd(self.SET_CONTRAST_CURRENT, self.contrast)
        # Set scale factor of segment output current control
        self.write_cmd(self.MASTER_CURRENT_CONTROL, self.brightness)
        # Set default linear gray scale table
        self.write_cmd(self.SELECT_DEFAULT_LINEAR_GRAY_SCALE_TABLE)
        # Set phase 1 as 5 clocks and phase 2 as 14 clocks
        self.write_cmd(self.SET_PHASE_LENGTH, 0xE2)
        # Enhance driving scheme capability
        self.write_cmd(self.DISPLAY_ENHANCEMENT_B,
                       self.RESERVED_ENHANCEMENT | 0xA2, 0x20)
        # Set pre-charge voltage level as 0.60 * VCC
        self.write_cmd(self.SET_PRECHARGE_VOLTAGE, 0x1F)
        # Set second pre-charge period as 8 clocks
        self.write_cmd(self.SET_SECOND_PRECHARGE_PERIOD, 0x08)
        # Set common pin deselect voltage as 0.86 * VCC
        self.write_cmd(self.SET_VCOMH_VOLTAGE, 0x07)
        # Normal display mode
        self.write_cmd(self.SET_DISPLAY_MODE_NORMAL)
        self.write_cmd(self.PARTIAL_DISPLAY_DISABLE)
        self.write_cmd(self.DISPLAY_SLEEP_OFF)

        self.blank()

    def begin(self):
        """Take the panel bus for a multi-call raw sequence:

            d.begin()
            try:
                d._write_cmd(d.WRITE_RAM)
                d._write_data(buf)
            finally:
                d.end()

        Inside begin()/end() call ONLY the raw wire layer
        (_write_cmd/_write_data/_set_window) - any public method
        deadlocks on its own non-reentrant lock. Nothing else can
        touch the panel while the bus is held: keep such
        sequences short."""
        self._lock.acquire()

    def end(self):
        """Release the bus taken by begin(). Put this in a finally:
        an exception between the two leaves the bus held."""
        self._lock.release()

    def blank(self):
        """Clear GDDRAM (all pixels off).

        Panel-side clear: streams zero bytes into RAM, no framebuffer
        involved. Framebuffer clears live on Surface.clear().
        One transaction: window plus the whole zero stream under the
        lock, so a presenter frame can never interleave into it.
        """
        lk = self._lock
        lk.acquire()
        try:
            self._blank()
        finally:
            lk.release()

    def _blank(self):
        """blank()'s wire sequence, unlocked. Call only with the
        transaction lock held (blank(), cleanup())."""
        self._set_window(self._row_full)
        z = self._zblk
        for _ in range(self.buffer_length // 512):
            self._write_data(z)

    def cleanup(self):
        """Clean up resources: blank, sleep, deinit SPI.

        One transaction over the whole shutdown: the raw internals
        stand in for blank()/sleep() (the lock is not reentrant),
        so no other thread's frame can land between the blank and
        the sleep. With an async chain close the CHAIN first
        (swapchain.close() returning True): only then is the worker
        provably off the wire; cleanup against a live worker just
        blocks until its transfer ends, then deinits.
        """
        lk = self._lock
        lk.acquire()
        try:
            self._blank()
            self._write_cmd(self.DISPLAY_SLEEP_ON)
            self.spi.deinit()
        finally:
            lk.release()
        print('display off')

    def _frm(self, b3):
        """Achieved Hz for a raw 0xB3 byte, per the model above."""
        lvl = b3 >> 4
        if lvl > self.OSC_DEF_LVL:
            lvl = self.OSC_DEF_LVL          # osc saturates near default
        return (self.OSC_STEP_HZ * lvl
                / ((self.K_ROW_DCLK * self.height) << (b3 & 0x0F)))

    def _set_window(self, row_args):
        """Set full-width column window and given row window.

        Raw (unlocked) - part of a caller's transaction: present_mv,
        _blank and begin()/end() code hold the lock around it.

        Identical byte sequence and D/C# behavior as set_address(), but
        using pre-allocated buffers (no per-frame allocations).

        Args:
            row_args (bytearray): Row start/end argument pair.
        """
        self.dc(0)
        self.cs(0)
        self.spi.write(self._cmd_col)
        self.cs(1)
        self._write_data(self._col_args)
        self.dc(0)
        self.cs(0)
        self.spi.write(self._cmd_row)
        self.cs(1)
        self._write_data(row_args)
        self.dc(0)
        self.cs(0)
        self.spi.write(self._cmd_ram)
        self.cs(1)

    def _write_cmd(self, command, *args):
        """Raw wire layer, NO lock: command byte at D/C#=0, args at
        D/C#=1, one CS-plucked sequence. Callers hold the transaction
        lock: the public methods (which wrap this) and begin()/end()
        sequences."""
        self.dc(0)
        self.cs(0)
        self.spi.write(bytearray([command]))
        self.cs(1)
        # Handle any passed data
        if len(args) > 0:
            self._write_data(bytearray(args))

    def _write_data(self, data):
        """Raw wire layer, NO lock: data at D/C#=1. See _write_cmd()."""
        self.dc(1)
        self.cs(0)
        self.spi.write(data)
        self.cs(1)

    def present_mv(self, mv, y0, y1):
        """Present a memoryview of contiguous rows y0..y1.

        Performs no allocations, so it is safe to call from a second
        thread (GIL-off ports like RP2) while the main thread renders
        into a different framebuffer. The memoryview must be created
        by the rendering thread (and stay referenced there).

        The row window AND the payload are ONE transaction (module
        docstring): no other thread's command can open between
        WRITE_RAM and the bytes - that collision makes the panel
        swallow the whole payload. A main-thread caller of this can
        block up to one in-flight transfer.

        Args:
            mv (memoryview): rows y0..y1, byte_width bytes per row.
            y0 (int): First row.
            y1 (int): Last row.
        """
        lk = self._lock
        lk.acquire()
        try:
            self._row_part[0] = y0
            self._row_part[1] = y1
            self._set_window(self._row_part)
            self._write_data(mv)
        finally:
            lk.release()

    def reset(self):
        """Perform reset.

        Serialized like any transaction: a reset pulse between a
        window and its payload would orphan the frame, so the bus is
        held for the ~150 ms pulse - longer than any transfer, which
        is exactly the point."""
        lk = self._lock
        lk.acquire()
        try:
            self.rst(0)
            sleep_ms(50)
            self.rst(1)
            sleep_ms(100)
        finally:
            lk.release()

    def set_address(self, x0, y0, x1, y1, offset=28):
        """Set column and row addresses.

        One atomic transaction, ending at WRITE_RAM - but it sends
        NO payload: a following write_data() is a separate
        transaction, and with a live presenter that pair is not
        atomic (see the module docstring). Raw set_address +
        write_data is only safe with no async chain; with one,
        hold the bus (begin()/end()) or use present_mv().

        Args:
            x0 (byte): Starting X address
            y0 (byte): Starting Y address
            x1 (byte): Ending X address
            y1 (byte): Ending Y address
            offset (byte): Horizontal offset (Default 28)
        Note:
            There is a horizontal offset of 28 (pixels start from segment 112)
        """
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.SET_COLUMN_ADDRESS, x0 + offset,
                            x1 + offset)
            self._write_cmd(self.SET_ROW_ADDRESS, y0, y1)
            self._write_cmd(self.WRITE_RAM)
        finally:
            lk.release()

    def set_column_address(self, column_start, column_end):
        """Set column start and end address of display data RAM.

        Args:
            column_start (byte): Start column
            column_end (byte): End column
        """
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.SET_COLUMN_ADDRESS, column_start,
                            column_end)
        finally:
            lk.release()

    def set_contrast(self, contrast):
        """Set per-shade segment current live (cmd 0xC1, 0..255)."""
        self.contrast = contrast
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.SET_CONTRAST_CURRENT, contrast)
        finally:
            lk.release()

    def set_brightness(self, brightness):
        """Set global current scale live (cmd 0xC7, 0..15).

        Hardware dimming: all 16 GS levels stay distinct, just dimmer
        (a Surface LUT crush would drop levels). Two SPI bytes - call
        it from an idle policy, just not every frame. The command and
        its arg are one transaction: from the app thread this can
        block up to an in-flight frame transfer, and it can NEVER
        land inside one (the P2 corruption this replaced).
        """
        self.brightness = brightness & 0x0F
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.MASTER_CURRENT_CONTROL, self.brightness)
        finally:
            lk.release()

    def set_display_enhancement_a(self, external_vsl=True,
                                  enhanced_gs_quality=True):
        """Enhance the display performance A.

        Args:
            external_vsl (bool): True (Default)=External, False=Internal
            enhanced_gs_quality (bool): True (Default)=Enhanced, False=Normal
        """
        if external_vsl:
            vsl = self.ENABLE_EXTERNAL_VSL
        else:
            vsl = self.ENABLE_INTERNAL_VSL

        if enhanced_gs_quality:
            enhanced_gs = self.ENHANCED_LOW_GRAY_SCALE_QUALITY
        else:
            enhanced_gs = self.NORMAL_GRAYSCALE_QUALITY

        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.DISPLAY_ENHANCEMENT_A,
                            vsl | 0xA0,
                            enhanced_gs | 0x05)
        finally:
            lk.release()

    def set_display_enhancement_b(self, enhanced=True):
        """Enhance the display performance B.

        Args:
            enhance (bool): True (Default)=Recommended, False=Normal
        """
        if enhanced:
            deb = self.RESERVED_ENHANCEMENT
        else:
            deb = self.NORMAL_ENHANCEMENT

        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.DISPLAY_ENHANCEMENT_B,
                            deb | 0x82,
                            0x20)
        finally:
            lk.release()

    def set_refresh(self, fps):
        """Retune the panel refresh (cmd 0xB3) to the rate closest to fps.

        Searches every valid (oscillator level, divide) pair of the
        model above and programs the closest match - the capped osc
        keeps the pick away from the divide-8 region that flickers
        on-panel. The oscillator quantizes: the ACHIEVED rate is
        returned (and kept as refresh_hz) - pace framebuffer presents
        to THAT, not to the requested number. Panel refresh and
        GDDRAM writes stay independent: presenting faster than
        refresh_hz never breaks anything, frames just merge between
        panel scans.
        """
        if fps <= 0:
            raise ValueError('fps must be > 0')
        best = None
        for div in range(11):            # A[3:0]: divide by 2**div
            for lvl in range(1, 16):     # A[7:4]: 0 would stop the osc
                b3 = (lvl << 4) | div
                frm = self._frm(b3)
                err = abs(frm - fps)
                if best is None or err < best[0]:
                    best = (err, b3, frm)
        lk = self._lock
        lk.acquire()
        try:
            self._b3 = best[1]
            self.refresh_hz = best[2]
            self._write_cmd(self.SET_FRONT_CLOCK_DIVIDER, self._b3)
        finally:
            lk.release()
        return self.refresh_hz

    def set_row_address(self, row_start, row_end):
        """Set row start and end address of display data RAM.

        Args:
            row_start (byte): Start column
            row_end (byte): End column
        """
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.SET_ROW_ADDRESS, row_start, row_end)
        finally:
            lk.release()

    def sleep(self):
        """Put display to sleep."""
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.DISPLAY_SLEEP_ON)
        finally:
            lk.release()

    def wake(self):
        """Wake display from sleep."""
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(self.DISPLAY_SLEEP_OFF)
        finally:
            lk.release()

    def write_cmd(self, command, *args):
        """Write command to display: ONE atomic transaction, command
        byte and args together. A following raw write_data() is a
        SEPARATE transaction - the PAIR is not atomic against a live
        presenter; for a multi-call wire sequence hold the bus
        (begin()/end() + the raw _write_cmd/_write_data layer).

        Args:
            command (byte): Display command code.
            *args (optional bytes): Data to transmit.
        """
        lk = self._lock
        lk.acquire()
        try:
            self._write_cmd(command, *args)
        finally:
            lk.release()

    def write_data(self, data):
        """Write data to display: one atomic transaction at D/C#=1.
        Standalone, it lands in whatever window the panel currently
        holds: pair it with a command only inside begin()/end()
        (see write_cmd() and the module docstring).

        Args:
            data (bytes): Data to transmit.
        """
        lk = self._lock
        lk.acquire()
        try:
            self._write_data(data)
        finally:
            lk.release()
