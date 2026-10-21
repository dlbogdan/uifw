"""XGLCD Font Utility."""
from math import floor
from framebuf import FrameBuffer, MONO_VLSB, GS4_HMSB  # type: ignore


class XglcdFont(object):
    """Font data in X-GLCD format.

    Attributes:
        letters: A bytearray of letters (columns consist of bytes)
        width: Maximum pixel width of font
        height: Pixel height of font
        start_letter: ASCII number of first letter
        height_bytes: How many bytes comprises letter height

    Note:
        Font files can be generated with the free version of MikroElektronika
        GLCD Font Creator:  www.mikroe.com/glcd-font-creator
        The font file must be in X-GLCD 'C' format.
        To save text files from this font creator program in Win7 or higher
        you must use XP compatibility mode or you can just use the clipboard.
    """

    def __init__(self, path, width, height, start_letter=32, letter_count=96):
        """Constructor for X-GLCD Font object.

        Args:
            path (string): Full path of font file
            width (int): Maximum width in pixels of each letter
            height (int): Height in pixels of each letter
            start_letter (int): First ACII letter.  Default is 32.
            letter_count (int): Total number of letters.  Default is 96.
        """
        self.width = width
        self.height = height
        self.start_letter = start_letter
        self.letter_count = letter_count
        self.bytes_per_letter = (floor(
            (self.height - 1) / 8) + 1) * self.width + 1
        self.__load_xglcd_font(path)
        # Per-glyph caches, filled lazily with used glyphs only:
        # _mono_cache: letter -> (FrameBuffer, w, h) for get_letter
        # _gs4_cache: (ord << 4 | gs) -> (buf, w, w_pad, h) for
        # get_gs4_ord / get_letter_gs4. INT keys: a per-frame text
        # pass over tuple keys allocates one tuple per glyph per
        # frame (GC churn on-board: measured); ints below 2^30 don't.
        self._mono_cache = {}
        self._gs4_cache = {}

    def __load_xglcd_font(self, path):
        """Load X-GLCD font data from text file.

        Args:
            path (string): Full path of font file.
        """
        bytes_per_letter = self.bytes_per_letter
        # Buffer to hold letter byte values
        self.letters = bytearray(bytes_per_letter * self.letter_count)
        mv = memoryview(self.letters)
        offset = 0
        with open(path, 'rb') as f:
            for line in f:
                # Stop if font file has more glyphs than requested
                if offset + bytes_per_letter > len(self.letters):
                    break
                # Skip lines that do not start with hex values
                line = line.strip()
                if len(line) == 0 or line[0:2] != b'0x':
                    continue
                # Remove comments
                comment = line.find(b'//')
                if comment != -1:
                    line = line[0:comment].strip()
                # Remove trailing commas
                if line.endswith(b','):
                    line = line[0:len(line) - 1]
                # Convert hex strings to bytearray and insert in to letters
                mv[offset: offset + bytes_per_letter] = bytearray(
                    int(b, 16) for b in line.split(b','))
                offset += bytes_per_letter

    def get_letter(self, letter, rotate=0):
        """Convert letter byte data to pixels.

        Args:
            letter (string): Letter to return (must exist within font).
            rotate (int): rotation (default: 0)
        Returns:
            (FrameBuffer): Pixel data in MONO_VLSB.
            (int, int): Letter width and height.
        """
        # Get index of letter
        letter_ord = ord(letter) - self.start_letter
        # Confirm font contains letter (BOTH bounds: a negative index
        # would read through the end of letters - silent garbage)
        if letter_ord < 0 or letter_ord >= self.letter_count:
            print('Font does not contain character: ' + letter)
            return b'', 0, 0
        if rotate == 0:
            cached = self._mono_cache.get(letter)
            if cached is not None:
                return cached
        bytes_per_letter = self.bytes_per_letter
        offset = letter_ord * bytes_per_letter
        mv = memoryview(self.letters[offset:offset + bytes_per_letter])

        # Get width of letter (specified by first byte)
        width = mv[0]
        height = self.height
        byte_height = (height - 1) // 8 + 1  # Support fonts up to 5 bytes high
        if byte_height > 6:
            print("Error: maximum font byte height equals 6.")
            return b'', 0, 0
        array_size = width * byte_height
        ba = bytearray(mv[1:array_size + 1])
        # Set inversion and re-order bytes if height > 1 byte
        pos = 0
        ba2 = bytearray(array_size)

        for i in range(0, array_size, byte_height):
            ba2[pos] = ba[i]
            if byte_height > 1:
                ba2[pos + width] = ba[i + 1]
            if byte_height > 2:
                ba2[pos + width * 2] = ba[i + 2]
            if byte_height > 3:
                ba2[pos + width * 3] = ba[i + 3]
            if byte_height > 4:
                ba2[pos + width * 4] = ba[i + 4]
            if byte_height > 5:
                ba2[pos + width * 5] = ba[i + 5]
            pos += 1

        fb = FrameBuffer(ba2, width, height, MONO_VLSB)

        if rotate == 0:  # 0 degrees
            entry = (fb, width, height)
            self._mono_cache[letter] = entry
            return entry
        elif rotate == 90:  # 90 degrees
            byte_width = (width - 1) // 8 + 1
            adj_size = height * byte_width
            fb2 = FrameBuffer(bytearray(adj_size), height, width, MONO_VLSB)
            for y in range(height):
                for x in range(width):
                    fb2.pixel(y, x, fb.pixel(x, (height - 1) - y))
            return fb2, height, width
        elif rotate == 180:  # 180 degrees
            fb2 = FrameBuffer(bytearray(array_size), width, height, MONO_VLSB)
            for y in range(height):
                for x in range(width):
                    fb2.pixel(x, y,
                              fb.pixel((width - 1) - x, (height - 1) - y))
            return fb2, width, height
        elif rotate == 270:  # 270 degrees
            byte_width = (width - 1) // 8 + 1
            adj_size = height * byte_width
            fb2 = FrameBuffer(bytearray(adj_size), height, width, MONO_VLSB)
            for y in range(height):
                for x in range(width):
                    fb2.pixel(y, x, fb.pixel((width - 1) - x, y))
            return fb2, height, width

    def get_letter_gs4(self, letter, gs=15):
        """Get letter as cached GS4_HMSB packed data.

        Converted once per (letter, gs), then reused - pairs with
        Display.draw_text_gs4() for fast dynamic text.

        Args:
            letter (string): Letter to get (must exist within font).
            gs (int): Grayscale 0=Black to 15=White.
        Returns:
            (tuple): (buf, w, w_pad, h) where buf holds rows of w_pad
            pixels (even, for the draw_sprite_gs4 fast path; the pad
            column is transparent background), or None on error.
        """
        return self.get_gs4_ord(ord(letter), gs)

    def get_gs4_ord(self, o, gs=15):
        """get_letter_gs4 by ORDINAL - the allocation-free hot path:
        text renderers iterate text.encode() bytes and call this, so
        a frame of text allocates no 1-char strings and no key tuples
        (the ord<<4|gs key is a small int: tagged, never heap-boxed).

        Args:
            o (int): Character ordinal (must exist within font).
            gs (int): Grayscale 0=Black to 15=White (0..15).
        """
        key = (o << 4) | gs
        cached = self._gs4_cache.get(key)
        if cached is not None:
            return cached
        fb, w, h = self.get_letter(chr(o))
        if w == 0:
            return None
        w_pad = w + (w & 1)
        buf = bytearray(w_pad * h // 2)
        fb2 = FrameBuffer(buf, w_pad, h, GS4_HMSB)
        for y in range(h):
            for x in range(w):
                if fb.pixel(x, y):
                    fb2.pixel(x, y, gs)
        entry = (buf, w, w_pad, h)
        self._gs4_cache[key] = entry
        # the mono FrameBuffer was scaffolding for this conversion:
        # drop it (get_letter rebuilds on demand - rotation and a
        # later shade pay one re-conversion at init, and every
        # warmed glyph stops parking ~100 B of dead cache)
        self._mono_cache.pop(chr(o), None)
        return entry

    def warm_cache(self, gs=15, start_letter=None, letter_count=None,
                   chars=None):
        """Pre-convert glyphs into the GS4 cache at startup.

        Call during init, before the app starts its dynamic allocation
        churn: long-lived glyph buffers then get allocated contiguously
        (less fragmentation later) and first-use conversion jitter is
        gone. Watch the RAM cost: letter_count * w * h / 2 bytes.

        Args:
            gs (int): Grayscale to bake in.
            start_letter (int): First ASCII code (default: font's).
            letter_count (int): How many glyphs (default: font's).
            chars (string): Bake ONLY these glyphs (deduplicated) -
                the right call for fonts with a known small working
                set (a counter shows digits; a menu knows its labels).
                Overrides start_letter/letter_count.
        """
        if chars is not None:
            for ch in set(chars):
                self.get_gs4_ord(ord(ch), gs)
            return
        first = self.start_letter if start_letter is None else start_letter
        count = self.letter_count if letter_count is None else letter_count
        for i in range(first, first + count):
            self.get_gs4_ord(i, gs)

    def measure_text(self, text, spacing=1):
        """Measure length of text string in pixels.

        Args:
            text (string): Text string to measure
            spacing (optional int): Pixel spacing between letters.  Default: 1.
        Returns:
            int: length of text
        """
        length = 0
        end = self.start_letter + self.letter_count
        for b in text.encode():     # bytes: no 1-char strings, no ord()
            if b < self.start_letter or b >= end:
                continue  # unsupported: counts 0, matches draw() skip
            length += self.letters[(b - self.start_letter)
                                   * self.bytes_per_letter] + spacing
        return length
