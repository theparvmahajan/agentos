"""Run one real mission through the full pipeline: planner -> scheduler -> workers.

Needs, in other terminals:  make scheduler   and   make worker   (run `make worker` twice for two workers)
"""
import sys
import time

from core import repository as repo
from core.db import get_conn
from core.logging_setup import configure_logging
from core.models import MissionStatus
from core.planner import run_planner
from core.queue import build_queues

DEFAULT = "Compare PostgreSQL and MySQL for a small startup and summarize the tradeoffs"
TERMINAL = {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
TIMEOUT_SECONDS = 600


def main() -> None:
    configure_logging()
    objective = " ".join(sys.argv[1:]) or DEFAULT
    queues = build_queues()
    queues.ensure_groups()

    with get_conn() as conn:
        mission = repo.create_mission(conn, objective)
    print(f"mission {mission.id}\nobjective: {objective}\n")

    t0 = time.time()
    run_planner(mission.id)  # one LLM call: pending -> planning -> running
    print(f"planned in {time.time() - t0:.1f}s")
    queues.publish_event(mission.id)  # wake the scheduler

    started = time.time()
    last = None
    while time.time() - started < TIMEOUT_SECONDS:
        with get_conn() as conn:
            mission = repo.get_mission(conn, mission.id)
            tasks = repo.get_tasks(conn, mission.id)
        snapshot = tuple(sorted((t.key, t.status.value) for t in tasks))
        if snapshot != last:
            print(f"{time.time() - started:6.1f}s  " + "  ".join(f"{k}={s}" for k, s in snapshot))
            last = snapshot
        if mission.status in TERMINAL:
            break
        time.sleep(0.5)
    else:
        sys.exit("gave up waiting: are `make scheduler` and `make worker` running?")

    key_by_id = {t.id: t.key for t in tasks}
    ran = [t for t in tasks if t.started_at and t.finished_at]
    if ran:
        origin = min(t.started_at for t in ran)
        print("\n--- timeline (seconds after the first task started) ---")
        for t in sorted(ran, key=lambda t: t.started_at):
            begin = (t.started_at - origin).total_seconds()
            end = (t.finished_at - origin).total_seconds()
            deps = [key_by_id[d] for d in t.depends_on]
            print(f"{t.key:<5} {t.status.value:<10} {begin:6.1f}s -> {end:6.1f}s   depends_on={deps}")
        busy = sum((t.finished_at - t.started_at).total_seconds() for t in ran)
        wall = (max(t.finished_at for t in ran) - origin).total_seconds()
        if wall > 0:
            print(f"\ntask time added up: {busy:.1f}s   wall clock: {wall:.1f}s   overlap: x{busy / wall:.2f}")

    print(f"\n--- mission {mission.status.value} ---")
    print(mission.result or mission.error)


if __name__ == "__main__":
    main()