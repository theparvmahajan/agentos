"""Retries with backoff, task timeouts and the sweeper, against a real Postgres and Redis."""

import threading
import time
from itertools import pairwise

import pytest

from core import repository as repo
from core.config import settings
from core.db import get_conn
from core.llm.base import LLMError
from core.models import MissionStatus, TaskStatus
from core.retry import Backoff
from core.runner import AgentResult
from core.scheduler import run_scheduler, schedule_mission
from core.state import InvalidTransition
from core.sweeper import sweep_ready
from core.tools import ToolRegistry
from core.worker import Outcome, handle_task_message, run_worker
from tests.integration.conftest import LEASE_AGENT, dispatch_of, make_due, wait_until

SLOW = Backoff(base=10, cap=10)  # retries wait 5 to 10 s: long enough that nothing runs by accident
FAST = Backoff(base=0.05, cap=0.1)


def task_of(url, task_id):
    with get_conn(url) as conn:
        return repo.get_task(conn, task_id)


def raising(error):
    def run(agent, instruction, dependencies, *, tools, provider=None):
        raise error

    return run


def returning(text, delay=0.0):
    def run(agent, instruction, dependencies, *, tools, provider=None):
        time.sleep(delay)
        return AgentResult(output=text)

    return run


def handle_next(url, queues, mission_id, task_id, runner, **kwargs):
    """Publish the task's message, read it and run it through a worker."""
    queues.publish_task(mission_id, task_id)
    [message] = queues.tasks.read("w")
    kwargs.setdefault("backoff", SLOW)
    return handle_task_message(
        message, queues, runners={LEASE_AGENT: runner}, tools=ToolRegistry(),
        database_url=url, consumer="w", **kwargs,
    )


def seconds_until_due(url, task_id):
    with get_conn(url) as conn:
        return conn.execute(
            "SELECT extract(epoch FROM run_after - now()) AS s FROM task_dispatch WHERE task_id = %s",
            (task_id,),
        ).fetchone()["s"]


# ---------- what is retried ----------

def test_a_transient_failure_is_scheduled_for_a_retry_after_a_backoff_delay(
    test_db_url, queues, lease_mission
):
    mission_id, ids = lease_mission()
    outcome = handle_next(
        test_db_url, queues, mission_id, ids["a"], raising(LLMError("ollama is busy", retryable=True))
    )

    task = task_of(test_db_url, ids["a"])
    assert outcome == Outcome.RETRYING
    assert task.status == TaskStatus.READY and task.attempts == 1
    assert "ollama is busy" in task.last_error
    assert 4.5 <= seconds_until_due(test_db_url, ids["a"]) <= 10  # 5-10 s: the delay is in the database
    assert dispatch_of(test_db_url, ids["a"])["enqueued_at"] is None
    assert queues.events.length() == 0  # nothing to announce: the scheduler has nothing to decide
    assert queues.tasks.pending_count() == 0  # but the message was acknowledged


