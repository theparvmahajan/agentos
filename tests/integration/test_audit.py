"""The audit trail against real Postgres, and through the real worker."""

import contextlib
from uuid import uuid4

import pytest

from core import repository as repo
from core.audit import (
    AuditEntry,
    AuditUnavailable,
    Completion,
    PgAuditSink,
    fingerprint,
)
from core.db import get_conn
from core.models import MissionStatus, TaskStatus
from core.tools import ToolDenied, ToolError, build_default_registry
from core.tools.search import FakeSearchProvider, SearchResult
from core.worker import Outcome, handle_task_message

AGENT = "audit-test-agent"


@pytest.fixture
def sink(test_db_url):
    return PgAuditSink(test_db_url)


@pytest.fixture
def cleanup(test_db_url):
    """Audit rows are never deleted by the app, so tests remove what they created."""
    marks = {"missions": [], "tasks": [], "agent": None}
    yield marks
    with get_conn(test_db_url) as conn:
        for t in marks["tasks"]:
            conn.execute("DELETE FROM tool_audit WHERE task_id = %s", (t,))
        for m in marks["missions"]:
            conn.execute("DELETE FROM tool_audit WHERE mission_id = %s", (m,))
            conn.execute("DELETE FROM missions WHERE id = %s", (m,))
        conn.execute("DELETE FROM tool_audit WHERE agent = %s", (AGENT,))
        conn.execute("DELETE FROM agents WHERE name = %s", (AGENT,))


def rows(url, **where):
    clause = " AND ".join(f"{k} = %s" for k in where) or "TRUE"
    with get_conn(url) as conn:
        return conn.execute(
            f"SELECT * FROM tool_audit WHERE {clause} ORDER BY created_at", tuple(where.values())
        ).fetchall()


def entry(**kw) -> AuditEntry:
    digest, keys = fingerprint({"query": "x"})
    base = {
        "agent": AGENT, "tool": "web_search", "capability": "web.search", "decision": "allow",
        "rule": "policy", "reason": "allowed", "risk": "low", "arg_digest": digest,
        "arg_keys": keys,
    }
    return AuditEntry(**{**base, **kw})


def test_a_record_is_stored_and_completed(sink, test_db_url, cleanup):
    task = uuid4()
    cleanup["tasks"].append(task)
    audit_id = sink.record(entry(task_id=task, mission_id=uuid4(), attempt=2))
    [row] = rows(test_db_url, task_id=task)
    assert row["id"] == audit_id and row["decision"] == "allow" and row["attempt"] == 2
    assert row["outcome"] is None and row["arg_keys"] == ["query"]

    sink.complete(audit_id, Completion("ok", 12, untrusted_output=True))
    [row] = rows(test_db_url, task_id=task)
    assert (row["outcome"], row["latency_ms"], row["untrusted_output"]) == ("ok", 12, True)


def test_an_outcome_is_written_once(sink, test_db_url, cleanup):
    task = uuid4()
    cleanup["tasks"].append(task)
    audit_id = sink.record(entry(task_id=task))
    sink.complete(audit_id, Completion("ok", 5))
    sink.complete(audit_id, Completion("error", 999))  # a second write must not rewrite history
    [row] = rows(test_db_url, task_id=task)
    assert (row["outcome"], row["latency_ms"]) == ("ok", 5)


def test_the_database_refuses_nonsense_decisions(sink, cleanup):
    with pytest.raises(AuditUnavailable):
        sink.record(entry(decision="maybe"))
    with pytest.raises(AuditUnavailable):
        sink.record(entry(risk="spicy"))


def test_values_never_reach_any_column(test_db_url, cleanup):
    secret = "sk-live-NEVER-STORE-THIS-0123456789"
    reg = build_default_registry(
        FakeSearchProvider([SearchResult("t", "https://e.test", "s")]), audit=PgAuditSink(test_db_url)
    )
    agent = _agent()
    reg.call(agent, "web_search", {"query": secret})
    with get_conn(test_db_url) as conn:
        everything = conn.execute(
            "SELECT t::text AS row FROM tool_audit t WHERE agent = %s", (AGENT,)
        ).fetchall()
    assert everything, "expected an audit row"
    assert all(secret not in r["row"] for r in everything)
    [row] = rows(test_db_url, agent=AGENT)
    assert row["arg_digest"] == fingerprint({"query": secret})[0]


def test_the_record_outlives_its_mission(sink, test_db_url, cleanup):
    with get_conn(test_db_url) as conn:
        mission = repo.create_mission(conn, "to be deleted")
    cleanup["missions"].append(mission.id)
    sink.record(entry(mission_id=mission.id))
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE id = %s", (mission.id,))
    assert len(rows(test_db_url, mission_id=mission.id)) == 1


