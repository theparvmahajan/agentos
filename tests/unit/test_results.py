from datetime import datetime
from uuid import uuid4

from core.models import Task, TaskStatus
from core.results import clip, compose_result, describe_failure, in_dependency_order


def make_task(key, depends_on=()):
    return Task(
        id=uuid4(),
        mission_id=uuid4(),
        key=key,
        agent_id=uuid4(),
        instruction=f"do {key}",
        status=TaskStatus.PENDING,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
        depends_on=[t.id for t in depends_on],
    )


def test_in_dependency_order_ignores_the_order_it_is_given():
    research = make_task("z_research")
    summary = make_task("a_summary", [research])
    ordered = in_dependency_order([summary, research])  # dependent listed first
    assert [t.key for t in ordered] == ["z_research", "a_summary"]


def test_compose_result_uses_the_single_final_task():
    a = make_task("a")
    b = make_task("b", [a])
    assert compose_result([a, b], {a.id: "first", b.id: "second"}) == "second"


def test_compose_result_combines_several_final_tasks():
    x, y = make_task("x"), make_task("y")
    result = compose_result([x, y], {x.id: "X out", y.id: "Y out"})
    assert result == "## x\nX out\n\n## y\nY out"


def test_describe_failure_lists_failed_and_skipped_tasks():
    a, b = make_task("a"), make_task("b")
    message = describe_failure([a], [b], {a.id: "RuntimeError: boom"})
    assert message == "1 task(s) failed (a: RuntimeError: boom); skipped: b"


def test_describe_failure_clips_long_errors_and_handles_missing_ones():
    a = make_task("a")
    assert "..." in describe_failure([a], [], {a.id: "x" * 1000})
    assert "unknown error" in describe_failure([a], [], {})


def test_describe_failure_without_any_failed_task():
    b = make_task("b")
    assert describe_failure([], [b], {}) == "mission ended without completing every task; skipped: b"
    assert describe_failure([], [], {}) == "mission ended without completing every task"


def test_clip_leaves_short_text_alone():
    assert clip("short") == "short"
    assert clip("x" * 400).endswith("...")