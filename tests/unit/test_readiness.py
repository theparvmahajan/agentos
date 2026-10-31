import random
from types import SimpleNamespace
from uuid import UUID

import pytest

from core.dag import CycleError
from core.models import TaskStatus
from core.readiness import TaskNode, nodes_from_tasks, plan_next

P, R, RUN, C, F, S, X = (
    "pending",
    "ready",
    "running",
    "completed",
    "failed",
    "skipped",
    "cancelled",
)


def build(spec):
    """spec: {"name": (status, [dependency names])}. Returns (nodes, name_by_id)."""
    ids = {name: UUID(int=i + 1) for i, name in enumerate(spec)}
    nodes = [
        TaskNode(ids[name], TaskStatus(status), frozenset(ids[d] for d in deps))
        for name, (status, deps) in spec.items()
    ]
    return nodes, {v: k for k, v in ids.items()}


def decide(spec):
    nodes, name_of = build(spec)
    d = plan_next(nodes)
    return [name_of[i] for i in d.ready], [name_of[i] for i in d.skip], d.finished, d.succeeded


# ---------- what is ready ----------

def test_only_tasks_without_dependencies_start_ready():
    ready, skip, finished, _ = decide({"a": (P, []), "b": (P, ["a"]), "c": (P, ["b"])})
    assert (ready, skip, finished) == (["a"], [], False)


def test_independent_tasks_are_all_ready_at_once_in_input_order():
    ready, _, _, _ = decide({"x": (P, []), "y": (P, []), "z": (P, [])})
    assert ready == ["x", "y", "z"]


def test_a_dependent_becomes_ready_when_its_dependency_completes():
    assert decide({"a": (C, []), "b": (P, ["a"])})[0] == ["b"]


def test_a_task_waits_until_all_of_its_dependencies_completed():
    # diamond: a -> b, a -> c, (b, c) -> d
    spec = {"a": (C, []), "b": (C, ["a"]), "c": (RUN, ["a"]), "d": (P, ["b", "c"])}
    assert decide(spec)[0] == []
    spec["c"] = (C, ["a"])
    assert decide(spec)[0] == ["d"]


def test_diamond_fans_out_then_joins():
    spec = {"a": (C, []), "b": (P, ["a"]), "c": (P, ["a"]), "d": (P, ["b", "c"])}
    assert decide(spec)[0] == ["b", "c"]  # both can run in parallel
    spec["b"] = (C, ["a"])
    assert decide(spec)[0] == ["c"]


def test_tasks_already_in_flight_are_not_reported_again():
    assert decide({"a": (R, []), "b": (P, ["a"])})[:3] == ([], [], False)
    assert decide({"a": (RUN, []), "b": (P, ["a"])})[:3] == ([], [], False)


def test_acting_on_a_decision_is_idempotent():
    nodes, _ = build({"a": (P, []), "b": (P, ["a"])})
    first = plan_next(nodes)
    after = [TaskNode(n.id, TaskStatus.READY if n.id in first.ready else n.status, n.depends_on) for n in nodes]
    assert plan_next(after).ready == ()  # the same task is not handed out twice


# ---------- what must be skipped ----------

def test_dependents_of_a_failed_task_are_skipped_transitively_in_one_pass():
    spec = {
        "a": (C, []),
        "b": (F, ["a"]),
        "c": (P, ["b"]),
        "d": (P, ["c"]),
        "e": (P, ["a"]),  # independent of the failure
    }
    ready, skip, finished, _ = decide(spec)
    assert skip == ["c", "d"]
    assert ready == ["e"]
    assert finished is False


def test_a_task_behind_an_already_skipped_task_is_skipped():
    assert decide({"a": (F, []), "b": (S, ["a"]), "c": (P, ["b"])})[1] == ["c"]


def test_a_cancelled_dependency_also_blocks():
    assert decide({"a": (X, []), "b": (P, ["a"])})[1] == ["b"]


def test_one_dead_dependency_skips_immediately_even_if_another_is_still_running():
    spec = {"a": (F, []), "b": (RUN, []), "c": (P, ["a", "b"])}
    ready, skip, _, _ = decide(spec)
    assert skip == ["c"]  # no point waiting for b: c can never run
    assert ready == []


def test_a_running_task_is_never_skipped():
    assert decide({"a": (F, []), "b": (RUN, ["a"])})[1] == []


# ---------- finished and succeeded ----------

def test_all_completed_is_finished_and_succeeded():
    _, _, finished, succeeded = decide({"a": (C, []), "b": (C, ["a"])})
    assert (finished, succeeded) == (True, True)


def test_finished_means_finished_once_the_skips_in_the_same_decision_are_applied():
    # c is still PENDING in the database, but it can never run, so nothing is left to progress.
    spec = {"a": (C, []), "b": (F, ["a"]), "c": (P, ["b"])}
    assert decide(spec) == ([], ["c"], True, False)

    spec["c"] = (S, ["b"])  # once the skip has been applied, the answer is still "finished"
    assert decide(spec) == ([], [], True, False)