def test_an_unreachable_database_fails_closed(cleanup):
    dead = PgAuditSink("postgresql://agentos:agentos@127.0.0.1:1/none")
    with pytest.raises(AuditUnavailable) as e:
        dead.record(entry())
    assert e.value.retryable is True


def test_a_failed_outcome_write_is_logged_not_raised(cleanup, caplog):
    PgAuditSink("postgresql://agentos:agentos@127.0.0.1:1/none").complete(uuid4(), Completion("ok", 1))
    assert "could not record the outcome" in caplog.text


def test_denied_calls_are_recorded_with_their_rule(test_db_url, cleanup):
    reg = build_default_registry(FakeSearchProvider(), audit=PgAuditSink(test_db_url))
    agent = _agent("web.search")
    bound = reg.bind(_ctx(cleanup))
    for _ in range(10):
        bound.call(agent, "web_search", {"query": "q"})
    with pytest.raises(ToolDenied):  # the shipped policy allows 10 searches per task
        bound.call(agent, "web_search", {"query": "q"})
    denied = [r for r in rows(test_db_url, agent=AGENT) if r["decision"] == "deny"]
    assert [r["rule"] for r in denied] == ["web.search:max_calls_per_task"]


# -- through the real worker -----------------------------------------------------------------


def _agent(*caps):
    from datetime import datetime

    from core.models import Agent

    return Agent(id=uuid4(), name=AGENT, capabilities=list(caps or ["web.search"]),
                 created_at=datetime(2026, 1, 1))


def _ctx(cleanup):
    from core.tools import ToolContext

    c = ToolContext(task_id=uuid4(), mission_id=uuid4(), attempt=1)
    cleanup["tasks"].append(c.task_id)
    return c


def test_the_worker_stamps_every_tool_call_with_its_task_and_mission(test_db_url, queues, cleanup):
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT, capabilities=["web.search", "shell.execute"])
        mission = repo.create_mission(conn, "audit via worker")
        task = repo.create_task(conn, mission.id, "t1", agent.id, "search for things")
        repo.set_task_status(conn, task.id, TaskStatus.READY)
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
    cleanup["missions"].append(mission.id)
    cleanup["tasks"].append(task.id)
    queues.publish_task(mission.id, task.id)
    [message] = queues.tasks.read("w")

    tools = build_default_registry(
        FakeSearchProvider([SearchResult("t", "https://e.test", "s")]),
        audit=PgAuditSink(test_db_url),
    )

    def runner(agent, instruction, dependencies, *, tools, provider=None):
        tools.call(agent, "web_search", {"query": "something"})
        with contextlib.suppress(ToolError):  # the refusal is what we are here to see
            tools.call(agent, "shell", {"cmd": "rm -rf /"})  # not a tool that exists
        from core.runner import AgentResult

        return AgentResult(output="done")

    outcome = handle_task_message(
        message, queues, runners={AGENT: runner}, tools=tools, database_url=test_db_url
    )
    assert outcome is Outcome.COMPLETED

    got = rows(test_db_url, task_id=task.id)
    assert [(r["tool"], r["decision"]) for r in got] == [("web_search", "allow"), ("shell", "deny")]
    for r in got:
        assert r["mission_id"] == mission.id and r["attempt"] == 1 and r["agent"] == AGENT
    assert got[0]["outcome"] == "ok" and got[1]["rule"] == "unknown_tool"


def test_an_audit_failure_inside_a_task_makes_the_attempt_retryable(test_db_url, queues, cleanup):
    """The audit database going away must not let a tool run, and must not kill the mission:
    the attempt fails as retryable and the retry machinery takes over."""
    with get_conn(test_db_url) as conn:
        agent = repo.create_agent(conn, AGENT, capabilities=["web.search"])
        mission = repo.create_mission(conn, "audit down")
        task = repo.create_task(conn, mission.id, "t1", agent.id, "search", max_attempts=3)
        repo.set_task_status(conn, task.id, TaskStatus.READY)
        repo.set_mission_status(conn, mission.id, MissionStatus.RUNNING)
    cleanup["missions"].append(mission.id)
    queues.publish_task(mission.id, task.id)
    [message] = queues.tasks.read("w")

    search = FakeSearchProvider([SearchResult("t", "https://e.test", "s")])
    tools = build_default_registry(
        search, audit=PgAuditSink("postgresql://agentos:agentos@127.0.0.1:1/none")
    )

    def runner(agent, instruction, dependencies, *, tools, provider=None):
        tools.call(agent, "web_search", {"query": "x"})
        raise AssertionError("the tool must not have run")

    outcome = handle_task_message(
        message, queues, runners={AGENT: runner}, tools=tools, database_url=test_db_url
    )
    assert outcome is Outcome.RETRYING and search.calls == []
