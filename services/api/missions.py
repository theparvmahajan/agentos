from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus
from services.api.deps import DatabaseUrl, LauncherDep
from services.api.pagination import InvalidCursor, decode_cursor, encode_cursor
from services.api.schemas import MissionCreate, MissionOut, MissionPage
from services.api.views import to_mission_out, to_summary

router = APIRouter(prefix="/api/v1/missions", tags=["missions"])


@router.post("", status_code=202, response_model=MissionOut)
def create_mission(
    body: MissionCreate,
    background: BackgroundTasks,
    response: Response,
    database_url: DatabaseUrl,
    launcher: LauncherDep,
) -> MissionOut:
    with get_conn(database_url) as conn:
        mission = repo.create_mission(conn, body.objective)
    # The block above committed, so the background job is guaranteed to find the row.
    background.add_task(launcher, mission.id)
    response.headers["Location"] = f"/api/v1/missions/{mission.id}"
    return to_mission_out(mission, [], {})


@router.get("", response_model=MissionPage)
def list_missions(
    database_url: DatabaseUrl,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(description="next_cursor from the previous page")] = None,
    status: Annotated[MissionStatus | None, Query(description="only this status")] = None,
) -> MissionPage:
    """Newest first, in pages."""
    before = None
    if cursor is not None:
        try:
            before = decode_cursor(cursor)
        except InvalidCursor as e:
            raise HTTPException(status_code=422, detail="invalid cursor") from e
    with get_conn(database_url) as conn:
        # One extra row tells us whether another page exists, without a second COUNT query.
        rows = repo.list_missions(conn, limit=limit + 1, status=status, before=before)
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return MissionPage(items=[to_summary(m) for m in page], next_cursor=next_cursor)


@router.get("/{mission_id}", response_model=MissionOut)
def get_mission(mission_id: UUID, database_url: DatabaseUrl) -> MissionOut:
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="mission not found")
        tasks = repo.get_tasks(conn, mission_id)
        agent_names = {a.id: a.name for a in repo.list_agents(conn)}
    return to_mission_out(mission, tasks, agent_names)


@router.post("/{mission_id}/cancel", response_model=MissionOut)
def cancel_mission(mission_id: UUID, database_url: DatabaseUrl) -> MissionOut:
    """Stop a mission that has not finished. Tasks already running finish their current work,
    but their results are discarded."""
    with get_conn(database_url) as conn:
        cancelled = repo.cancel_mission(conn, mission_id)
        if cancelled is None:
            existing = repo.get_mission(conn, mission_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="mission not found")
            raise HTTPException(
                status_code=409, detail=f"mission already {existing.status.value}"
            )
        tasks = repo.get_tasks(conn, mission_id)
        agent_names = {a.id: a.name for a in repo.list_agents(conn)}
    return to_mission_out(cancelled, tasks, agent_names)
