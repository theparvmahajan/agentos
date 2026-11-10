"""Publish READY tasks that are due and have no queue message.

A READY task is supposed to have exactly one message on the tasks stream. Several things can leave
it without one: a retry that is still waiting out its backoff (by design: the message is held back
until `run_after`), a crash between "mark ready" and "publish", a reaper that died after requeueing,
or a message acknowledged by a worker that then refused to run it early.

Rather than make each of those paths perfect, one rule repairs all of them: every pass publishes
the READY tasks whose delay is over and that have no message newer than `ready_republish_seconds`.
The database remembers when a message was last published (`tasks.enqueued_at`), so a task waiting
in a busy queue is not flooded with copies, and a lost message is replaced within a minute.

A duplicate message is harmless: only one worker can claim a task.
"""

import logging
from dataclasses import dataclass, field
from uuid import UUID

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.queue import Queues

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepReport:
    published: tuple[UUID, ...] = field(default_factory=tuple)  # task ids put on the queue


def sweep_ready(
    queues: Queues,
    *,
    republish_after: float | None = None,
    grace: float | None = None,
    database_url: str | None = None,
    limit: int = 100,
) -> SweepReport:
    republish_after = (
        settings.ready_republish_seconds if republish_after is None else republish_after
    )
    grace = settings.ready_grace_seconds if grace is None else grace
    with get_conn(database_url) as conn:
        due = repo.claim_due_ready(conn, republish_after=republish_after, grace=grace, limit=limit)
    # Committed (and stamped) first, published second.
    for mission_id, task_id in due:
        queues.publish_task(mission_id, task_id)
        log.info("task %s: ready and due, published", task_id)
    return SweepReport(tuple(task_id for _, task_id in due))
