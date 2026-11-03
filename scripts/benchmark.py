"""How much faster do distributed workers finish a task graph than the sequential executor?

Every task is a deterministic "sleep D seconds" (agents/sleeper.py), so the numbers measure the
orchestration (scheduling, queues, claiming, bookkeeping), not an LLM's variable latency or a
single GPU serializing calls. The distributed runs use real scheduler and worker PROCESSES on a
private pair of Redis streams.

    python -m scripts.benchmark                      # prints a table and writes docs/BENCHMARKS.md
    python -m scripts.benchmark --duration 0.3 --reps 5 --workers 1 2 4 8

Needs `make up` and `make migrate`. It creates and deletes its own missions and a "sleeper" agent.
"""

import argparse
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Self

from agents.sleeper import run_sleeper
from core import repository as repo
from core.db import get_conn
from core.executor import execute_mission
from core.models import MissionStatus
from core.queue import Queues, build_queues, get_redis
from core.tools import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
BENCH_OBJECTIVE_PREFIX = "[bench] "
TERMINAL = {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}

# A graph shape: task key -> keys it depends on. Dependencies are listed before their dependents.
Shape = dict[str, list[str]]


def chain(n: int = 4) -> Shape:
    return {f"t{i}": ([f"t{i - 1}"] if i > 1 else []) for i in range(1, n + 1)}


def diamond() -> Shape:
    return {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}


def fan_out(width: int = 8) -> Shape:
    return {"root": [], **{f"w{i}": ["root"] for i in range(1, width + 1)}}


SHAPES: dict[str, Shape] = {"chain-4": chain(4), "diamond": diamond(), "fan-out-8": fan_out(8)}


def critical_path(shape: Shape) -> int:
    """The number of tasks on the longest dependency chain: the best possible number of 'rounds'."""
    depth: dict[str, int] = {}
    for key, deps in shape.items():
        depth[key] = 1 + max((depth[d] for d in deps), default=0)
    return max(depth.values())


def ideal_makespan(shape: Shape, workers: int, duration: float) -> float:
    """The fastest possible finish if scheduling cost nothing: tasks run in rounds of up to
    `workers` ready tasks, each round lasting `duration`. (Exact for equal-length tasks on these
    shapes.)"""
    done: set[str] = set()
    rounds = 0
    while len(done) < len(shape):
        ready = [k for k, deps in shape.items() if k not in done and set(deps) <= done]
        done.update(ready[:workers])
        rounds += 1
    return rounds * duration


@dataclass
class Result:
    shape: str
    mode: str  # "sequential executor" or "scheduler + N workers"
    workers: int  # 0 for the sequential executor
    tasks: int
    critical_path: int
    ideal: float
    walls: list[float] = field(default_factory=list)

    @property
    def median(self) -> float:
        return statistics.median(self.walls)


def speedup(sequential_median: float, median: float) -> float:
    return sequential_median / median


