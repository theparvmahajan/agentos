import hashlib
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Response

from core import repository as repo
from core.db import get_conn
from core.models import MissionStatus
from services.api.deps import DatabaseUrl, LauncherDep
from services.api.pagination import InvalidCursor, decode_cursor, encode_cursor
from services.api.schemas import MissionCreate, MissionOut, MissionPage
from services.api.views import to_mission_out, to_summary

router = APIRouter(prefix="/api/v1/missions", tags=["missions"])

# Visible ASCII, no spaces, 1 to 255 characters: easy to log, safe in a header, roomy enough for a UUID.
IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,255}$")


@router.post("", status_code=202, response_model=MissionOut)
def create_mission(
    body: MissionCreate,
    background: BackgroundTasks,
    response: Response,
    database_url: DatabaseUrl,
    launcher: LauncherDep,
    idempotency_key: Annotated[
        str | None,
        Header(
            description=(
                "Makes this request safe to repeat. Send the same key again (for example after a "
                "timeout) and you get the mission the first request created instead of a second "
                "one, with the header Idempotent-Replay: true. The same key with a different "
                "objective is refused with 409."
            )
        ),
    ] = None,
) -> MissionOut:
    if idempotency_key is not None and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key must be 1 to 255 visible ASCII characters without spaces",
        )
    if idempotency_key is None:
        with get_conn(database_url) as conn:
            mission = repo.create_mission(conn, body.objective)
        replay, tasks, agent_names = False, [], {}
    else:
        request_hash = hashlib.sha256(body.objective.encode()).hexdigest()
        try:
            with get_conn(database_url) as conn:
                mission, created = repo.create_mission_idempotent(
                    conn, body.objective, idempotency_key, request_hash
                )
                replay = not created
                tasks = repo.get_tasks(conn, mission.id) if replay else []
                agent_names = {a.id: a.name for a in repo.list_agents(conn)} if replay else {}
        except repo.IdempotencyConflict as e:
            raise HTTPException(
                status_code=409,
                detail="this Idempotency-Key was already used with a different objective",
            ) from e
    if not replay:
        # The block above committed, so the background job is guaranteed to find the row.
        background.add_task(launcher, mission.id)
    else:
        response.headers["Idempotent-Replay"] = "true"  # nothing new was started
    response.headers["Location"] = f"/api/v1/missions/{mission.id}"
    return to_mission_out(mission, tasks, agent_names)


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
