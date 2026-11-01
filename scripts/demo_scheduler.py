"""Watch the scheduler dispatch a diamond of tasks to two fake workers.

Needs the scheduler running in another terminal:  make scheduler
No LLM is involved: each fake worker just sleeps for a couple of seconds per task.
"""
import sys
import threading
import time
from uuid import UUID

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.queue import Queues, build_queues
from core.state import InvalidTransition

GRAPH = [("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])]
WORK_SECONDS = 2.0
TIMEOUT_SECONDS = 60


def fake_worker(name: str, queues: Queues, stop: threading.Event, t0: float) -> None:
    def say(text: str) -> None:
        print(f"{time.time() - t0:5.1f}s  {name}  {text}", flush=True)

    while not stop.is_set():
        for message in queues.tasks.read(name, count=1, block_ms=300):
            task_id = UUID(message.fields["task_id"])
            mission_id = UUID(message.fields["mission_id"])
            try:
                with get_conn() as conn:  # claim: READY -> RUNNING (only one worker can win)
                    task = repo.set_task_status(conn, task_id, TaskStatus.RUNNING)
            except InvalidTransition:
                queues.tasks.ack(message.id)  # not ours to run (e.g. left over from another run)
                continue
            say(f"started   {task.key}   (mission {str(mission_id)[:8]})")
            time.sleep(WORK_SECONDS)
            with get_conn() as conn:
                repo.set_task_status(conn, task_id, TaskStatus.COMPLETED, output=f"output of {task.key}")
            say(f"finished  {task.key}   (mission {str(mission_id)[:8]})")
            queues.publish_event(mission_id, task_id)  # tell the scheduler something changed
            queues.tasks.ack(message.id)


def main() -> None:
    queues = build_queues()
    queues.ensure_groups()

    with get_conn() as conn:
        agent = repo.get_agent_by_name(conn, "researcher")
        if agent is None:
            raise SystemExit("run `make seed` first")
        mission = repo.create_mission(conn, "demo: scheduler diamond")
        ids = {}
        for key, deps in GRAPH:
            task = repo.create_task(
                conn, mission.id, key, agent.id, f"demo task {key}", depends_on=[ids[d] for d in deps]
            )
            ids[key] = task.id
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)

    print(f"mission {mission.id}: a -> (b, c) -> d, {WORK_SECONDS:.0f}s of work per task\n")
    t0 = time.time()
    stop = threading.Event()
    workers = [
        threading.Thread(target=fake_worker, args=(f"worker-{i}", queues, stop, t0), daemon=True)
        for i in (1, 2)
    ]
    for w in workers:
        w.start()

    queues.publish_event(mission.id)  # "this mission is ready to be scheduled"

    finished = None
    while time.time() - t0 < TIMEOUT_SECONDS:
        with get_conn() as conn:
            finished = repo.get_mission(conn, mission.id)
        if finished.status in (MissionStatus.COMPLETED, MissionStatus.FAILED):
            break
        time.sleep(0.2)
    stop.set()

    if finished.status not in (MissionStatus.COMPLETED, MissionStatus.FAILED):
        sys.exit("\nnothing happened: is the scheduler running? start it with `make scheduler`")
    print(f"\n{time.time() - t0:.1f}s  mission {finished.status.value}: {finished.result or finished.error}")
    print(f"       (run one after another this would take {len(GRAPH) * WORK_SECONDS:.0f}s)")


if __name__ == "__main__":
    main()