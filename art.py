"""Artwork loader: JSON artwork files -> VectorRenderer definitions.

The artwork format is plain JSON - full reference in ART_FORMAT.md.
load() runs ONCE at startup (allocations are fine there); its only
job beyond json.load is resolving layer 'prof' names against the
top-level 'profiles' map, so a shared silhouette stays single-source
while tuning.
"""
import json

from .paths import res


def load(path):
    """Load a JSON artwork file into a VectorRenderer definition dict.

    Placement is NOT part of the artwork - the app passes x/y/height
    to VectorRenderer.
    """
    with open(res(path)) as f:
        defn = json.load(f)
    profiles = defn.get('profiles')
    if profiles:
        for L in defn['layers']:
            p = L.get('prof')
            if isinstance(p, str):
                L['prof'] = profiles[p]
    return defn
