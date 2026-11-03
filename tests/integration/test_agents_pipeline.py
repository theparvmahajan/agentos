"""Research -> code -> review, through the real planner, scheduler and workers (threads),
real Postgres and Redis, the real run_python subprocess. Only the LLM and web search are fake."""

import json
import threading
import time

import pytest

from agents import run_coder, run_researcher, run_reviewer
from agents.reviewer import Verdict, parse_verdict
from core import repository as repo
from core.db import get_conn
from core.llm.fake import FakeProvider
from core.models import MissionStatus, TaskStatus
from core.planner import run_planner
from core.scheduler import run_scheduler
from core.tools import build_default_registry
from core.tools.python_exec import run_python_local
from core.tools.search import FakeSearchProvider, SearchResult
from core.worker import run_worker

# Names are unique to this file so they can never collide with the seeded agents.
RESEARCHER, CODER, REVIEWER = "it-researcher", "it-coder", "it-reviewer"
RUNNERS = {RESEARCHER: run_researcher, CODER: run_coder, REVIEWER: run_reviewer}


@pytest.fixture
def mission(test_db_url):
    with get_conn(test_db_url) as conn:
        agent_ids = [
            repo.create_agent(conn, RESEARCHER, capabilities=["web.search"]).id,
            repo.create_agent(conn, CODER, capabilities=["code.execute"]).id,
            repo.create_agent(conn, REVIEWER, capabilities=[]).id,
        ]
        mission_id = repo.create_mission(conn, "how many primes are below 50?").id
    yield mission_id
    with get_conn(test_db_url) as conn:
        conn.execute("DELETE FROM missions WHERE id = %s", (mission_id,))
        for agent_id in agent_ids:
            conn.execute("DELETE FROM agents WHERE id = %s", (agent_id,))


@pytest.fixture
def system(test_db_url, queues):
    running = []

    def _start(provider, search, workers=2):
        tools = build_default_registry(search, code_runner=run_python_local)
        targets = [(run_scheduler, "sched", {})] + [
            (run_worker, f"worker-{i}", {"runners": RUNNERS, "tools": tools, "provider": provider})
            for i in range(workers)
        ]
        for target, name, extra in targets:
            stop = threading.Event()
            thread = threading.Thread(
                target=target,
                args=(queues,),
                kwargs={
                    "consumer": name, "stop": stop, "database_url": test_db_url,
                    "block_ms": 100, "retry_delay": 0.05, **extra,
                },
                daemon=True,
            )
            thread.start()
            running.append((stop, thread))

    yield _start
    for stop, _ in running:
        stop.set()
    for _, thread in running:
        thread.join(timeout=5)
        assert not thread.is_alive()


def wait_until(condition, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def plan() -> str:
    return json.dumps({"tasks": [
        {"key": "t1", "agent": RESEARCHER, "instruction": "find the definition of a prime",
         "depends_on": []},
        {"key": "t2", "agent": CODER, "instruction": "count primes below 50",
         "depends_on": ["t1"]},
        {"key": "t3", "agent": REVIEWER, "instruction": "review the code and its output",
         "depends_on": ["t2"]},
    ]})


def tool(name, **args) -> str:
    return json.dumps({"action": "tool", "tool": name, "args": args})


def final(answer) -> str:
    return json.dumps({"action": "final", "answer": answer})


CODE = (
    "print(sum(1 for n in range(2, 50) if all(n % d for d in range(2, int(n**0.5) + 1))))"
)
SEARCH = FakeSearchProvider([SearchResult("Primes", "https://primes.test", "a prime has 2 divisors")])


def state(test_db_url, mission_id):
    with get_conn(test_db_url) as conn:
        return repo.get_mission(conn, mission_id), {
            t.key: t for t in repo.get_tasks(conn, mission_id)
        }


def run_mission(test_db_url, queues, system, mission, responses):
    llm = FakeProvider(responses)
    run_planner(mission, provider=llm, database_url=test_db_url)
    system(llm, SEARCH)
    queues.publish_event(mission)  # the planner is done: wake the scheduler
    assert wait_until(
        lambda: state(test_db_url, mission)[0].status
        in (MissionStatus.COMPLETED, MissionStatus.FAILED)
    )
    return llm


def test_research_then_code_then_review_runs_end_to_end(test_db_url, queues, mission, system):
    llm = run_mission(test_db_url, queues, system, mission, [
        plan(),
        tool("web_search", query="prime number definition"),
        final("A prime has exactly two divisors (https://primes.test)."),
        tool("run_python", code=CODE),
        final(f"```python\n{CODE}\n```\noutput: 15\nworked"),
        final("VERDICT: APPROVE\n- 15 is the known count of primes below 50"),
    ])
    done, tasks = state(test_db_url, mission)

    assert done.status == MissionStatus.COMPLETED
    assert {t.status for t in tasks.values()} == {TaskStatus.COMPLETED}
    assert parse_verdict(done.result) is Verdict.APPROVE  # the mission result is the review
    # The coder really ran code: the model was shown the real stdout "15".
    assert "exit code 0" in llm.calls[4]["prompt"] and "\n15" in llm.calls[4]["prompt"]
    # Each task was shown the previous task's output.
    assert "exactly two divisors" in llm.calls[3]["prompt"]
    assert "output: 15" in llm.calls[5]["prompt"]


def test_a_rejection_is_reported_but_the_mission_still_completes(
    test_db_url, queues, mission, system
):
    run_mission(test_db_url, queues, system, mission, [
        plan(),
        final("A prime has two divisors."),
        tool("run_python", code="print(14)"),
        final("output: 14, worked"),
        final("VERDICT: REJECT\n- 14 is wrong: there are 15 primes below 50"),
    ])
    done, tasks = state(test_db_url, mission)

    assert done.status == MissionStatus.COMPLETED
    assert tasks["t3"].status == TaskStatus.COMPLETED
    assert parse_verdict(done.result) is Verdict.REJECT


def test_a_coder_that_never_runs_its_code_fails_and_the_review_is_skipped(
    test_db_url, queues, mission, system
):
    run_mission(test_db_url, queues, system, mission, [
        plan(),
        final("A prime has two divisors."),
        *[final("it is 15, trust me")] * 8,  # the coder's whole step budget
    ])
    done, tasks = state(test_db_url, mission)

    assert done.status == MissionStatus.FAILED
    assert tasks["t2"].status == TaskStatus.FAILED
    assert "have not successfully run any code" in tasks["t2"].last_error
    assert tasks["t3"].status == TaskStatus.SKIPPED  # nothing was reviewed that did not exist


def test_an_agent_cannot_use_a_tool_it_lacks_the_capability_for(
    test_db_url, queues, mission, system, tmp_path
):
    # The researcher tries to run code. The registry refuses; the model is told and carries on.
    marker = tmp_path / "ran"
    llm = run_mission(test_db_url, queues, system, mission, [
        plan(),
        tool("run_python", code=f"open({str(marker)!r}, 'w').write('x')"),
        final("I could not run code, so here is the definition."),
        tool("run_python", code="print(15)"),
        final("output: 15, worked"),
        final("VERDICT: APPROVE"),
    ])
    done, tasks = state(test_db_url, mission)

    assert "lacks capability 'code.execute'" in llm.calls[2]["prompt"]
    assert not marker.exists()  # the code never ran
    assert done.status == MissionStatus.COMPLETED
    assert tasks["t1"].output.startswith("I could not run code")
