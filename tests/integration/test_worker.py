import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from agents import run_researcher
from core import repository as repo
from core import worker
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.models import MissionStatus, TaskStatus
from core.planner import run_planner
from core.queue import Queues
from core.runner import AgentResult
from core.scheduler import run_scheduler, schedule_mission
from core.tools import ToolRegistry, build_default_registry
from core.tools.search import FakeSearchProvider, SearchResult
from core.worker import Outcome, handle_task_message, run_worker

AGENT_NAME = "worker-test-agent"


class Recorder:
    """A fake runner. Records every call; can fail on chosen instructions; can take a while."""

    def __init__(self, fail_on=(), delay=0.0):
        self.fail_on = set(fail_on)
        self.delay = delay
        self.calls = []  # (instruction, dependencies)
        self.spans = {}  # instruction -> (start, end)

    def __call__(self, agent, instruction, dependencies, *, tools, provider=None):
        start = time.time()
        self.calls.append((instruction, list(dependencies)))
        time.sleep(self.delay)
        if instruction in self.fail_on:
            raise RuntimeError("boom")
        self.spans[instruction] = (start, time.time())
        return AgentResult(output=f"result of {instruction}")


@pytest.fixture
def make_mission(test_db_url):
    created = {"missions": [], "agent": None}

    def _make(spec, status=MissionStatus.RUNNING):
        # spec: [(key, [dependency keys])], dependencies listed first
        with get_conn(test_db_url) as conn:
            if created["agent"] is None:
                created["agent"] = repo.create_agent(
                    conn, AGENT_NAME, capabilities=["web.search"]
                ).id
            mission = repo.create_mission(conn, "worker test")
            ids = {}
            for key, deps in spec:
                task = repo.create_task(
                    conn, mission.id, key, created["agent"], f"do {key}",
                    depends_on=[ids[d] for d in deps],
                )
                ids[key] = task.id
            repo.set_mission_status(conn, mission.id, status)
        created["missions"].append(mission.id)
        return mission.id

    yield _make

    with get_conn(test_db_url) as conn:
        for mission_id in created["missions"]:
            conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))
        if created["agent"] is not None:
            conn.execute("DELETE FROM agents WHERE id = %s", (created["agent"],))


def tasks_by_key(url, mission_id):
    with get_conn(url) as conn:
        return {t.key: t for t in repo.get_tasks(conn, mission_id)}


def mission_of(url, mission_id):
    with get_conn(url) as conn:
        return repo.get_mission(conn, mission_id)


def next_message(queues, consumer="w"):
    [message] = queues.tasks.read(consumer)
    return message


def handle(test_db_url, queues, message, runner, **kwargs):
    return handle_task_message(
        message, queues, runners={AGENT_NAME: runner}, tools=ToolRegistry(),
        database_url=test_db_url, **kwargs,
    )


def events(queues):
    return [f for _, f in queues.events.client.xrange(queues.events.name)]


