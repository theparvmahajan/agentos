from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.models import MissionStatus, TaskStatus


class MissionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    objective: str = Field(min_length=1, max_length=2000)


class TaskOut(BaseModel):
    id: UUID
    key: str
    agent: str
    instruction: str
    status: TaskStatus
    depends_on: list[str]
    attempts: int
    output: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class MissionOut(BaseModel):
    id: UUID
    objective: str
    status: MissionStatus
    result: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    tasks: list[TaskOut] = []

class MissionSummaryOut(BaseModel):
    """One row of the mission list: no tasks, just counts."""

    id: UUID
    objective: str
    status: MissionStatus
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    tasks_total: int
    tasks_completed: int


class MissionPage(BaseModel):
    items: list[MissionSummaryOut]
    next_cursor: str | None = None  # pass it back as ?cursor= to get the next page


# ---------- the WebSocket feed: WS /api/v1/missions/{id}/stream ----------
# Every message is a complete, current STATE (not a delta), so applying one twice or out of
# order is harmless: the client just replaces what it holds for that mission or task.

class MissionPatch(BaseModel):
    """The mission's own fields. Its tasks arrive as separate task messages."""

    id: UUID
    status: MissionStatus
    result: str | None = None
    error: str | None = None
    updated_at: datetime
    completed_at: datetime | None = None


class SnapshotMessage(BaseModel):
    """Always the first message, and sent again after a resync: replace everything you hold."""

    type: Literal["snapshot"] = "snapshot"
    mission: MissionOut


class TaskMessage(BaseModel):
    type: Literal["task"] = "task"
    mission_id: UUID
    task: TaskOut  # a task not seen before is a new node


class MissionMessage(BaseModel):
    """After a terminal status (completed, failed, cancelled) the server closes the stream."""

    type: Literal["mission"] = "mission"
    mission: MissionPatch
