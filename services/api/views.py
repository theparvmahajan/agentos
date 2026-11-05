"""Turning database objects into the public response shapes. Used by the REST endpoints and by
the WebSocket feed, so both always describe a mission in exactly the same way."""

from uuid import UUID

from core.dag import topological_order
from core.models import Mission, MissionSummary, Task
from services.api.schemas import MissionOut, MissionPatch, MissionSummaryOut, TaskOut


def in_plan_order(tasks: list[Task]) -> list[Task]:
    """Dependencies first. (The database returns same-transaction rows sorted by key.)"""
    by_id = {t.id: t for t in tasks}
    graph = {str(t.id): {str(d) for d in t.depends_on} for t in tasks}
    return [by_id[UUID(node)] for node in topological_order(graph)]


def to_task_out(task: Task, tasks: list[Task], agent_names: dict[UUID, str]) -> TaskOut:
    key_by_id = {t.id: t.key for t in tasks}
    return TaskOut(
        id=task.id,
        key=task.key,
        agent=agent_names[task.agent_id],
        instruction=task.instruction,
        status=task.status,
        depends_on=[key_by_id[d] for d in task.depends_on],
        attempts=task.attempts,
        output=task.output,
        error=task.last_error,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def to_mission_out(
    mission: Mission, tasks: list[Task], agent_names: dict[UUID, str]
) -> MissionOut:
    return MissionOut(
        id=mission.id,
        objective=mission.objective,
        status=mission.status,
        result=mission.result,
        error=mission.error,
        created_at=mission.created_at,
        updated_at=mission.updated_at,
        completed_at=mission.completed_at,
        tasks=[to_task_out(t, tasks, agent_names) for t in in_plan_order(tasks)],
    )


def to_mission_patch(mission: Mission) -> MissionPatch:
    return MissionPatch(
        id=mission.id,
        status=mission.status,
        result=mission.result,
        error=mission.error,
        updated_at=mission.updated_at,
        completed_at=mission.completed_at,
    )


def to_summary(mission: MissionSummary) -> MissionSummaryOut:
    return MissionSummaryOut(
        id=mission.id,
        objective=mission.objective,
        status=mission.status,
        error=mission.error,
        created_at=mission.created_at,
        updated_at=mission.updated_at,
        completed_at=mission.completed_at,
        tasks_total=mission.tasks_total,
        tasks_completed=mission.tasks_completed,
    )
