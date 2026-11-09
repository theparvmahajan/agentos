"""Watch retries with backoff and a task timeout rescue two flaky tasks.

    python -m scripts.demo_retries

Needs Postgres and Redis (`make up && make migrate`). No LLM, no network, no cost. It runs a real
scheduler and worker (as threads, on private streams) and two tasks that misbehave on purpose:

  flaky : fails twice with a transient error ("the LLM is busy"), then works
  slow  : hangs on its first attempt (longer than the 1 s timeout used here), then works

What you should see, for each task: attempt 1 fails or times out; the task goes back to `ready` and
WAITS (the "due in" countdown is the backoff delay, stored in the database); the sweeper publishes
it when the wait is over; attempt 2 (and 3 for `flaky`) runs; the mission completes. The delays
double (about 0.5-1 s, then 1-2 s) and are partly random, so your numbers will differ from run to run.
"""

import logging
import threading
import time
import uuid

from core import repository as repo
from core.db import get_conn
from core.llm.base import LLMError
from core.models import MissionStatus
from core.queue import build_queues, get_redis
from core.retry import Backoff
from core.runner import AgentResult
from core.scheduler import run_scheduler
from core.tools import ToolRegistry
from core.worker import run_worker

BACKOFF = Backoff(base=1.0, cap=8.0)
TIMEOUT = 1.0
AGENT = "retry-demo"


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="        worker log: %(message)s")
    prefix = f"demo-retries-{uuid.uuid4().hex[:6]}"
    queues = build_queues(prefix=prefix)
    queues.ensure_groups()
    attempts = {"flaky": 0, "slow": 0}

    def runner(agent, instruction, dependencies, *, tools, provider=None):
        attempts[instruction] += 1
        n = attempts[instruction]
        if instruction == "flaky" and n < 3:
            raise LLMError(f"the model server is busy (HTTP 503, call {n})", retryable=True)
        if instruction == "slow" and n == 1:
            time.sleep(5)  # longer than TIMEOUT: the worker gives up waiting
        return AgentResult(output=f"{instruction} worked on attempt {n}")

    with get_conn() as conn:
        agent = repo.create_agent(conn, AGENT, "demo only", capabilities=[])
        mission = repo.create_mission(conn, "[demo] retries and timeouts")
        tasks = {
            name: repo.create_task(conn, mission.id, name, agent.id, name, max_attempts=4)
            for name in ("flaky", "slow")
        }
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)

    stop = threading.Event()
    threads = [
        threading.Thread(target=run_scheduler, daemon=True, kwargs={
            "queues": queues, "consumer": "demo-scheduler", "stop": stop,
            "block_ms": 200, "sweep_interval": 0.2, "backoff": BACKOFF}),
        threading.Thread(target=run_worker, daemon=True, kwargs={
            "queues": queues, "consumer": "demo-worker", "stop": stop,
            "runners": {AGENT: runner}, "tools": ToolRegistry(), "block_ms": 200,
            "backoff": BACKOFF, "timeout_seconds": TIMEOUT}),
    ]
    started = time.time()

    def say(text: str) -> None:
        print(f"[{time.time() - started:5.1f}s] {text}", flush=True)

    def describe(task_id) -> tuple[tuple, str]:
        with get_conn() as conn:
            t = repo.get_task(conn, task_id)
            d = conn.execute(
                "SELECT round(extract(epoch FROM run_after - now())::numeric, 1) AS due "
                "FROM task_dispatch WHERE task_id = %s", (task_id,),
            ).fetchone()
        waiting = f"  due in {d['due']}s" if d and d["due"] is not None and d["due"] > 0 else ""
        error = f"  last error: {t.last_error}" if t.last_error and t.status.value == "ready" else ""
        return (t.status, t.attempts, bool(waiting)), f"{t.status.value:<9} attempts={t.attempts}{waiting}{error}"

    try:
        for thread in threads:
            thread.start()
        queues.publish_event(mission.id)
        seen: dict = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            for name, task in tasks.items():
                key, line = describe(task.id)
                if seen.get(name) != key:
                    seen[name] = key
                    say(f"{name:<6} {line}")
            with get_conn() as conn:
                status = repo.get_mission(conn, mission.id).status
            if status in (MissionStatus.COMPLETED, MissionStatus.FAILED):
                break
            time.sleep(0.15)
        with get_conn() as conn:
            final = repo.get_mission(conn, mission.id)
            outputs = {n: repo.get_task(conn, t.id).output for n, t in tasks.items()}
        say(f"mission {final.status.value}")
        for name, output in outputs.items():
            print(f"        {name}: {output}")
        print("RECOVERED" if final.status == MissionStatus.COMPLETED else "NOT RECOVERED")
    finally:
        stop.set()
        for thread in threads:
            thread.join(5)
        get_redis().delete(queues.tasks.name, queues.events.name)
        with get_conn() as conn:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission.id,))
            conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


if __name__ == "__main__":
    main()
