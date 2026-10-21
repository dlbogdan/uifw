"""widgets.py: the small built-in renderables - Label, Group
(PLAN-app-layer P3; closes roadmap item 2, "Remaining: Label
(dynamic)").

The engine demands NO contract (it reads buffers + clock only);
these are ordinary duck-typed scene members the framework ships so
apps stop hand-rolling a class per line of text. Anything with
draw(surface, t) (+ optional visible) is a scene member - these
satisfy it exactly like any third-party renderable, and the engine
cannot tell the difference.

Label: one dynamic text line - the app sets .value (then
scene.invalidate() for immediacy: the same rule as every other
external push, the recheck ladder stays the safety net).
Group: renderables that compose as ONE scene member - pages,
clusters, the tour pattern; z-order = list order.
"""


class Label:
    """One text line as a scene member.

    Args:
        x, y (int): top-left of the line.
        text (TextRenderer): the renderer (its font draws).
        gs (int): shade.
        spacing (int): letter spacing.
        value (str): the drawn string - set it to change the line.
        visible (bool): Scene skips hidden members for free.
    """

    def __init__(self, x, y, text, gs=15, spacing=1, value='',
                 visible=True):
        self.x, self.y = x, y
        self.text = text
        self.gs = gs
        self.spacing = spacing
        self.value = value
        self.visible = visible

    def draw(self, surface, t):
        self.text.draw(surface, self.x, self.y, self.value,
                       gs=self.gs, spacing=self.spacing)


class Group:
    """Renderables composing as ONE scene member: draw() loops the
    items in order (z-order = list order), honoring each item's
    visible (getattr default True - the Scene rule, applied one
    level down). add/remove at runtime; an empty group is inert."""

    def __init__(self, items=(), visible=True):
        self.items = list(items)
        self.visible = visible

    def add(self, item):
        """Append a renderable (drawn last = on top)."""
        self.items.append(item)
        return item

    def remove(self, item):
        self.items.remove(item)

    def draw(self, surface, t):
        for it in self.items:
            if getattr(it, 'visible', True):
                it.draw(surface, t)