def wait_until(condition, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


# ---------- handling one message ----------

def test_a_task_is_run_recorded_announced_and_acknowledged(test_db_url, queues, make_mission):
    m = make_mission([("a", []), ("b", ["a"])])
    runner = Recorder()
    schedule_mission(m, queues, database_url=test_db_url)  # enqueues a

    outcome = handle(test_db_url, queues, next_message(queues), runner)

    assert outcome == Outcome.COMPLETED
    a = tasks_by_key(test_db_url, m)["a"]
    assert a.status == TaskStatus.COMPLETED and a.output == "result of do a" and a.attempts == 1
    assert events(queues) == [{"mission_id": str(m), "task_id": str(a.id)}]  # announced
    assert queues.tasks.pending_count() == 0 and queues.tasks.length() == 0  # acknowledged


def test_the_agent_receives_the_outputs_of_its_dependencies(test_db_url, queues, make_mission):
    m = make_mission([("a", []), ("b", ["a"])])
    runner = Recorder()
    schedule_mission(m, queues, database_url=test_db_url)
    handle(test_db_url, queues, next_message(queues), runner)
    schedule_mission(m, queues, database_url=test_db_url)  # a is done, so b is enqueued

    handle(test_db_url, queues, next_message(queues), runner)

    instruction, dependencies = runner.calls[1]
    assert instruction == "do b"
    assert [(d.key, d.instruction, d.output) for d in dependencies] == [
        ("a", "do a", "result of do a")
    ]


def test_a_failing_runner_marks_the_task_failed_and_still_announces_it(test_db_url, queues, make_mission):
    m = make_mission([("a", [])])
    schedule_mission(m, queues, database_url=test_db_url)

    outcome = handle(test_db_url, queues, next_message(queues), Recorder(fail_on={"do a"}))

    assert outcome == Outcome.FAILED
    a = tasks_by_key(test_db_url, m)["a"]
    assert a.status == TaskStatus.FAILED and a.last_error == "RuntimeError: boom"
    assert len(events(queues)) == 1  # the scheduler must hear about failures too
    assert queues.tasks.pending_count() == 0


def test_an_agent_without_a_runner_fails_the_task_clearly(test_db_url, queues, make_mission):
    m = make_mission([("a", [])])
    schedule_mission(m, queues, database_url=test_db_url)

    outcome = handle_task_message(
        next_message(queues), queues, runners={}, tools=ToolRegistry(), database_url=test_db_url
    )

    assert outcome == Outcome.FAILED
    assert f"no runner registered for agent '{AGENT_NAME}'" in tasks_by_key(test_db_url, m)["a"].last_error


# ---------- duplicates and crashes ----------

def test_a_duplicate_message_for_a_finished_task_runs_nothing_but_reannounces(test_db_url, queues, make_mission):
    m = make_mission([("a", [])])
    runner = Recorder()
    schedule_mission(m, queues, database_url=test_db_url)
    message = next_message(queues)
    handle(test_db_url, queues, message, runner)

    queues.tasks.publish(message.fields)  # the same task delivered again
    outcome = handle(test_db_url, queues, next_message(queues), runner)

    assert outcome == Outcome.DUPLICATE
    assert len(runner.calls) == 1  # never run twice
    assert len(events(queues)) == 2  # but announced again, in case the first announcement was lost
    assert queues.tasks.pending_count() == 0


def test_a_duplicate_for_a_task_someone_else_is_running_is_ignored(test_db_url, queues, make_mission):
    m = make_mission([("a", [])])
    runner = Recorder()
    schedule_mission(m, queues, database_url=test_db_url)
    task = tasks_by_key(test_db_url, m)["a"]
    with get_conn(test_db_url) as conn:  # another worker has already claimed it
        repo.set_task_status(conn, task.id, TaskStatus.RUNNING)

    outcome = handle(test_db_url, queues, next_message(queues), runner)

    assert outcome == Outcome.DUPLICATE
    assert runner.calls == []
    assert events(queues) == []  # not finished, so nothing to announce
    assert queues.tasks.pending_count() == 0


def test_six_workers_given_the_same_message_run_the_task_exactly_once(test_db_url, queues, make_mission):
    m = make_mission([("a", [])])
    runner = Recorder(delay=0.2)
    schedule_mission(m, queues, database_url=test_db_url)
    message = next_message(queues)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda _: handle(test_db_url, queues, message, runner), range(6)))

    assert len(runner.calls) == 1
    assert outcomes.count(Outcome.COMPLETED) == 1
    assert outcomes.count(Outcome.DUPLICATE) == 5
    assert tasks_by_key(test_db_url, m)["a"].attempts == 1


def test_if_announcing_fails_the_message_stays_pending_and_the_replay_announces(
    test_db_url, queues, make_mission, monkeypatch
):
    m = make_mission([("a", [])])
    runner = Recorder()
    schedule_mission(m, queues, database_url=test_db_url)
    message = next_message(queues)

    def redis_is_down(self, mission_id, task_id=None):
        raise redis.ConnectionError("redis went away")

    with monkeypatch.context() as patch:
        patch.setattr(Queues, "publish_event", redis_is_down)
        with pytest.raises(redis.ConnectionError):
            handle(test_db_url, queues, message, runner)

    # The result was saved, but nothing was announced and the message was not acknowledged.
    assert tasks_by_key(test_db_url, m)["a"].status == TaskStatus.COMPLETED
    assert events(queues) == []
    assert queues.tasks.pending_count() == 1

    [again] = queues.tasks.read("w", pending=True)  # what the worker loop does next
    outcome = handle(test_db_url, queues, again, runner)

    assert outcome == Outcome.DUPLICATE
    assert len(runner.calls) == 1  # still only run once
    assert len(events(queues)) == 1  # and now the scheduler is told
    assert queues.tasks.pending_count() == 0


@pytest.mark.parametrize("fields", [{}, {"mission_id": "x"}, {"task_id": "not-a-uuid"}])
def test_malformed_messages_are_dropped(test_db_url, queues, fields):
    queues.tasks.publish(fields or {"junk": "1"})
    outcome = handle(test_db_url, queues, next_message(queues), Recorder())
    assert outcome == Outcome.DROPPED
    assert queues.tasks.pending_count() == 0


