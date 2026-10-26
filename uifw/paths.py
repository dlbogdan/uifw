"""paths: asset-root resolution for the engine's resource loaders.

The loaders (art.load, SpriteStore.load, XglcdFont) open RELATIVE paths
against CWD. That is correct for a root-deployed board (CWD='/') and on
the host (project root). It is WRONG under the micropy-system A/B
launcher: slot activation prepends the slot dir to sys.path but never
chdirs — CWD stays '/' while the app slot lives at '/apps/a' or
'/apps/b'. The guest therefore sets the root ONCE, letter-agnostic:

    from uifw import paths
    paths.set_root(sys.path[0])       # the active slot, post-activation

Default root '' keeps every existing call site byte-identical (relative
to CWD — host tooling and root-deployed demos need no change).
Absolute paths are never prefixed. This module imports NOTHING, so it
sits in the "no fakes needed" tier of the measured import map.
"""

_ROOT = ''


def set_root(path):
    """Asset root for relative loader paths. '' restores CWD-relative."""
    global _ROOT
    _ROOT = path.rstrip('/') if path else ''


def root():
    return _ROOT


def res(path):
    """Resolve one asset path: absolute passes through, relative joins root."""
    if not _ROOT or path.startswith('/'):
        return path
    return _ROOT + '/' + path
