from datetime import datetime
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