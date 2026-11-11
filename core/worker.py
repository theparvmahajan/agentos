import logging
import threading
import time
from enum import Enum
from typing import Self
from uuid import UUID

import redis

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.llm import LLMProvider
from core.models import Task, TaskStatus
from core.queue import Message, Queues
from core.retry import Backoff, TaskTimeout, is_retryable
from core.runner import DependencyOutput, Runner
from core.state import TASK_TERMINAL, InvalidTransition
from core.tools import ToolContext, ToolRegistry

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE = "duplicate"  # nothing to run: someone else has the task, or it already finished
    DROPPED = "dropped"  # malformed message, or a task that does not exist
    DISCARDED = "discarded"  # the task was cancelled while it ran: its result is thrown away
    RETRYING = "retrying"  # a transient failure: the task will run again after a backoff delay


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


class LeaseKeeper:
    """Renews a task's lease from a background thread while its runner works.

    The runner is a blocking call (an LLM, a tool), so the heartbeat has to live on its own
    thread. Each beat uses a short-lived connection. A failed beat (database blip) is logged and
    retried at the next tick: the lease only expires if the trouble outlasts the whole lease.
    If a beat finds the lease is no longer ours, `lost` becomes True and beating stops; the
    worker's result is then refused by the fencing check anyway.
    """

    def __init__(
        self,
        task_id: UUID,
        owner: str,
        attempt: int,
        *,
        lease_seconds: float,
        interval: float,
        database_url: str | None,
    ):
        self._args = (task_id, owner, attempt)
        self._lease_seconds = lease_seconds
        self._interval = interval
        self._database_url = database_url
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{task_id}", daemon=True)
        self.lost = False

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with get_conn(self._database_url) as conn:
                    ok = repo.renew_lease(conn, *self._args, self._lease_seconds)
            except Exception:  # logged below; the next tick tries again
                log.exception("task %s: lease renewal failed; will retry", self._args[0])
                continue
            if not ok:
                self.lost = True
                log.warning("task %s: lease lost (reaped or finished elsewhere)", self._args[0])
                return