def test_a_message_for_an_unknown_task_is_dropped(test_db_url, queues):
    queues.tasks.publish({"mission_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4())})
    assert handle(test_db_url, queues, next_message(queues), Recorder()) == Outcome.DROPPED
    assert queues.tasks.pending_count() == 0


# ---------- the worker loop ----------

def start(target, *args, **kwargs):
    stop = threading.Event()
    thread = threading.Thread(target=target, args=args, kwargs={"stop": stop, **kwargs}, daemon=True)
    thread.start()
    return stop, thread


@pytest.fixture
def system(test_db_url, queues):
    """A scheduler and some workers running in threads. Call it with a runner and a worker count."""
    running = []

    def _start(runners, workers=2, tools=None, provider=None):
        tools = tools or ToolRegistry()
        running.append(start(
            run_scheduler, queues, consumer="sched", database_url=test_db_url,
            block_ms=100, retry_delay=0.05,
        ))
        for i in range(workers):
            running.append(start(
                run_worker, queues, consumer=f"worker-{i}", runners=runners, tools=tools,
                provider=provider, database_url=test_db_url, block_ms=100, retry_delay=0.05,
            ))

    yield _start
    for stop, _ in running:
        stop.set()
    for _, thread in running:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_workers_and_scheduler_run_a_diamond_with_the_middle_tasks_in_parallel(
    test_db_url, queues, make_mission, system
):
    m = make_mission([("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])])
    runner = Recorder(delay=0.4)
    system({AGENT_NAME: runner}, workers=2)

    queues.publish_event(m)
    assert wait_until(lambda: mission_of(test_db_url, m).status == MissionStatus.COMPLETED)

    assert mission_of(test_db_url, m).result == "result of do d"
    (b_start, b_end), (c_start, c_end) = runner.spans["do b"], runner.spans["do c"]
    assert max(b_start, c_start) < min(b_end, c_end)  # b and c really overlapped
    assert len(runner.calls) == 4  # and nothing ran twice
    assert wait_until(lambda: queues.tasks.length() == 0 and queues.events.length() == 0)


def test_a_failure_in_the_loop_skips_dependents_and_fails_the_mission(
    test_db_url, queues, make_mission, system
):
    m = make_mission([("a", []), ("b", ["a"]), ("c", ["b"])])
    system({AGENT_NAME: Recorder(fail_on={"do b"})}, workers=1)

    queues.publish_event(m)
    assert wait_until(lambda: mission_of(test_db_url, m).status == MissionStatus.FAILED)

    tasks = tasks_by_key(test_db_url, m)
    assert tasks["a"].status == TaskStatus.COMPLETED
    assert tasks["b"].status == TaskStatus.FAILED
    assert tasks["c"].status == TaskStatus.SKIPPED
    assert "b: RuntimeError: boom" in mission_of(test_db_url, m).error


def test_a_message_that_fails_to_process_is_retried_by_the_loop(
    test_db_url, queues, make_mission, system, monkeypatch
):
    m = make_mission([("a", [])])
    real = worker.handle_task_message
    attempts = []

    def flaky(message, q, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("database hiccup")
        return real(message, q, **kwargs)

    monkeypatch.setattr(worker, "handle_task_message", flaky)
    system({AGENT_NAME: Recorder()}, workers=1)

    queues.publish_event(m)
    assert wait_until(lambda: mission_of(test_db_url, m).status == MissionStatus.COMPLETED)
    assert len(attempts) == 3  # failed twice, then the pending message was replayed


def test_a_restarted_worker_picks_up_what_the_previous_one_never_finished(
    test_db_url, queues, make_mission, system
):
    m = make_mission([("a", [])])
    schedule_mission(m, queues, database_url=test_db_url)
    queues.tasks.read("worker-0")  # worker-0 received the message, then died before doing anything
    assert queues.tasks.pending_count() == 1

    system({AGENT_NAME: Recorder()}, workers=1)  # a new worker-0 starts

    assert wait_until(lambda: mission_of(test_db_url, m).status == MissionStatus.COMPLETED)


def test_the_whole_pipeline_with_the_real_researcher_agent(test_db_url, queues, make_mission, system):
    m = make_mission([("seed", [])], status=MissionStatus.PENDING)
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM tasks WHERE mission_id = %s", (m,))

    plan = json.dumps({"tasks": [
        {"key": "t1", "agent": AGENT_NAME, "instruction": "find facts", "depends_on": []},
        {"key": "t2", "agent": AGENT_NAME, "instruction": "summarize", "depends_on": ["t1"]},
    ]})
    llm = FakeProvider([
        plan,
        json.dumps({"action": "tool", "tool": "web_search", "args": {"query": "facts"}}),
        json.dumps({"action": "final", "answer": "found: fact one"}),
        json.dumps({"action": "final", "answer": "summary: fact one matters"}),
    ])
    search = FakeSearchProvider([SearchResult("Doc", "https://doc.test", "fact one")])

    run_planner(m, provider=llm, database_url=test_db_url)
    system({AGENT_NAME: run_researcher}, workers=2, tools=build_default_registry(search), provider=llm)
    queues.publish_event(m)  # the planner is done: wake the scheduler

    assert wait_until(lambda: mission_of(test_db_url, m).status == MissionStatus.COMPLETED)
    assert mission_of(test_db_url, m).result == "summary: fact one matters"
    assert search.calls == [("facts", 5)]
    assert "found: fact one" in llm.calls[3]["prompt"]  # t2 was shown t1's output