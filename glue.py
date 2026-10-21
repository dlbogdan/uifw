"""glue: the one remaining framework bridge (D7, shrinking under D9).

D9 (owner, 2026-10-21): uifw depends on micropy-system and never runs
standalone. An adapter is only needed while TWO components each own one
concept — so the stores are gone: the app-config store and the
config-section view now live in the framework (`lib/coresys/app_store.py`
as AppStore and ConfigSection) and the UI consumes them directly. What is
left here is the scheduler bridge, pending step 8d (frame-loop
ownership).

    # app/main.py, micropy-system consumer
    from lib.coresys.app_store import AppStore
    from lib.coresys.logger import info

    app.stat(2000, sink=info)                 # stats into /log.txt + shell 'log'
    store = AppStore('/app-config.json')      # UI values: APP-owned file
    menu.load_store(store); menu.bind_store(store)
    glue.sched_periodic(tasks, app.sched)     # clock.Sched on manager_tasks

Config-file ruling, unchanged: /system-config.json is FRAMEWORK-owned and
is re-rendered by tools/target/render_config.py at every provisioning, so
UI-committed values belong in the app's own file. ConfigSection is for
values the framework owns and the UI merely edits.
"""
from utime import ticks_ms


def sched_periodic(tasks, sched, interval_ms=100, task_id='ui-sched'):
    """Run clock.Sched.tick on the framework TaskManager.

    Uses ensure_periodic_task's sync update_func path (measured
    signature: task_id, update_func, interval_ms, description,
    is_coroutine). Division of labor: the DISPLAY heartbeat stays in the
    app's own loop (frame budget); this ticks scheduled callbacks'
    cadence, so it must run at or below the app's sched expectations.
    Returns whatever ensure_periodic_task returns."""
    return tasks.ensure_periodic_task(
        task_id, lambda: sched.tick(ticks_ms()),
        interval_ms=interval_ms,
        description='uifw clock.Sched',
        is_coroutine=False)