def _run_with_timeout(call, timeout: float):
    """Run `call()` and give up waiting after `timeout` seconds (raises TaskTimeout).

    Python cannot kill a thread, so a runner that is truly stuck is ABANDONED, not stopped: its
    thread keeps going until the blocking call it is in returns. That is bounded: an LLM request
    has its own timeout (LLM_TIMEOUT_SECONDS) and the code sandbox kills its container after its
    own limit. When the abandoned thread finally returns, its result is refused by the fencing
    check (the attempt is over), so it cannot change anything. docs/RELIABILITY.md lists this.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = call()
        except BaseException as e:  # noqa: BLE001 - handed to the caller below
            box["error"] = e

    thread = threading.Thread(target=target, name="task-runner", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TaskTimeout(f"timed out after {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _fail_or_retry(
    database_url: str | None,
    task: Task,
    attempt: int,
    error: str,
    *,
    retryable: bool,
    backoff: Backoff,
) -> Outcome:
    """A failed attempt: retry later if the failure is transient and attempts remain, else fail.

    "Attempts remain" is decided in ONE place, the database: schedule_retry only matches while
    attempts < max_attempts, and returns None otherwise.
    """
    if retryable:
        delay = backoff.delay(attempt)
        with get_conn(database_url) as conn:
            scheduled = repo.schedule_retry(
                conn, task.id, attempt, error=error, delay_seconds=delay
            )
        if scheduled is not None:
            log.warning(
                "task %s: attempt %d of %d failed (%s); retrying in %.1fs",
                task.key, attempt, task.max_attempts, error, delay,
            )
            return Outcome.RETRYING
        # Nothing matched: no attempts left, or cancelled, or handed to another worker. Fall
        # through to the guarded FAILED write, which discards the result in the last two cases.
        error = f"{error} (gave up after {attempt} of {task.max_attempts} attempts)"
    return _record(database_url, task, attempt, TaskStatus.FAILED, Outcome.FAILED, error=error)


def _record(
    database_url: str | None,
    task: Task,
    attempt: int,
    status: TaskStatus,
    outcome: Outcome,
    **fields,
) -> Outcome:
    """Save a finished task's result. The update is fenced to this claim's attempt, so a result
    that arrives after the task was cancelled OR handed to another worker (lease expired) matches
    nothing: it is discarded, a normal outcome and not an error."""
    try:
        with get_conn(database_url) as conn:
            repo.set_task_status(conn, task.id, status, attempt=attempt, **fields)
    except InvalidTransition:
        log.info("task %s: no longer ours (cancelled or lease lost): result discarded", task.key)
        return Outcome.DISCARDED
    return outcome


def handle_task_message(
    message: Message,
    queues: Queues,
    *,
    runners: dict[str, Runner],
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
    consumer: str = "worker",
    lease_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
    backoff: Backoff | None = None,
    timeout_seconds: float | None = None,
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

    backoff = backoff or Backoff.from_settings()
    lease_seconds = settings.task_lease_seconds if lease_seconds is None else lease_seconds
    heartbeat_seconds = (
        settings.lease_heartbeat_seconds if heartbeat_seconds is None else heartbeat_seconds
    )

    # Claim: READY -> RUNNING is a guarded update, so only one worker can win it, however many
    # times the message was delivered. The same transaction takes the lease.
    try:
        with get_conn(database_url) as conn:
            claimed = repo.claim_task(conn, task_id, consumer, lease_seconds)
    except InvalidTransition:
        return _handle_duplicate(message, queues, task, database_url)
    attempt = claimed.attempts  # the fencing token for everything this claim writes

    by_id = {t.id: t for t in mission_tasks}
    agent = agents[task.agent_id]
    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else agent.timeout_seconds * settings.task_timeout_scale
    )
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
        # The runner gets a view of the tools that knows which task and attempt is calling, so
        # every tool decision lands in the audit trail against the right task. The agent code
        # cannot choose or alter this: it is built here, from the claimed row.
        task_tools = (
            tools.bind(
                ToolContext(
                    task_id=task.id,
                    mission_id=task.mission_id,
                    tenant_id=task.tenant_id,
                    attempt=attempt,
                )
            )
            if tools is not None
            else None
        )
        with LeaseKeeper(
            task_id,
            consumer,
            attempt,
            lease_seconds=lease_seconds,
            interval=heartbeat_seconds,
            database_url=database_url,
        ):
            result = _run_with_timeout(
                lambda: runner(
                    agent, task.instruction, dependencies, tools=task_tools, provider=provider
                ),
                timeout,
            )
    except Exception as e:  # noqa: BLE001 - any runner failure is recorded on the task
        error = f"{type(e).__name__}: {e}"
        log.warning("task %s failed after %.1fs: %s", task.key, time.perf_counter() - started, error)
        outcome = _fail_or_retry(
            database_url, task, attempt, error, retryable=is_retryable(e), backoff=backoff
        )
    else:
        outcome = _record(
            database_url,
            task,
            attempt,
            TaskStatus.COMPLETED,
            Outcome.COMPLETED,
            output=result.output,
        )
        if outcome is Outcome.COMPLETED:
            log.info(
                "task %s completed in %.1fs (%d steps, %d tool calls)",
                task.key,
                time.perf_counter() - started,
                result.steps,
                result.tool_calls,
            )

    # Order matters: the result is committed above, THEN announced, THEN the message is
    # acknowledged. A crash between any two steps is recovered when the message is redelivered.
    # A task going back for a retry has nothing to announce: the scheduler has nothing to decide
    # until it finishes, and the sweeper (not this worker) will publish it when it is due.
    if outcome is not Outcome.RETRYING:
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
    lease_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
    backoff: Backoff | None = None,
    timeout_seconds: float | None = None,
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
                    consumer=consumer,
                    lease_seconds=lease_seconds,
                    heartbeat_seconds=heartbeat_seconds,
                    backoff=backoff,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:  # keeps the worker alive; the message stays pending and is retried
                log.exception("task message %s could not be handled; will retry", message.id)
                stop.wait(retry_delay)

    log.info("worker %s stopped", consumer)