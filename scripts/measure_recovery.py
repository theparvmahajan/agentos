"""Measure how long the system takes to recover when a worker is killed mid-task.

    python -m scripts.measure_recovery --trials 5                  # with the shipped defaults
    python -m scripts.measure_recovery --trials 10 --fast          # short lease: quick to run

Needs Postgres and Redis (`make up && make migrate`). No LLM, no network, no cost.

Each trial starts a REAL scheduler process and two REAL worker processes on private streams, lets
one worker start a task, kills that worker with no warning (`Popen.kill`), and measures the time
from the kill until another worker STARTS the task again (attempt 2). That span is what a user would
feel as "the mission froze": it contains the lease running out, the reaper noticing, the retry's
backoff delay and the sweeper publishing the task. (How long attempt 2 then takes to finish is the
task's own duration, not a property of recovery.)

The result is printed as a markdown table with the settings used, the machine, and the number of
trials. With few trials only min / median / max are meaningful, so only those are printed; do not
quote a percentile from five runs.
"""

import argparse
import os
import platform
import random
import statistics
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
PROFILES = {
    "defaults": {"TASK_LEASE_SECONDS": "30", "LEASE_HEARTBEAT_SECONDS": "10", "REAPER_INTERVAL_SECONDS": "10",
                 "SWEEP_INTERVAL_SECONDS": "2", "RETRY_BASE_SECONDS": "2", "RETRY_MAX_SECONDS": "60"},
    "fast": {"TASK_LEASE_SECONDS": "3", "LEASE_HEARTBEAT_SECONDS": "1", "REAPER_INTERVAL_SECONDS": "1",
             "SWEEP_INTERVAL_SECONDS": "0.2", "RETRY_BASE_SECONDS": "0.5", "RETRY_MAX_SECONDS": "5"},
}


def spawn(module: str, prefix: str, settings: dict, **extra: str) -> subprocess.Popen:
    env = {**os.environ, **settings, "QUEUE_PREFIX": prefix, **extra}
    # MEASURE_LOGDIR=some/dir keeps each process's log, to see WHY a trial went wrong.
    logdir = os.environ.get("MEASURE_LOGDIR")
    out = subprocess.DEVNULL
    if logdir:
        Path(logdir).mkdir(parents=True, exist_ok=True)
        out = open(Path(logdir) / f"{extra.get('WORKER_NAME', module.split('.')[-1])}.log", "w")  # noqa: SIM115
    return subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, env=env, stdout=out, stderr=out)


def one_trial(settings: dict, task_seconds: int) -> float:
    prefix = f"measure-recovery-{uuid.uuid4().hex[:6]}"
    queues = build_queues(prefix=prefix)
    queues.ensure_groups()
    procs: dict[str, subprocess.Popen] = {}
    with get_conn() as conn:
        agent = repo.get_agent_by_name(conn, "sleeper")
        created_agent = agent is None
        if created_agent:
            agent = repo.create_agent(conn, "sleeper", "measurement only", capabilities=[])
        mission = repo.create_mission(conn, "[measure] worker kill")
        task = repo.create_task(conn, mission.id, "a", agent.id, f"sleep {task_seconds}")
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
    try:
        procs["scheduler"] = spawn("services.scheduler", prefix, settings)
        procs["w1"] = spawn("services.worker", prefix, settings, WORKER_NAME="w1")
        procs["w2"] = spawn("services.worker", prefix, settings, WORKER_NAME="w2")
        time.sleep(3)  # let all three processes finish starting before the task is queued
        queues.publish_event(mission.id)

        def state():
            with get_conn() as conn:
                t = repo.get_task(conn, task.id)
                lease = conn.execute("SELECT owner FROM task_leases WHERE task_id = %s", (task.id,)).fetchone()
            return t.status.value, t.attempts, lease["owner"] if lease else None

        deadline = time.time() + 60
        while state()[:2] != ("running", 1) and time.time() < deadline:
            time.sleep(0.05)
        victim = state()[2]  # whichever worker took the task
        if victim is None:
            raise RuntimeError("no worker started the task within 60 s")
        # Kill at a RANDOM point in the heartbeat cycle. A fixed delay would always hit the same
        # phase and make every trial look alike; a real crash can happen at any moment.
        time.sleep(1.0 + random.uniform(0, float(settings["LEASE_HEARTBEAT_SECONDS"])))
        procs[victim.rsplit("-", 1)[0]].kill()
        killed_at = time.time()

        limit = killed_at + float(os.environ.get("MEASURE_GIVE_UP", 4 * float(settings["TASK_LEASE_SECONDS"]) + 60))
        while time.time() < limit:
            status, attempts, _ = state()
            if attempts == 2 and status == "running":
                return time.time() - killed_at
            time.sleep(0.05)
        raise RuntimeError("the task was not restarted in time")
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
        for proc in procs.values():
            proc.wait(10)
        get_redis().delete(queues.tasks.name, queues.events.name)
        with get_conn() as conn:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission.id,))
            if created_agent:
                conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--fast", action="store_true", help="short lease (3 s) so trials take seconds")
    args = parser.parse_args()
    profile = "fast" if args.fast else "defaults"
    settings = PROFILES[profile]
    task_seconds = 25 if not args.fast else 20  # the sleeper agent refuses sleeps over 30 s

    times = []
    for i in range(args.trials):
        seconds = one_trial(settings, task_seconds)
        times.append(seconds)
        print(f"trial {i + 1}/{args.trials}: recovered in {seconds:.1f} s", flush=True)

    lease, hb = float(settings["TASK_LEASE_SECONDS"]), float(settings["LEASE_HEARTBEAT_SECONDS"])
    reaper, sweep = float(settings["REAPER_INTERVAL_SECONDS"]), float(settings["SWEEP_INTERVAL_SECONDS"])
    base = float(settings["RETRY_BASE_SECONDS"])
    low = (lease - hb) + base / 2  # at best the last heartbeat was just before the kill
    high = lease + reaper + base + sweep
    print("\n### Recovery time after `kill -9` of a worker mid-task\n")
    print(f"- profile: **{profile}** (" + ", ".join(f"{k}={v}" for k, v in settings.items()) + ")")
    print(f"- trials: **{len(times)}**; machine: {platform.platform()}, {os.cpu_count()} CPUs, Python {platform.python_version()}")
    print(f"- polling resolution: about 0.05 s; expected range from the settings: {low:.1f} to {high:.1f} s\n")
    print("| min | median | mean | max |")
    print("|---|---|---|---|")
    print(f"| {min(times):.1f} s | {statistics.median(times):.1f} s | {statistics.mean(times):.1f} s | {max(times):.1f} s |")


if __name__ == "__main__":
    main()