def test_a_permanent_failure_is_not_retried(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()
    outcome = handle_next(
        test_db_url, queues, mission_id, ids["a"], raising(LLMError("HTTP 401: bad key"))
    )

    task = task_of(test_db_url, ids["a"])
    assert outcome == Outcome.FAILED
    assert task.status == TaskStatus.FAILED and task.attempts == 1
    assert "gave up" not in task.last_error  # it never got to retry
    assert queues.events.length() == 1  # the scheduler is told, so dependents are skipped


def test_a_bug_in_a_runner_is_not_retried(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()
    outcome = handle_next(test_db_url, queues, mission_id, ids["a"], raising(KeyError("oops")))
    assert outcome == Outcome.FAILED and task_of(test_db_url, ids["a"]).attempts == 1


def test_a_task_that_keeps_failing_gives_up_after_max_attempts(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission(max_attempts=2)
    flaky = raising(LLMError("busy", retryable=True))

    assert handle_next(test_db_url, queues, mission_id, ids["a"], flaky) == Outcome.RETRYING
    make_due(test_db_url, ids["a"])
    assert handle_next(test_db_url, queues, mission_id, ids["a"], flaky) == Outcome.FAILED

    task = task_of(test_db_url, ids["a"])
    assert task.status == TaskStatus.FAILED and task.attempts == 2
    assert "gave up after 2 of 2 attempts" in task.last_error
    assert queues.events.length() == 1


def test_a_cancelled_task_is_not_retried(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()

    def cancel_then_fail(agent, instruction, dependencies, *, tools, provider=None):
        with get_conn(test_db_url) as conn:
            repo.set_task_status(conn, ids["a"], TaskStatus.CANCELLED)
        raise LLMError("busy", retryable=True)

    outcome = handle_next(test_db_url, queues, mission_id, ids["a"], cancel_then_fail)
    assert outcome == Outcome.DISCARDED
    assert task_of(test_db_url, ids["a"]).status == TaskStatus.CANCELLED


def test_the_database_refuses_a_retry_that_is_out_of_attempts_or_not_ours(
    test_db_url, lease_mission
):
    """The guard lives in the SQL itself, not only in the worker's decision."""
    _, ids = lease_mission(max_attempts=1)
    with get_conn(test_db_url) as conn:
        repo.claim_task(conn, ids["a"], "w", 30)  # attempt 1 of 1

    with get_conn(test_db_url) as conn:
        assert repo.schedule_retry(conn, ids["a"], 1, error="x", delay_seconds=1) is None  # none left
        assert repo.schedule_retry(conn, ids["a"], 2, error="x", delay_seconds=1) is None  # wrong attempt
    assert task_of(test_db_url, ids["a"]).status == TaskStatus.RUNNING  # untouched


# ---------- the delay is real ----------

def test_a_stray_message_cannot_run_a_task_before_its_delay_is_over(
    test_db_url, queues, lease_mission
):
    mission_id, ids = lease_mission()
    handle_next(
        test_db_url, queues, mission_id, ids["a"], raising(LLMError("busy", retryable=True))
    )

    # A duplicate or old message for the same task arrives while it is still waiting.
    outcome = handle_next(test_db_url, queues, mission_id, ids["a"], returning("too early"))

    task = task_of(test_db_url, ids["a"])
    assert outcome == Outcome.DUPLICATE  # refused and acknowledged
    assert task.status == TaskStatus.READY and task.attempts == 1 and task.output is None

    make_due(test_db_url, ids["a"])  # once due it runs normally
    assert handle_next(test_db_url, queues, mission_id, ids["a"], returning("on time")) == Outcome.COMPLETED
    assert task_of(test_db_url, ids["a"]).output == "on time"


def test_claiming_a_due_task_clears_its_dispatch_row(test_db_url, lease_mission):
    _, ids = lease_mission()
    assert dispatch_of(test_db_url, ids["a"]) is not None  # stamped when it became READY
    with get_conn(test_db_url) as conn:
        repo.claim_task(conn, ids["a"], "w", 30)
    assert dispatch_of(test_db_url, ids["a"]) is None


# ---------- the whole loop: fail, wait, retry, succeed ----------

def test_a_mission_survives_two_transient_failures_end_to_end(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission(ready=False)  # pending: the real scheduler takes over
    with get_conn(test_db_url) as conn:
        agent_id = repo.get_task(conn, ids["a"]).agent_id
        b = repo.create_task(conn, mission_id, "b", agent_id, "do b", depends_on=[ids["a"]])
    calls = []

    def flaky(agent, instruction, dependencies, *, tools, provider=None):
        calls.append(time.monotonic())
        if len(calls) < 3:
            raise LLMError(f"busy (call {len(calls)})", retryable=True)
        return AgentResult(output="finally")

    stops = []
    for target, kwargs in (
        (run_scheduler, {"queues": queues, "consumer": "s", "database_url": test_db_url,
                         "block_ms": 100, "sweep_interval": 0.1, "backoff": FAST}),
        (run_worker, {"queues": queues, "consumer": "w", "runners": {LEASE_AGENT: flaky},
                      "tools": ToolRegistry(), "database_url": test_db_url,
                      "block_ms": 100, "backoff": FAST}),
    ):
        stop = threading.Event()
        thread = threading.Thread(target=target, kwargs={"stop": stop, **kwargs}, daemon=True)
        thread.start()
        stops.append((stop, thread))
    try:
        queues.publish_event(mission_id)
        assert wait_until(
            lambda: _mission_status(test_db_url, mission_id) == MissionStatus.COMPLETED, 20
        )
    finally:
        for stop, _ in stops:
            stop.set()
        for _, thread in stops:
            thread.join(5)

    a = task_of(test_db_url, ids["a"])
    assert a.status == TaskStatus.COMPLETED and a.attempts == 3 and a.output == "finally"
    assert "busy (call 2)" in a.last_error  # the last failure is kept for diagnosis
    assert task_of(test_db_url, b.id).status == TaskStatus.COMPLETED
    gaps = [later - earlier for earlier, later in pairwise(calls)]
    assert all(gap >= 0.02 for gap in gaps)  # each retry really waited (half of 0.05, at least)


def _mission_status(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id).status


# ---------- timeouts ----------

def test_a_task_that_runs_too_long_times_out_and_is_retried(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()
    started = time.monotonic()
    outcome = handle_next(
        test_db_url, queues, mission_id, ids["a"], returning("too slow", delay=5), timeout_seconds=0.3
    )

    assert time.monotonic() - started < 2  # the worker stopped waiting; it did not sit out the 5 s
    task = task_of(test_db_url, ids["a"])
    assert outcome == Outcome.RETRYING and task.status == TaskStatus.READY
    assert "TaskTimeout: timed out after 0.3s" in task.last_error


def test_a_task_that_times_out_every_time_ends_up_failed(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission(max_attempts=1)
    outcome = handle_next(
        test_db_url, queues, mission_id, ids["a"], returning("slow", delay=5), timeout_seconds=0.2
    )
    task = task_of(test_db_url, ids["a"])
    assert outcome == Outcome.FAILED and task.status == TaskStatus.FAILED
    assert "timed out after 0.2s (gave up after 1 of 1 attempts)" in task.last_error


def test_an_abandoned_runner_that_finishes_later_changes_nothing(test_db_url, queues, lease_mission):
    mission_id, ids = lease_mission()
    finished = threading.Event()

    def slow_then_late(agent, instruction, dependencies, *, tools, provider=None):
        time.sleep(0.8)
        finished.set()
        return AgentResult(output="from the abandoned attempt")

    assert handle_next(
        test_db_url, queues, mission_id, ids["a"], slow_then_late, timeout_seconds=0.2
    ) == Outcome.RETRYING
    make_due(test_db_url, ids["a"])
    assert handle_next(
        test_db_url, queues, mission_id, ids["a"], returning("from the retry")
    ) == Outcome.COMPLETED

    assert finished.wait(5)  # the abandoned thread really did finish its work...
    time.sleep(0.2)
    task = task_of(test_db_url, ids["a"])
    assert task.output == "from the retry" and task.attempts == 2  # ...and nothing came of it


def test_the_timeout_comes_from_the_agent_scaled_by_the_setting(
    test_db_url, queues, lease_mission, monkeypatch
):
    mission_id, ids = lease_mission()  # the test agent has the default 60 s timeout
    monkeypatch.setattr(settings, "task_timeout_scale", 0.005)  # 60 s * 0.005 = 0.3 s
    outcome = handle_next(
        test_db_url, queues, mission_id, ids["a"], returning("slow", delay=5)
    )
    assert outcome == Outcome.RETRYING
    assert "timed out after 0.3s" in task_of(test_db_url, ids["a"]).last_error


# ---------- the sweeper ----------

def test_a_ready_task_whose_message_was_lost_is_published(test_db_url, queues, lease_mission):
    _, ids = lease_mission()  # READY, stamped as enqueued, but no message was ever sent
    assert sweep_ready(queues, database_url=test_db_url).published == ()  # a message is presumed to exist

    with get_conn(test_db_url) as conn:  # a minute later, still READY, still waiting
        conn.execute(
            "UPDATE task_dispatch SET enqueued_at = now() - interval '2 minutes' WHERE task_id = %s",
            (ids["a"],),
        )
    assert sweep_ready(queues, database_url=test_db_url).published == (ids["a"],)
    [message] = queues.tasks.read("w")
    assert message.fields["task_id"] == str(ids["a"])


def test_a_ready_task_with_no_dispatch_row_at_all_is_found(test_db_url, queues, lease_mission):
    _, ids = lease_mission()
    with get_conn(test_db_url) as conn:  # as if the process died before writing the row
        conn.execute("DELETE FROM task_dispatch WHERE task_id = %s", (ids["a"],))
    assert sweep_ready(queues, database_url=test_db_url).published == (ids["a"],)


def test_a_published_task_is_not_published_again_every_pass(test_db_url, queues, lease_mission):
    _, ids = lease_mission()
    make_due(test_db_url, ids["a"])
    with get_conn(test_db_url) as conn:
        conn.execute("UPDATE task_dispatch SET enqueued_at = NULL WHERE task_id = %s", (ids["a"],))

    assert sweep_ready(queues, database_url=test_db_url).published == (ids["a"],)
    assert sweep_ready(queues, database_url=test_db_url).published == ()  # stamped: not again
    assert sweep_ready(queues, database_url=test_db_url).published == ()
    assert queues.tasks.length() == 1  # a busy queue does not fill with copies


def test_the_sweeper_ignores_tasks_that_are_waiting_or_not_ready(test_db_url, queues, lease_mission):
    _, ids = lease_mission(keys=("waiting", "running", "done", "pending"))
    with get_conn(test_db_url) as conn:
        repo.claim_task(conn, ids["running"], "w", 30)
        repo.claim_task(conn, ids["done"], "w", 30)
        repo.set_task_status(conn, ids["done"], TaskStatus.COMPLETED, output="x")
        conn.execute("UPDATE tasks SET status = 'pending' WHERE id = %s", (ids["pending"],))
        conn.execute(  # still serving its backoff
            "UPDATE task_dispatch SET run_after = now() + interval '1 hour', enqueued_at = NULL "
            "WHERE task_id = %s",
            (ids["waiting"],),
        )
    assert sweep_ready(queues, database_url=test_db_url).published == ()
    assert queues.tasks.length() == 0


def test_the_scheduler_stamps_what_it_publishes_so_the_sweeper_does_not_double_it(
    test_db_url, queues, lease_mission
):
    mission_id, _ = lease_mission(ready=False)
    schedule_mission(mission_id, queues, database_url=test_db_url)
    assert queues.tasks.length() == 1
    assert sweep_ready(queues, database_url=test_db_url).published == ()
    assert queues.tasks.length() == 1


@pytest.mark.parametrize("round_", range(15))
def test_two_sweepers_racing_publish_each_task_once(test_db_url, queues, lease_mission, round_):
    _, ids = lease_mission(keys=("a", "b", "c", "d"))
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM task_dispatch WHERE task_id = ANY(%s)", (list(ids.values()),))
    barrier = threading.Barrier(2)
    reports = []

    def go():
        barrier.wait()
        reports.append(sweep_ready(queues, database_url=test_db_url))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    published = [task for r in reports for task in r.published]
    assert sorted(published) == sorted(ids.values())  # every task, none twice
    assert queues.tasks.length() == 4


def test_the_scheduler_loop_runs_the_sweeper(test_db_url, queues, lease_mission):
    _, ids = lease_mission()
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM task_dispatch WHERE task_id = %s", (ids["a"],))  # message "lost"
    stop = threading.Event()
    thread = threading.Thread(
        target=run_scheduler,
        kwargs={"queues": queues, "consumer": "s", "stop": stop, "database_url": test_db_url,
                "block_ms": 100, "sweep_interval": 0.1},
        daemon=True,
    )
    thread.start()
    try:
        assert wait_until(lambda: queues.tasks.length() == 1, 10)
    finally:
        stop.set()
        thread.join(5)


def test_a_claim_without_a_ready_task_still_raises(test_db_url, lease_mission):
    _, ids = lease_mission()
    with get_conn(test_db_url) as conn:
        repo.claim_task(conn, ids["a"], "w", 30)
    with get_conn(test_db_url) as conn, pytest.raises(InvalidTransition):
        repo.claim_task(conn, ids["a"], "other", 30)
