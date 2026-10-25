"""Text renderer: XglcdFont glyphs onto a Surface.

Two paths:
- draw(): dynamic text (values that change every frame) - each glyph
  is a lazily converted GS4 buffer (font.get_letter_gs4) copied with
  the Surface.blit_gs4 fast path.
- sprite(): static text pre-rendered once into one GS4 buffer, then
  blitted as a single sprite every frame.
"""
from .surface import Surface


def _as_char(c):
    """Element of a pre-split iterable -> the 1-char string the
    per-character font path wants (iterables may hold chars, ints or
    bytes). Hot path (get_gs4_ord) never calls this."""
    return chr(c) if isinstance(c, int) else str(c)


class TextRenderer:
    """Render text with one font onto any Surface.

    Args:
        font (XglcdFont object): Font instance (see xglcd_font.py).
    """

    def __init__(self, font):
        self.font = font

    def draw(self, surface, x, y, text, gs=15, spacing=1, key=False,
             clip=None):
        """Draw text using per-glyph GS4 caches (fast dynamic path).

        XglcdFont-class fonts take the ORD path: text.encode() bytes
        straight into font.get_gs4_ord() - zero 1-char-string and zero
        key-tuple allocations per frame (one small bytes object per
        draw call). Fonts without get_gs4_ord (app-supplied fakes)
        keep the per-character path. For absolute zero, pre-split the
        string once and pass the iterable: draw accepts any iterable.

        text (string | iterable): a string takes the encode() fast
        path; any iterable of characters (or ordinals) takes the
        per-element path - the pre-split form this docstring promises.

        Args:
            surface (Surface): Target surface.
            x (int): Starting X position.
            y (int): Starting Y position.
            text (string): Text to draw.
            gs (int): Grayscale 0=Black to 15=White (default grayscale table)
            spacing (int): Pixels between letters (default: 1)
            key (bool): True = glyph background (gs 0) is transparent
                (text over non-black pixels, e.g. a menu pill).
            clip (tuple): Optional (cx, cy, cw, ch) viewport rect -
                glyphs partially outside are clipped, not dropped.
        """
        font = self.font
        get = getattr(font, 'get_gs4_ord', None)
        if get is not None:
            if isinstance(text, str):
                ords = text.encode()      # the promised allocation-free path
            else:
                # pre-split iterable: chars or ordinals, never .encode()
                ords = (ord(c) if isinstance(c, str) else c for c in text)
            for b in ords:
                glyph = get(b, gs)
                if glyph is None:
                    continue  # unsupported char: skip it, draw the rest
                              # (measure_text counts it as 0 too)
                buf, w, w_pad, h = glyph
                surface.blit_gs4(buf, x, y, w_pad, h, key=key, clip=clip)
                x += w + spacing
            return
        for letter in text:
            glyph = font.get_letter_gs4(_as_char(letter), gs)
            if glyph is None:
                continue  # unsupported char: skip it, draw the rest
                          # (measure_text counts it as 0 too)
            buf, w, w_pad, h = glyph
            surface.blit_gs4(buf, x, y, w_pad, h, key=key, clip=clip)
            x += w + spacing

    def sprite(self, text, gs=15, spacing=1):
        """Pre-render text once into a GS4_HMSB sprite.

        Render static text a single time, then draw it each frame with
        Surface.blit_gs4() - far faster than re-rasterizing glyphs with
        draw() every frame.

        Args:
            text (string): Text to render.
            gs (int): Grayscale 0=Black to 15=White.
            spacing (int): Pixels between letters (default: 1)
        Returns:
            (bytearray, int, int): Sprite data, width (padded even for
            the blit_gs4 fast path), height.
        """
        font = self.font
        w = font.measure_text(text, spacing)
        if w & 1:
            w += 1  # Pad even for the blit_gs4 fast path
        h = font.height
        s = Surface(w, h)
        x = 0
        for letter in text:
            glyph = font.get_letter_gs4(letter, gs)
            if glyph is None:
                continue  # unsupported char: skip (measured as 0)
            buf, gw, w_pad, gh = glyph
            s.blit_gs4(buf, x, 0, w_pad, gh)
            x += gw + spacing
        return s.buf, w, h
