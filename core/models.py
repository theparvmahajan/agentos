from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class MissionStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class Agent(BaseModel):
    id: UUID
    name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    model: str | None = None
    timeout_seconds: int = 60
    created_at: datetime


class Mission(BaseModel):
    id: UUID
    tenant_id: UUID | None = None
    objective: str
    status: MissionStatus
    result: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class Task(BaseModel):
    id: UUID
    tenant_id: UUID | None = None
    mission_id: UUID
    key: str
    agent_id: UUID
    instruction: str
    status: TaskStatus
    output: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    depends_on: list[UUID] = Field(default_factory=list)