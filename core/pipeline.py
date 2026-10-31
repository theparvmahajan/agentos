import logging
from uuid import UUID

from core import repository as repo
from core.db import get_conn
from core.executor import execute_mission
from core.llm import LLMProvider
from core.models import MissionStatus
from core.planner import run_planner
from core.runner import Runner
from core.tools import ToolRegistry

log = logging.getLogger(__name__)


def _fail_if_still_running(database_url: str | None, mission_id: UUID, message: str) -> None:
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is not None and mission.status == MissionStatus.RUNNING:
            repo.set_mission_status(conn, mission_id, MissionStatus.FAILED, error=message)


def process_mission(
    mission_id: UUID,
    *,
    runners: dict[str, Runner],
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
) -> None:
    """Plan a pending mission, then execute it. Runs as a background job, so it never raises:
    nobody is waiting to catch an exception. Failures are recorded on the mission instead."""
    try:
        run_planner(mission_id, provider=provider, database_url=database_url)
    except Exception:  # the planner already marked the mission failed if it got that far
        log.exception("planning did not complete for mission %s", mission_id)
        return

    try:
        execute_mission(
            mission_id,
            runners=runners,
            tools=tools,
            provider=provider,
            database_url=database_url,
        )
    except Exception as e:  # background job: record the failure, do not crash
        log.exception("execution crashed for mission %s", mission_id)
        _fail_if_still_running(
            database_url, mission_id, f"internal error: {type(e).__name__}: {e}"
        )