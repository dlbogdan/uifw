"""events.py: the input event vocabulary - the framework's shared
kernel. Input sources (input.py decoders, picosim, app code) produce
these small ints; widgets (menu.py, editors.py) consume them. They
live here so menu and editors speak the same protocol without
importing each other. CANONICAL: import from events. menu.py and
input.py re-export these names as compat shims for existing
importers - do not add new importers of the re-exports.

LONG_BACK / LONG_OK are ONE gesture (button held past a threshold,
the input.Buttons 'long' option) on two different buttons: the name
says which button was held, the WIDGETS define the meaning -
    LONG_OK    commit and leave  (editors: on_done, hide)
    LONG_BACK  leave, NOT saved   (editors: on_cancel, hide - the
               same exit as SOFT1)
Short BACK keeps editing (delete a char / pop a level): a hold never
destroys a value. Which pin carries which code is app config, not
widget policy. Codes stay 6/7: picosim and on-board apps pin them.
"""
UP, DOWN, OK, BACK, SOFT1 = 1, 2, 3, 4, 5
LONG_BACK, LONG_OK = 6, 7
