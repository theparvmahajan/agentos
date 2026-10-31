from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response

from core import repository as repo
from core.dag import topological_order
from core.db import get_conn
from core.models import Mission, Task
from services.api.deps import DatabaseUrl, ProcessorDep
from services.api.schemas import MissionCreate, MissionOut, TaskOut

router = APIRouter(prefix="/missions", tags=["missions"])


def _in_plan_order(tasks: list[Task]) -> list[Task]:
    """Dependencies first. (The database returns same-transaction rows sorted by key.)"""
    by_id = {t.id: t for t in tasks}
    graph = {str(t.id): {str(d) for d in t.depends_on} for t in tasks}
    return [by_id[UUID(node)] for node in topological_order(graph)]


def _to_out(mission: Mission, tasks: list[Task], agent_names: dict[UUID, str]) -> MissionOut:
    key_by_id = {t.id: t.key for t in tasks}
    return MissionOut(
        id=mission.id,
        objective=mission.objective,
        status=mission.status,
        result=mission.result,
        error=mission.error,
        created_at=mission.created_at,
        updated_at=mission.updated_at,
        completed_at=mission.completed_at,
        tasks=[
            TaskOut(
                id=t.id,
                key=t.key,
                agent=agent_names[t.agent_id],
                instruction=t.instruction,
                status=t.status,
                depends_on=[key_by_id[d] for d in t.depends_on],
                attempts=t.attempts,
                output=t.output,
                error=t.last_error,
                started_at=t.started_at,
                finished_at=t.finished_at,
            )
            for t in _in_plan_order(tasks)
        ],
    )


@router.post("", status_code=202, response_model=MissionOut)
def create_mission(
    body: MissionCreate,
    background: BackgroundTasks,
    response: Response,
    database_url: DatabaseUrl,
    processor: ProcessorDep,
) -> MissionOut:
    with get_conn(database_url) as conn:
        mission = repo.create_mission(conn, body.objective)
    # The block above committed, so the background job is guaranteed to find the row.
    background.add_task(processor, mission.id)
    response.headers["Location"] = f"/missions/{mission.id}"
    return _to_out(mission, [], {})


@router.get("/{mission_id}", response_model=MissionOut)
def get_mission(
    mission_id: UUID,
    database_url: DatabaseUrl,
) -> MissionOut:
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="mission not found")
        tasks = repo.get_tasks(conn, mission_id)
        agent_names = {a.id: a.name for a in repo.list_agents(conn)}
    return _to_out(mission, tasks, agent_names)