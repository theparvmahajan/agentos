from collections.abc import Mapping
from uuid import UUID

from core.dag import topological_order
from core.models import Task

ERROR_CHARS = 300


def clip(text: str, limit: int = ERROR_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def in_dependency_order(tasks: list[Task]) -> list[Task]:
    """Dependencies first. (The database returns same-transaction rows sorted by key.)"""
    by_id = {t.id: t for t in tasks}
    graph = {str(t.id): {str(d) for d in t.depends_on} for t in tasks}
    return [by_id[UUID(node)] for node in topological_order(graph)]


def compose_result(ordered: list[Task], outputs: Mapping[UUID, str]) -> str:
    """A mission's result is the output of the tasks nothing else depends on."""
    depended_on = {d for t in ordered for d in t.depends_on}
    sinks = [t for t in ordered if t.id not in depended_on]
    if len(sinks) == 1:
        return outputs[sinks[0].id]
    return "\n\n".join(f"## {t.key}\n{outputs[t.id]}" for t in sinks)


def describe_failure(failed: list[Task], skipped: list[Task], errors: Mapping[UUID, str]) -> str:
    skipped_note = f"; skipped: {', '.join(t.key for t in skipped)}" if skipped else ""
    if not failed:
        return "mission ended without completing every task" + skipped_note
    detail = "; ".join(f"{t.key}: {clip(errors.get(t.id, 'unknown error'))}" for t in failed)
    return f"{len(failed)} task(s) failed ({detail})" + skipped_note