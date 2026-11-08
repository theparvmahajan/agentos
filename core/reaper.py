"""Recover tasks whose worker died.

A worker holds a lease on the task it runs and renews it (heartbeat). When the worker is killed
the renewals stop, the lease expires, and a pass of `reap_expired` finds the task still marked
`running`:

    attempts left   -> running -> ready, then publish it again   (a retry)
    no attempts left -> running -> failed, then announce it       (the scheduler skips dependents)

Order of side effects follows the rest of the system: commit the state change FIRST, publish
SECOND. A crash between the two leaves a task `ready` that nobody was told about; that window is
a known gap (a sweeper for stale `ready` tasks is planned), not something this module hides.

Any number of reapers may run at once: each recovery is one guarded UPDATE, so a task is
recovered exactly once.

What this does NOT do: stop the dead worker's program (it is dead) or a *zombie* that is merely
slow. A zombie that wakes up later is refused by the fencing check in `set_task_status`.
Re-running a task is only safe when the task is safe to repeat; today's tools (web search and a
no-network sandbox) are. A tool with real-world side effects must not be retried blindly (M5/M6).
"""

import logging
from dataclasses import dataclass, field
from uuid import UUID

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.queue import Queues

log = logging.getLogger(__name__)

LEASE_EXPIRED = "worker lost: its lease expired after {seconds:g}s without a heartbeat"


@dataclass(frozen=True)
class ReapReport:
    requeued: tuple[UUID, ...] = field(default_factory=tuple)  # task ids sent back to the queue
    failed: tuple[UUID, ...] = field(default_factory=tuple)  # task ids that ran out of attempts


def reap_expired(
    queues: Queues,
    *,
    lease_seconds: float | None = None,
    database_url: str | None = None,
    limit: int = 50,
) -> ReapReport:
    lease_seconds = settings.task_lease_seconds if lease_seconds is None else lease_seconds
    with get_conn(database_url) as conn:
        orphans = repo.find_orphaned_tasks(conn, lease_seconds=lease_seconds, limit=limit)

    requeued: list = []
    failed: list = []
    for task_id, attempt in orphans:
        reason = LEASE_EXPIRED.format(seconds=lease_seconds)
        with get_conn(database_url) as conn:  # one short transaction per task
            recovered = repo.recover_orphaned_task(
                conn, task_id, attempt, lease_seconds=lease_seconds, reason=reason
            )
        if recovered is None:
            continue  # it finished, was renewed, or another reaper took it
        action, task = recovered
        if action == "requeued":
            queues.publish_task(task.mission_id, task.id)
            requeued.append(task.id)
            log.warning(
                "task %s: %s; requeued (attempt %d of %d used)",
                task.key, reason, task.attempts, task.max_attempts,
            )
        else:
            queues.publish_event(task.mission_id, task.id)
            failed.append(task.id)
            log.error("task %s: %s; no attempts left, failed", task.key, reason)
    return ReapReport(tuple(requeued), tuple(failed))
