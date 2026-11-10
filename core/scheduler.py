import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

import redis

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.models import Mission, MissionStatus, TaskStatus
from core.queue import Message, Queues
from core.readiness import nodes_from_tasks, plan_next
from core.reaper import reap_expired
from core.results import compose_result, describe_failure, in_dependency_order
from core.retry import Backoff
from core.state import InvalidTransition
from core.sweeper import sweep_ready

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
            # Stamp only AFTER publishing. A crash between the two leaves the task unstamped, and
            # the sweeper publishes it within a few seconds (see repo.mark_enqueued).
            with get_conn(database_url) as conn:
                repo.mark_enqueued(conn, task_id)
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


def _ack(queues: Queues, message: Message) -> None:
    """Acknowledge an event. If Redis is unreachable right now the event simply stays pending and
    is handled again later; a scheduling pass is idempotent, so that costs nothing. What must not
    happen is the exception ending the scheduler loop."""
    try:
        queues.events.ack(message.id)
    except redis.RedisError:
        log.exception("could not acknowledge event %s; it will be handled again", message.id)


@dataclass(frozen=True)
class MissionSweepReport:
    rescheduled: tuple[UUID, ...] = ()  # stalled missions the scheduler pass was re-run for
    failed: tuple[UUID, ...] = ()  # missions declared failed: planning never finished


def sweep_missions(
    queues: Queues,
    *,
    stale_seconds: float | None = None,
    planning_stale_seconds: float | None = None,
    database_url: str | None = None,
) -> MissionSweepReport:
    """The safety net under the event stream.

    Events wake the scheduler, but an event can be lost (a crash between a commit and its publish).
    This looks at the database instead: a running mission with nothing in flight and no activity
    for a while gets a scheduling pass, which is idempotent; a mission stuck pending or planning
    for far too long is failed. Both repair a mission that would otherwise wait forever.
    """
    stale_seconds = settings.mission_stale_seconds if stale_seconds is None else stale_seconds
    planning_stale_seconds = (
        settings.planning_stale_seconds if planning_stale_seconds is None else planning_stale_seconds
    )
    with get_conn(database_url) as conn:
        stalled = repo.find_stalled_missions(conn, stale_seconds=stale_seconds)
        failed = repo.fail_stale_planning(conn, stale_seconds=planning_stale_seconds)
    for mission_id in stalled:
        report = schedule_mission(mission_id, queues, database_url=database_url)
        log.warning(
            "mission %s had stalled; scheduling pass queued %d, skipped %d, finished=%s",
            mission_id, len(report.enqueued), len(report.skipped), report.finished,
        )
    for mission in failed:
        log.error("mission %s: %s", mission.id, mission.error)
    return MissionSweepReport(tuple(stalled), tuple(m.id for m in failed))


def run_scheduler(
    queues: Queues,
    *,
    consumer: str,
    stop: threading.Event,
    database_url: str | None = None,
    block_ms: int = 1000,
    batch: int = 20,
    retry_delay: float = 1.0,
    reap_interval: float | None = None,
    lease_seconds: float | None = None,
    sweep_interval: float | None = None,
    republish_after: float | None = None,
    backoff: Backoff | None = None,
    mission_sweep_interval: float | None = None,
    mission_stale_seconds: float | None = None,
    planning_stale_seconds: float | None = None,
) -> None:
    """Wake up on mission events and run a scheduling pass for each mission mentioned.

    With `reap_interval` set, the same loop also recovers tasks whose worker died every that
    many seconds (see core/reaper.py); with `sweep_interval` set it also publishes READY tasks
    that are due and have no queue message (see core/sweeper.py): retries whose backoff is over,
    and anything whose message was lost; with `mission_sweep_interval` set it also checks for
    missions that stopped making progress (see `sweep_missions`). Several schedulers may do any of
    these at once; all are race-safe.

    Several copies can run at once (use a different `consumer` name for each): the consumer
    group hands every event to exactly one of them.
    """
    queues.ensure_groups()
    log.info("scheduler %s started", consumer)
    next_reap = next_sweep = next_mission_sweep = time.monotonic()

    while not stop.is_set():
        if reap_interval is not None and time.monotonic() >= next_reap:
            next_reap = time.monotonic() + reap_interval
            try:
                reap_expired(
                    queues, lease_seconds=lease_seconds, database_url=database_url, backoff=backoff
                )
            except Exception:  # logged below; the next interval tries again
                log.exception("reaper pass failed")
        if sweep_interval is not None and time.monotonic() >= next_sweep:
            next_sweep = time.monotonic() + sweep_interval
            try:
                sweep_ready(queues, republish_after=republish_after, database_url=database_url)
            except Exception:  # logged below; the next interval tries again
                log.exception("sweeper pass failed")
        if mission_sweep_interval is not None and time.monotonic() >= next_mission_sweep:
            next_mission_sweep = time.monotonic() + mission_sweep_interval
            try:
                sweep_missions(
                    queues,
                    stale_seconds=mission_stale_seconds,
                    planning_stale_seconds=planning_stale_seconds,
                    database_url=database_url,
                )
            except Exception:  # logged below; the next interval tries again
                log.exception("mission sweep failed")
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
                _ack(queues, message)

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
                _ack(queues, message)

        if failed_any:
            stop.wait(retry_delay)

    log.info("scheduler %s stopped", consumer)