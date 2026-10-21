"""glue: micropy-system alignment adapters (D7, PLAN-framework-integration).

The engine NEVER imports lib.coresys: the host suites run without the
framework, the import map is a measured contract, and the submodule
direction forbids the dependency. Alignment therefore happens through
duck-typed adapters that TAKE framework objects, plus injectable sinks
(App.stat(sink=...)). This module imports only stdlib.

    # app/main.py, micropy-system consumer
    from lib.coresys.logger import info
    from lib.coresys.manager_config import ConfigManager   # if used

    app.stat(2000, sink=info)                 # stats into /log.txt + shell 'log'
    store = glue.FileStore('/app-config.json')
    menu.load_store(store); menu.bind_store(store)
    glue.sched_periodic(tasks, app.sched)     # clock.Sched on manager_tasks

Config-file ruling: /system-config.json is FRAMEWORK-owned — it is
re-rendered by tools/target/render_config.py at every provisioning, so
UI-committed values there would not survive re-provision. UI values
live in the app's own file (FileStore default /app-config.json). The
ConfigView form (a section of a live ConfigManager) is for values the
FRAMEWORK owns and the UI merely edits.
"""
import json


class FileStore:
    """Menu store protocol (keys/load/save) over one JSON file.

    Writes are a direct open('w') + json.dump — the same LittleFS-
    proven pattern as manager_config.save_config (no rename games;
    HARDWARE_BASELINE's rename caveats stay respected). A corrupt or
    missing file starts empty: a seed is not a crash.
    """

    def __init__(self, path='/app-config.json'):
        self.path = path
        try:
            with open(path) as f:
                d = json.load(f)
            self.d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            self.d = {}

    def keys(self):
        return list(self.d.keys())

    def load(self, iid):
        return self.d.get(iid)

    def save(self, iid, value):
        self.d[iid] = value
        with open(self.path, 'w') as f:
            json.dump(self.d, f)


class ConfigView:
    """Menu store protocol over ONE SECTION of a manager_config
    instance (duck-typed: .config dict, .get(section, key),
    .set(section, key, value)). The framework keeps persistence and
    subscribers; the UI edits through the same keys."""

    def __init__(self, cfg, section='UI'):
        self.cfg = cfg
        self.section = section

    def _sec(self):
        s = self.cfg.config.get(self.section)
        return s if isinstance(s, dict) else {}

    def keys(self):
        return list(self._sec().keys())

    def load(self, iid):
        return self._sec().get(iid)

    def save(self, iid, value):
        self.cfg.set(self.section, iid, value)


def sched_periodic(tasks, sched, interval_ms=100, task_id='ui-sched'):
    """Run clock.Sched.tick on the framework TaskManager.

    Uses ensure_periodic_task's sync update_func path (measured
    signature: task_id, update_func, interval_ms, description,
    is_coroutine). Division of labor: the DISPLAY heartbeat stays in
    the app's own loop (frame budget); this ticks scheduled
    callbacks' cadence, so it must run at or below the app's sched
    expectations. Returns whatever ensure_periodic_task returns."""
    from utime import ticks_ms
    return tasks.ensure_periodic_task(
        task_id, lambda: sched.tick(ticks_ms()),
        interval_ms=interval_ms,
        description='uifw clock.Sched',
        is_coroutine=False)
