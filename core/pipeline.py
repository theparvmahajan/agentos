import logging
import time
from uuid import UUID

from core import repository as repo
from core.db import get_conn
from core.llm import LLMProvider
from core.models import MissionStatus, TaskStatus
from core.planner import run_planner
from core.queue import Queues

log = logging.getLogger(__name__)

# Waits between attempts to hand a planned mission to the scheduler (so 3 attempts in total).
PUBLISH_RETRY_DELAYS = (0.2, 0.5)


def _abandon(database_url: str | None, mission_id: UUID, message: str) -> None:
    """Fail a planned mission whose hand-off to the scheduler never worked, and cancel its tasks
    so nothing is left looking runnable. Never raises."""
    try:
        with get_conn(database_url) as conn:
            repo.finish_mission_if_running(conn, mission_id, MissionStatus.FAILED, error=message)
            for task in repo.get_tasks(conn, mission_id):
                if task.status == TaskStatus.PENDING:
                    repo.set_task_status(conn, task.id, TaskStatus.CANCELLED)
    except Exception:
        log.exception("could not mark mission %s as failed", mission_id)


def plan_and_publish(
    mission_id: UUID,
    *,
    queues: Queues,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
) -> None:
    """Plan a pending mission, then wake the scheduler with one event. The scheduler and the
    workers do everything after that.

    Runs as a background job, so it never raises: nobody is waiting to catch an exception.
    Failures are recorded on the mission instead.
    """
    try:
        run_planner(mission_id, provider=provider, database_url=database_url)
    except Exception:  # the planner already marked the mission failed if it got that far
        log.exception("planning did not complete for mission %s", mission_id)
        return

    last_error: Exception | None = None
    for delay in (0.0, *PUBLISH_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            queues.publish_event(mission_id)
        except Exception as e:  # noqa: BLE001 - Redis may be briefly down; retried, then recorded
            last_error = e
            log.warning("could not publish mission %s to the scheduler: %r", mission_id, e)
        else:
            log.info("mission %s planned and handed to the scheduler", mission_id)
            return

    _abandon(
        database_url,
        mission_id,
        f"could not hand the mission to the scheduler: {type(last_error).__name__}: {last_error}",
    )
