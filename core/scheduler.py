import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

import redis

from core import repository as repo
from core.db import get_conn
from core.models import Mission, MissionStatus, TaskStatus
from core.queue import Message, Queues
from core.readiness import nodes_from_tasks, plan_next
from core.results import compose_result, describe_failure, in_dependency_order
from core.state import InvalidTransition

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduleReport:
    enqueued: tuple[UUID, ...] = ()
    skipped: tuple[UUID, ...] = ()
    finished: bool = False


def _mark(database_url: str | None, task_id: UUID, status: TaskStatus) -> bool:
    """Move a task to `status`. False if someone else already moved it (we lost a race)."""
    try:
        with get_conn(database_url) as conn:
            repo.set_task_status(conn, task_id, status)
    except InvalidTransition:
        return False
    return True


def _finish(
    database_url: str | None, mission_id: UUID, status: MissionStatus, **kwargs
) -> Mission | None:
    with get_conn(database_url) as conn:
        return repo.finish_mission_if_running(conn, mission_id, status, **kwargs)


def schedule_mission(
    mission_id: UUID, queues: Queues, *, database_url: str | None = None
) -> ScheduleReport:
    """One scheduling pass over a mission: skip what can never run, enqueue what is ready,
    and finish the mission if nothing is left.

    Safe to call at any time, any number of times, from several schedulers at once:
    every change is a guarded database update, so each task is skipped or enqueued once.
    """
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is None or mission.status != MissionStatus.RUNNING:
            return ScheduleReport()  # unknown, not planned yet, or already over
        tasks = repo.get_tasks(conn, mission_id)

    if not tasks:
        _finish(database_url, mission_id, MissionStatus.FAILED, error="mission has no tasks")
        return ScheduleReport(finished=True)

    decision = plan_next(nodes_from_tasks(tasks))
    status = {t.id: t.status for t in tasks}

    skipped: list[UUID] = []
    for task_id in decision.skip:
        if _mark(database_url, task_id, TaskStatus.SKIPPED):
            skipped.append(task_id)
        status[task_id] = TaskStatus.SKIPPED

    enqueued: list[UUID] = []
    for task_id in decision.ready:
        # Mark READY first, then publish: a worker that receives the message must find the task
        # READY. If we lose the race here, another scheduler is enqueueing it, so we must not.
        if _mark(database_url, task_id, TaskStatus.READY):
            queues.publish_task(mission_id, task_id)
            enqueued.append(task_id)
        status[task_id] = TaskStatus.READY

    if decision.finished:
        ordered = in_dependency_order(tasks)
        if decision.succeeded:
            outputs = {t.id: t.output or "" for t in ordered}
            done = _finish(
                database_url,
                mission_id,
                MissionStatus.COMPLETED,
                result=compose_result(ordered, outputs),
            )
        else:
            failed = [t for t in ordered if status[t.id] == TaskStatus.FAILED]
            skipped_tasks = [t for t in ordered if status[t.id] == TaskStatus.SKIPPED]
            errors = {t.id: t.last_error or "unknown error" for t in failed}
            done = _finish(
                database_url,
                mission_id,
                MissionStatus.FAILED,
                error=describe_failure(failed, skipped_tasks, errors),
            )
        if done is not None:
            log.info("mission %s %s", mission_id, done.status.value)

    if enqueued or skipped:
        log.info(
            "mission %s: enqueued %d, skipped %d", mission_id, len(enqueued), len(skipped)
        )
    return ScheduleReport(tuple(enqueued), tuple(skipped), decision.finished)


def run_scheduler(
    queues: Queues,
    *,
    consumer: str,
    stop: threading.Event,
    database_url: str | None = None,
    block_ms: int = 1000,
    batch: int = 20,
    retry_delay: float = 1.0,
) -> None:
    """Wake up on mission events and run a scheduling pass for each mission mentioned.

    Several copies can run at once (use a different `consumer` name for each): the consumer
    group hands every event to exactly one of them.
    """
    queues.ensure_groups()
    log.info("scheduler %s started", consumer)

    while not stop.is_set():
        try:
            # First anything we received before but never acknowledged (a failed pass, a restart),
            # then wait for new events.
            messages = queues.events.read(consumer, count=batch, pending=True)
            if not messages:
                messages = queues.events.read(consumer, count=batch, block_ms=block_ms)
        except redis.RedisError:
            log.exception("cannot read the events stream; retrying")
            stop.wait(retry_delay)
            continue

        by_mission: dict[UUID, list[Message]] = defaultdict(list)
        for message in messages:
            try:
                by_mission[UUID(message.fields["mission_id"])].append(message)
            except (KeyError, ValueError):
                log.warning("dropping malformed event %s: %s", message.id, message.fields)
                queues.events.ack(message.id)

        failed_any = False
        # Many events for one mission only need one pass.
        for mission_id, group in by_mission.items():
            try:
                schedule_mission(mission_id, queues, database_url=database_url)
            except Exception:  # keeps the loop alive; the events stay pending and are retried
                log.exception("scheduling pass failed for mission %s", mission_id)
                failed_any = True
                continue
            for message in group:
                queues.events.ack(message.id)

        if failed_any:
            stop.wait(retry_delay)

    log.info("scheduler %s stopped", consumer)