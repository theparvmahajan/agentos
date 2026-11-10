"""A worker as a real process, for tests that kill it.

    python -m tests.chaos.child_worker <queue prefix> <agent name> <database url> <seconds per step>
"""

import sys
import threading
import time

from core.queue import build_queues
from core.retry import Backoff
from core.runner import AgentResult
from core.tools import ToolRegistry
from core.worker import run_worker


def make_runner(seconds: float):
    def run(agent, instruction, dependencies, *, tools, provider=None):
        time.sleep(seconds)
        return AgentResult(output=f"{instruction} done")

    return run


if __name__ == "__main__":
    prefix, agent_name, database_url, seconds = sys.argv[1:5]
    run_worker(
        build_queues(prefix=prefix),
        consumer="chaos-worker",  # a stable name: a restarted worker replays what it never finished
        stop=threading.Event(),
        runners={agent_name: make_runner(float(seconds))},
        tools=ToolRegistry(),
        database_url=database_url,
        block_ms=100,
        retry_delay=0.2,
        lease_seconds=2.0,
        heartbeat_seconds=0.5,
        backoff=Backoff(0.05, 0.1),
    )
