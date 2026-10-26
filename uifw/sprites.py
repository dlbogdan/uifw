"""Sprite resources: load GS4 images once, blit them cheaply.

A sprite is a GS4_HMSB packed bytearray with the grayscale baked in
at load time, so drawing is a pure row-copy (Surface.blit_gs4) with
no per-pixel palette work per frame.
"""
from framebuf import FrameBuffer, MONO_HMSB, GS4_HMSB  # type: ignore

from .paths import res


class SpriteStore:
    """GS4 sprite loading and drawing (static icons, badges, art)."""

    @staticmethod
    def load(path, w, h, gs=15, invert=False):
        """Load MONO_HMSB bitmap from disc as a GS4_HMSB sprite.

        The grayscale is baked in once at load time so drawing can
        copy rows without per-pixel palette lookups.

        Args:
            path (string): Path of sprite image.
            w, h (int): Width and height of image.
            gs (int): Grayscale 0=Black to 15=White.
            invert (bool): True = invert image.
        Returns:
            (bytearray): GS4_HMSB packed sprite data.
        """
        array_size = w * h
        with open(res(path), "rb") as f:
            buf = bytearray(f.read(array_size))
            fb = FrameBuffer(buf, w, h, MONO_HMSB)
            out = bytearray((array_size + 1) // 2)
            fb2 = FrameBuffer(out, w, h, GS4_HMSB)
            fg = 0 if invert else gs
            bg = gs if invert else 0
            for y1 in range(h):
                for x1 in range(w):
                    fb2.pixel(x1, y1, fg if fb.pixel(x1, y1) else bg)
            return out

    @staticmethod
    def draw(surface, buf, x, y, w, h):
        """Blit a loaded sprite onto a surface (fast row-copy path).

        Args:
            surface (Surface): Target surface.
            buf (bytearray): GS4_HMSB packed sprite data.
            x (int): Starting X position.
            y (int): Starting Y position.
            w (int): Width of drawing.
            h (int): Height of drawing.
        """
        surface.blit_gs4(buf, x, y, w, h)