def test_a_failure_with_other_work_still_running_is_not_finished():
    spec = {"a": (F, []), "b": (RUN, []), "c": (P, ["a"])}
    assert decide(spec) == ([], ["c"], False, False)


@pytest.mark.parametrize("busy", [P, R, RUN])
def test_unfinished_work_means_not_finished(busy):
    spec = {"a": (C, []), "b": (busy, [])}
    _, _, finished, succeeded = decide(spec)
    assert (finished, succeeded) == (False, False)


def test_empty_graph_is_finished_but_not_a_success():
    d = plan_next([])
    assert (d.ready, d.skip, d.finished, d.succeeded) == ((), (), True, False)


# ---------- bad input ----------

def test_cycles_are_rejected():
    with pytest.raises(CycleError):
        decide({"a": (P, ["b"]), "b": (P, ["a"])})


def test_unknown_dependencies_are_rejected():
    ghost = UUID(int=999)
    node = TaskNode(UUID(int=1), TaskStatus.PENDING, frozenset({ghost}))
    with pytest.raises(ValueError, match="unknown task"):
        plan_next([node])


# ---------- properties ----------

def test_the_answer_does_not_depend_on_the_order_tasks_are_listed():
    spec = {
        "a": (C, []),
        "b": (F, ["a"]),
        "c": (P, ["b"]),
        "d": (P, ["a"]),
        "e": (P, ["a", "d"]),
    }
    nodes, _ = build(spec)
    expected = plan_next(nodes)
    rng = random.Random(1)
    for _ in range(25):
        shuffled = nodes[:]
        rng.shuffle(shuffled)
        got = plan_next(shuffled)
        assert set(got.ready) == set(expected.ready)
        assert set(got.skip) == set(expected.skip)
        assert (got.finished, got.succeeded) == (expected.finished, expected.succeeded)


def test_nodes_from_tasks_lets_in_memory_statuses_override_stored_ones():
    a, b = UUID(int=1), UUID(int=2)
    tasks = [
        SimpleNamespace(id=a, status=TaskStatus.PENDING, depends_on=[]),
        SimpleNamespace(id=b, status=TaskStatus.PENDING, depends_on=[a]),
    ]
    assert plan_next(nodes_from_tasks(tasks)).ready == (a,)
    assert plan_next(nodes_from_tasks(tasks, {a: TaskStatus.COMPLETED})).ready == (b,)


def random_dag(rng, size):
    deps = {}
    for i in range(size):
        earlier = list(range(i))
        deps[i] = rng.sample(earlier, rng.randint(0, min(3, len(earlier)))) if earlier else []
    return deps


@pytest.mark.parametrize("seed", range(300))
def test_random_runs_never_deadlock_and_never_break_the_rules(seed):
    """Simulate a whole mission with random failures and check the rules at every step."""
    rng = random.Random(seed)
    size = rng.randint(1, 12)
    deps = random_dag(rng, size)
    ids = {i: UUID(int=i + 1) for i in range(size)}
    status = {ids[i]: TaskStatus.PENDING for i in range(size)}
    parents = {ids[i]: {ids[d] for d in deps[i]} for i in range(size)}
    dead = {TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED}

    def snapshot():
        return [TaskNode(t, status[t], frozenset(parents[t])) for t in status]

    for _ in range(20 * size + 20):
        d = plan_next(snapshot())

        # the rules
        assert not set(d.ready) & set(d.skip)
        for t in d.ready:
            assert status[t] == TaskStatus.PENDING
            assert all(status[p] == TaskStatus.COMPLETED for p in parents[t])
        for t in d.skip:
            assert status[t] == TaskStatus.PENDING

        for t in d.skip:
            status[t] = TaskStatus.SKIPPED
        if d.finished:
            break

        # something random happens: enqueue a ready task, start an enqueued one, or finish one
        actions = (
            [("enqueue", t) for t in d.ready]
            + [("start", t) for t, s in status.items() if s == TaskStatus.READY]
            + [("finish", t) for t, s in status.items() if s == TaskStatus.RUNNING]
        )
        assert actions, "deadlock: unfinished mission with nothing ready, queued or running"

        kind, t = rng.choice(actions)
        if kind == "enqueue":
            status[t] = TaskStatus.READY
        elif kind == "start":
            status[t] = TaskStatus.RUNNING
        else:
            status[t] = TaskStatus.FAILED if rng.random() < 0.25 else TaskStatus.COMPLETED
    else:
        pytest.fail("simulation did not terminate")

    # final state makes sense
    final = plan_next(snapshot())
    assert final.finished
    for t, s in status.items():
        assert s in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED}
        if s == TaskStatus.COMPLETED:
            assert all(status[p] == TaskStatus.COMPLETED for p in parents[t])
        if s == TaskStatus.SKIPPED:
            assert any(status[p] in dead for p in parents[t])
        if s == TaskStatus.FAILED:
            assert all(status[p] == TaskStatus.COMPLETED for p in parents[t])  # it really ran
    assert final.succeeded == all(s == TaskStatus.COMPLETED for s in status.values())