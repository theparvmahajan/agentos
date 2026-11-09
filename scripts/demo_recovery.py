"""Kill a worker in the middle of a task and watch the system recover.

    python -m scripts.demo_recovery

Needs Postgres and Redis (`make up && make migrate`). No LLM, no network, no cost: the task is a
fixed-length sleep (the benchmark's "sleeper" agent). It starts a real scheduler process and a
real worker process on private streams, then kills the worker with no warning (`Popen.kill`:
SIGKILL on Linux/macOS, TerminateProcess on Windows, so the worker gets no chance to clean up).

What you should see: the task is running under worker w1's lease; w1 dies; the lease stops being
renewed and runs out; the scheduler's reaper puts the task back as `ready` with a short backoff delay; the sweeper
publishes it when the delay is over; worker w2 runs it as attempt 2; the mission completes. The lease
is short here (3 s) so this takes about fifteen seconds; the default is 30 s.
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus
from core.queue import build_queues, get_redis

ROOT = Path(__file__).resolve().parents[1]
LEASE, HEARTBEAT, REAPER = "3", "1", "1"
TASK_SECONDS = 6


def spawn(module: str, prefix: str, **extra: str) -> subprocess.Popen:
    env = {
        **os.environ, "QUEUE_PREFIX": prefix, "TASK_LEASE_SECONDS": LEASE,
        "LEASE_HEARTBEAT_SECONDS": HEARTBEAT, "REAPER_INTERVAL_SECONDS": REAPER, **extra,
    }
    return subprocess.Popen(
        [sys.executable, "-m", module], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def snapshot(task_id) -> tuple[tuple, str]:
    """(what counts as a change, a line to print). The seconds left are not part of the key: they
    tick down and jump back up at every heartbeat, which would print a line per poll."""
    with get_conn() as conn:
        t = repo.get_task(conn, task_id)
        lease = conn.execute(
            "SELECT owner, attempt, round(extract(epoch FROM expires_at - now())::numeric, 1) AS left "
            "FROM task_leases WHERE task_id = %s", (task_id,),
        ).fetchone()
    held = f"lease: {lease['owner']} attempt {lease['attempt']}, {lease['left']}s left" if lease else "no lease"
    key = (t.status, t.attempts, lease["owner"] if lease else None)
    return key, f"{t.status.value:<9} attempts={t.attempts}  {held}"


def main() -> None:
    prefix = f"demo-recovery-{uuid.uuid4().hex[:6]}"
    queues = build_queues(prefix=prefix)
    queues.ensure_groups()
    procs: list[subprocess.Popen] = []
    with get_conn() as conn:
        agent = repo.get_agent_by_name(conn, "sleeper")
        created_agent = agent is None
        if created_agent:
            agent = repo.create_agent(conn, "sleeper", "demo only", capabilities=[])
        mission = repo.create_mission(conn, "[demo] kill a worker mid-task")
        a = repo.create_task(conn, mission.id, "a", agent.id, f"sleep {TASK_SECONDS}")
        repo.create_task(conn, mission.id, "b", agent.id, "sleep 0.5", depends_on=[a.id])
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)

    started = time.time()

    def say(text: str) -> None:
        print(f"[{time.time() - started:5.1f}s] {text}", flush=True)

    try:
        procs.append(spawn("services.scheduler", prefix))
        w1 = spawn("services.worker", prefix, WORKER_NAME="w1")
        procs.append(w1)
        queues.publish_event(mission.id)

        deadline = time.time() + 30
        while snapshot(a.id)[0][0].value != "running" and time.time() < deadline:
            time.sleep(0.1)
        last, line = snapshot(a.id)
        say(f"task a        {line}")
        w1.kill()
        w1.wait()
        say("KILLED worker w1 (no cleanup ran)")
        procs.append(spawn("services.worker", prefix, WORKER_NAME="w2"))
        say("started worker w2")

        while time.time() < deadline + 30:
            with get_conn() as conn:
                state = repo.get_mission(conn, mission.id).status
            key, line = snapshot(a.id)
            if key != last:
                say(f"task a        {line}")
                last = key
            if state in (MissionStatus.COMPLETED, MissionStatus.FAILED):
                break
            time.sleep(0.5)

        with get_conn() as conn:
            final = repo.get_mission(conn, mission.id)
            task_a = repo.get_task(conn, a.id)
        say(f"mission {final.status.value}; task a attempts={task_a.attempts}")
        print(f"\ntask a last_error: {task_a.last_error}")
        ok = final.status == MissionStatus.COMPLETED and task_a.attempts == 2
        print("RECOVERED" if ok else "NOT RECOVERED: see the log lines above")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        get_redis().delete(queues.tasks.name, queues.events.name)
        with get_conn() as conn:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission.id,))
            if created_agent:
                conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


if __name__ == "__main__":
    main()