def render_table(results: list[Result]) -> str:
    lines = [
        (
            "| Graph | Tasks | Critical path | Mode | Median (s) | Min-max (s) | Ideal (s) "
            "| Speedup vs sequential | Efficiency (ideal / actual) |"
        ),
        "|---|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        base = next(x for x in results if x.shape == r.shape and x.workers == 0)
        lines.append(
            f"| {r.shape} | {r.tasks} | {r.critical_path} | {r.mode} | {r.median:.2f} "
            f"| {min(r.walls):.2f}-{max(r.walls):.2f} | {r.ideal:.2f} "
            f"| {speedup(base.median, r.median):.2f}x | {r.ideal / r.median:.0%} |"
        )
    return "\n".join(lines)


# ---------- running the cases ----------

def create_mission(shape: Shape, duration: float, agent_id, label: str, database_url=None):
    """A RUNNING mission whose tasks are all PENDING, exactly what the planner leaves behind."""
    with get_conn(database_url) as conn:
        mission = repo.create_mission(conn, f"{BENCH_OBJECTIVE_PREFIX}{label}")
        ids = {}
        for key, deps in shape.items():
            task = repo.create_task(
                conn, mission.id, key, agent_id, f"sleep {duration}",
                depends_on=[ids[d] for d in deps],
            )
            ids[key] = task.id
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
    return mission.id


def new_result(name: str, shape: Shape, mode: str, workers: int, duration: float) -> Result:
    return Result(
        name, mode, workers, len(shape), critical_path(shape),
        ideal_makespan(shape, workers or 1, duration),
    )


def run_sequential(shape: Shape, duration: float, agent_id, label: str) -> float:
    mission_id = create_mission(shape, duration, agent_id, label)
    start = time.perf_counter()
    execute_mission(mission_id, runners={"sleeper": run_sleeper}, tools=ToolRegistry())
    return time.perf_counter() - start


def run_distributed(
    shape: Shape, duration: float, agent_id, label: str, queues: Queues, timeout: float = 120
) -> float:
    mission_id = create_mission(shape, duration, agent_id, label)
    start = time.perf_counter()
    queues.publish_event(mission_id)  # exactly what the API does after planning
    while time.perf_counter() - start < timeout:
        with get_conn() as conn:
            mission = repo.get_mission(conn, mission_id)
        if mission.status in TERMINAL:
            wall = time.perf_counter() - start
            if mission.status != MissionStatus.COMPLETED:
                raise RuntimeError(f"benchmark mission ended {mission.status.value}: {mission.error}")
            return wall
        time.sleep(0.01)
    raise TimeoutError("benchmark mission did not finish: are Postgres and Redis up?")


class Cluster:
    """One scheduler process and N worker processes on a private pair of streams."""

    def __init__(self, workers: int):
        self.workers = workers
        self.prefix = f"bench-{uuid.uuid4().hex[:8]}"
        self.procs: list[subprocess.Popen] = []
        self.queues = build_queues(prefix=self.prefix)

    def _spawn(self, module: str, **extra: str) -> subprocess.Popen:
        env = {**os.environ, "QUEUE_PREFIX": self.prefix, **extra}
        return subprocess.Popen(
            [sys.executable, "-m", module], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def __enter__(self) -> Self:
        self.procs.append(self._spawn("services.scheduler"))
        for i in range(self.workers):
            self.procs.append(self._spawn("services.worker", WORKER_NAME=f"{self.prefix}-w{i}"))
        return self

    def __exit__(self, *exc) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        client = get_redis()
        client.delete(self.queues.tasks.name, self.queues.events.name)


def environment_notes(duration: float, reps: int) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip() or "unknown"
    return (
        f"- Date: {date.today().isoformat()}, commit `{commit}`\n"
        f"- Machine: {platform.system()} {platform.release()}, {os.cpu_count()} logical CPUs, "
        f"Python {platform.python_version()}\n"
        f"- Every task sleeps {duration:g}s; {reps} measured runs per cell after one warm-up; "
        f"median reported\n"
        "- Postgres and Redis run locally in Docker; workers and scheduler are separate processes"
    )


def write_report(path: Path, table: str, notes: str) -> None:
    path.write_text(
        "# Parallel execution benchmark\n\n"
        "Generated by `python -m scripts.benchmark`. Do not edit by hand.\n\n"
        f"{notes}\n\n{table}\n\n"
        "## How to read this\n\n"
        "- **Ideal** is the best possible time if scheduling were free: tasks run in rounds of up to "
        "N ready tasks.\n"
        "- **Efficiency** is ideal / actual. The gap is the orchestration overhead: scheduler wake-up, "
        "queue hops, the guarded database claims and status writes.\n"
        "- A **chain** cannot go faster than its critical path however many workers exist. "
        "That is the dependency logic working, not a defect.\n"
        "- Tasks here are sleeps. Real LLM tasks add variable latency, and a single local GPU may "
        "serialize them, so speedups on real agents are lower. These numbers isolate the runtime.\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--duration", type=float, default=0.5, help="seconds each task sleeps")
    parser.add_argument("--reps", type=int, default=3, help="measured runs per cell")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "BENCHMARKS.md")
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args(argv)

    with get_conn() as conn:
        existing = repo.get_agent_by_name(conn, "sleeper")
        agent = existing or repo.create_agent(conn, "sleeper", description="benchmark only")
    results: list[Result] = []
    try:
        for name, shape in SHAPES.items():
            seq = new_result(name, shape, "sequential executor", 0, args.duration)
            run_sequential(shape, 0.05, agent.id, f"{name} warm-up")
            seq.walls = [run_sequential(shape, args.duration, agent.id, name) for _ in range(args.reps)]
            results.append(seq)
            print(f"{name:<10} sequential        median {seq.median:6.2f}s", flush=True)

        for workers in args.workers:
            with Cluster(workers) as cluster:
                run_distributed(SHAPES["diamond"], 0.05, agent.id, "warm-up", cluster.queues)
                for name, shape in SHAPES.items():
                    mode = f"scheduler + {workers} worker{'s' if workers > 1 else ''}"
                    res = new_result(name, shape, mode, workers, args.duration)
                    res.walls = [
                        run_distributed(shape, args.duration, agent.id, name, cluster.queues)
                        for _ in range(args.reps)
                    ]
                    results.append(res)
                    print(f"{name:<10} {res.mode:<18} median {res.median:6.2f}s", flush=True)
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM missions WHERE objective LIKE %s", (BENCH_OBJECTIVE_PREFIX + "%",))
            if existing is None:
                conn.execute("DELETE FROM agents WHERE id = %s", (agent.id,))

    order = {name: i for i, name in enumerate(SHAPES)}
    results.sort(key=lambda r: (order[r.shape], r.workers))
    table = render_table(results)
    print("\n" + table)
    if not args.no_write:
        write_report(args.out, table, environment_notes(args.duration, args.reps))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
