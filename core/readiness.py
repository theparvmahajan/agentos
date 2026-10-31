from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from core.dag import topological_order
from core.models import TaskStatus

# A pending task can never run once any task it depends on ended up in one of these.
DEAD = frozenset({TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED})
# Tasks that are not done and could still make progress.
ACTIVE = frozenset({TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING})


@dataclass(frozen=True)
class TaskNode:
    """Just what the scheduling decision needs to know about a task."""

    id: UUID
    status: TaskStatus
    depends_on: frozenset[UUID] = frozenset()


@dataclass(frozen=True)
class Decision:
    ready: tuple[UUID, ...]  # pending tasks whose dependencies have all completed
    skip: tuple[UUID, ...]  # pending tasks that can never run: a dependency failed or was skipped
    # True once the tasks in `skip` have been marked skipped: nothing is left that could still
    # make progress. So always apply `skip` BEFORE acting on `finished`.
    finished: bool
    succeeded: bool  # finished, and every task completed


def plan_next(nodes: Iterable[TaskNode]) -> Decision:
    """Decide what should happen next, from the tasks' current statuses alone.

    A pure function: no database, no clock, no memory between calls. Given the same
    statuses it always answers the same way, so a scheduler that crashed and restarted
    can just call it again.

    It only looks at PENDING tasks. A task that is already READY or RUNNING is in flight
    and is never reported again, so acting on a Decision twice does no harm.
    """
    nodes = list(nodes)
    by_key = {str(n.id): n for n in nodes}
    ids = {n.id for n in nodes}
    for n in nodes:
        missing = n.depends_on - ids
        if missing:
            raise ValueError(f"task {n.id} depends on unknown task(s): {sorted(map(str, missing))}")

    # Dependencies first, so every dependency's effective status is known before its dependents.
    order = topological_order({k: {str(d) for d in n.depends_on} for k, n in by_key.items()})

    effective: dict[UUID, TaskStatus] = {}
    ready: list[UUID] = []
    skip: list[UUID] = []

    for key in order:
        node = by_key[key]
        status = node.status
        if status == TaskStatus.PENDING:
            dep_statuses = [effective[d] for d in node.depends_on]
            if any(s in DEAD for s in dep_statuses):
                status = TaskStatus.SKIPPED  # takes effect for the tasks after it, in this same pass
                skip.append(node.id)
            elif all(s == TaskStatus.COMPLETED for s in dep_statuses):
                ready.append(node.id)
        effective[node.id] = status

    finished = not any(s in ACTIVE for s in effective.values())
    succeeded = (
        finished and bool(effective) and all(s == TaskStatus.COMPLETED for s in effective.values())
    )
    return Decision(ready=tuple(ready), skip=tuple(skip), finished=finished, succeeded=succeeded)


class _HasGraphFields(Protocol):
    id: UUID
    status: TaskStatus
    depends_on: list[UUID]


def nodes_from_tasks(
    tasks: Iterable[_HasGraphFields],
    statuses: Mapping[UUID, TaskStatus] | None = None,
) -> list[TaskNode]:
    """Build nodes from stored tasks. `statuses` overrides what the task objects say,
    for callers that track newer statuses in memory."""
    overrides = statuses or {}
    return [
        TaskNode(t.id, overrides.get(t.id, t.status), frozenset(t.depends_on)) for t in tasks
    ]