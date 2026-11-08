"""A worker that is about to be killed. Started by test_crash_recovery.py as a real process:

    python -m tests.integration.crash_child <queue prefix> <agent name> <database url>

It takes one task, starts "working" on it for ten minutes, and keeps its lease alive with a
heartbeat, exactly like a real worker. The test kills it mid-task.
"""

import sys
import threading
import time

from core.queue import build_queues
from core.runner import AgentResult
from core.tools import ToolRegistry
from core.worker import run_worker


def runner(agent, instruction, dependencies, *, tools, provider=None):
    time.sleep(600)
    return AgentResult(output="never reached")


if __name__ == "__main__":
    prefix, agent_name, database_url = sys.argv[1:4]
    run_worker(
        build_queues(prefix=prefix),
        consumer="doomed",
        stop=threading.Event(),
        runners={agent_name: runner},
        tools=ToolRegistry(),
        database_url=database_url,
        block_ms=200,
        lease_seconds=1.5,
        heartbeat_seconds=0.4,
    )
