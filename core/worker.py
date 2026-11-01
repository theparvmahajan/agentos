import logging
import threading
import time
from enum import Enum
from uuid import UUID

import redis

from core import repository as repo
from core.db import get_conn
from core.llm import LLMProvider
from core.models import Task, TaskStatus
from core.queue import Message, Queues
from core.runner import DependencyOutput, Runner
from core.state import TASK_TERMINAL, InvalidTransition
from core.tools import ToolRegistry

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE = "duplicate"  # nothing to run: someone else has the task, or it already finished
    DROPPED = "dropped"  # malformed message, or a task that does not exist


def _handle_duplicate(
    message: Message, queues: Queues, task: Task, database_url: str | None
) -> Outcome:
    with get_conn(database_url) as conn:
        current = repo.get_task(conn, task.id)
    if current is not None and current.status in TASK_TERMINAL:
        # Already finished. Perhaps its worker died after saving the result but before announcing
        # it, so announce again. A repeated announcement is harmless: scheduling is idempotent.
        queues.publish_event(task.mission_id, task.id)
    log.info("task %s: duplicate message, nothing to run", task.id)
    queues.tasks.ack(message.id)
    return Outcome.DUPLICATE


def handle_task_message(
    message: Message,
    queues: Queues,
    *,
    runners: dict[str, Runner],
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
) -> Outcome:
    """Run the task a message points at, record the result, announce it, acknowledge the message.

    Acknowledges the message in every normal case. If the database or Redis fails, the exception
    propagates and the message stays pending, so it is delivered again.
    """
    try:
        task_id = UUID(message.fields["task_id"])
    except (KeyError, ValueError):
        log.warning("dropping malformed task message %s: %s", message.id, message.fields)
        queues.tasks.ack(message.id)
        return Outcome.DROPPED

    # Read everything needed BEFORE claiming, so a failure here changes nothing.
    with get_conn(database_url) as conn:
        task = repo.get_task(conn, task_id)
        if task is not None:
            mission_tasks = repo.get_tasks(conn, task.mission_id)
            agents = {a.id: a for a in repo.list_agents(conn)}
    if task is None:
        log.warning("dropping message for unknown task %s", task_id)
        queues.tasks.ack(message.id)
        return Outcome.DROPPED

    # Claim: READY -> RUNNING is a guarded update, so only one worker can win it,
    # however many times the message was delivered.
    try:
        with get_conn(database_url) as conn:
            repo.set_task_status(conn, task_id, TaskStatus.RUNNING)
    except InvalidTransition:
        return _handle_duplicate(message, queues, task, database_url)

    by_id = {t.id: t for t in mission_tasks}
    agent = agents[task.agent_id]
    log.info("task %s (%s) running on %s", task.key, task.mission_id, agent.name)
    started = time.perf_counter()

    try:
        runner = runners.get(agent.name)
        if runner is None:
            raise LookupError(f"no runner registered for agent '{agent.name}'")
        dependencies = [
            DependencyOutput(by_id[d].key, by_id[d].instruction, by_id[d].output or "")
            for d in task.depends_on
        ]
        result = runner(agent, task.instruction, dependencies, tools=tools, provider=provider)
    except Exception as e:  # noqa: BLE001 - any runner failure is recorded on the task
        error = f"{type(e).__name__}: {e}"
        log.warning("task %s failed after %.1fs: %s", task.key, time.perf_counter() - started, error)
        with get_conn(database_url) as conn:
            repo.set_task_status(conn, task_id, TaskStatus.FAILED, error=error)
        outcome = Outcome.FAILED
    else:
        with get_conn(database_url) as conn:
            repo.set_task_status(conn, task_id, TaskStatus.COMPLETED, output=result.output)
        log.info(
            "task %s completed in %.1fs (%d steps, %d tool calls)",
            task.key,
            time.perf_counter() - started,
            result.steps,
            result.tool_calls,
        )
        outcome = Outcome.COMPLETED

    # Order matters: the result is committed above, THEN announced, THEN the message is
    # acknowledged. A crash between any two steps is recovered when the message is redelivered.
    queues.publish_event(task.mission_id, task_id)
    queues.tasks.ack(message.id)
    return outcome


def run_worker(
    queues: Queues,
    *,
    consumer: str,
    stop: threading.Event,
    runners: dict[str, Runner],
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
    block_ms: int = 1000,
    retry_delay: float = 1.0,
) -> None:
    """Take tasks off the stream one at a time. Run several of these (processes or threads, each
    with its own `consumer` name) to run tasks in parallel: the consumer group gives every
    message to exactly one of them."""
    queues.ensure_groups()
    log.info("worker %s started", consumer)

    while not stop.is_set():
        try:
            # First anything this worker received but never finished (a failure, a restart),
            # then wait for new work.
            messages = queues.tasks.read(consumer, count=1, pending=True)
            if not messages:
                messages = queues.tasks.read(consumer, count=1, block_ms=block_ms)
        except redis.RedisError:
            log.exception("cannot read the tasks stream; retrying")
            stop.wait(retry_delay)
            continue

        for message in messages:
            try:
                handle_task_message(
                    message,
                    queues,
                    runners=runners,
                    tools=tools,
                    provider=provider,
                    database_url=database_url,
                )
            except Exception:  # keeps the worker alive; the message stays pending and is retried
                log.exception("task message %s could not be handled; will retry", message.id)
                stop.wait(retry_delay)

    log.info("worker %s stopped", consumer)