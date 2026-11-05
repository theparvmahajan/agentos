from collections import defaultdict
from datetime import datetime
from uuid import UUID

import psycopg

from core.models import (
    Agent,
    Mission,
    MissionStatus,
    MissionSummary,
    Task,
    TaskStatus,
)
from core.state import (
    MISSION_TERMINAL,
    TASK_TERMINAL,
    InvalidTransition,
    allowed_sources,
)

# ---------- agents ----------

def create_agent(
    conn: psycopg.Connection,
    name: str,
    description: str = "",
    capabilities: list[str] | None = None,
    model: str | None = None,
    timeout_seconds: int = 60,
) -> Agent:
    row = conn.execute(
        """
        INSERT INTO agents (name, description, capabilities, model, timeout_seconds)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (name, description, capabilities or [], model, timeout_seconds),
    ).fetchone()
    return Agent(**row)


def get_agent_by_name(conn: psycopg.Connection, name: str) -> Agent | None:
    row = conn.execute("SELECT * FROM agents WHERE name = %s", (name,)).fetchone()
    return Agent(**row) if row else None


def list_agents(conn: psycopg.Connection) -> list[Agent]:
    rows = conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
    return [Agent(**r) for r in rows]

# ---------- missions ----------

def create_mission(
    conn: psycopg.Connection, objective: str, tenant_id: UUID | None = None
) -> Mission:
    row = conn.execute(
        "INSERT INTO missions (objective, tenant_id) VALUES (%s, %s) RETURNING *",
        (objective, tenant_id),
    ).fetchone()
    return Mission(**row)


def get_mission(conn: psycopg.Connection, mission_id: UUID) -> Mission | None:
    row = conn.execute("SELECT * FROM missions WHERE id = %s", (mission_id,)).fetchone()
    return Mission(**row) if row else None


def set_mission_status(
    conn: psycopg.Connection,
    mission_id: UUID,
    status: MissionStatus,
    *,
    result: str | None = None,
    error: str | None = None,
) -> Mission:
    row = conn.execute(
        """
        UPDATE missions
        SET status = %s,
            result = COALESCE(%s, result),
            error = COALESCE(%s, error),
            updated_at = now(),
            completed_at = CASE WHEN %s THEN now() ELSE completed_at END
        WHERE id = %s
        RETURNING *
        """,
        (status.value, result, error, status in MISSION_TERMINAL, mission_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"mission {mission_id} not found")
    return Mission(**row)


# ---------- tasks ----------

def create_task(
    conn: psycopg.Connection,
    mission_id: UUID,
    key: str,
    agent_id: UUID,
    instruction: str,
    depends_on: list[UUID] | None = None,
    max_attempts: int = 3,
) -> Task:
    depends_on = list(set(depends_on or []))

    if depends_on:
        found = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE mission_id = %s AND id = ANY(%s)",
            (mission_id, depends_on),
        ).fetchone()["n"]
        if found != len(depends_on):
            raise ValueError("every dependency must be an existing task in the same mission")

    row = conn.execute(
        """
        INSERT INTO tasks (mission_id, key, agent_id, instruction, max_attempts)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (mission_id, key, agent_id, instruction, max_attempts),
    ).fetchone()

    for dep_id in depends_on:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (%s, %s)",
            (row["id"], dep_id),
        )
    return Task(**row, depends_on=depends_on)


def get_tasks(conn: psycopg.Connection, mission_id: UUID) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE mission_id = %s ORDER BY created_at, key",
        (mission_id,),
    ).fetchall()
    edges = conn.execute(
        """
        SELECT d.task_id, d.depends_on_task_id
        FROM task_dependencies d
        JOIN tasks t ON t.id = d.task_id
        WHERE t.mission_id = %s
        """,
        (mission_id,),
    ).fetchall()

    deps: dict[UUID, list[UUID]] = defaultdict(list)
    for e in edges:
        deps[e["task_id"]].append(e["depends_on_task_id"])

    return [Task(**r, depends_on=deps[r["id"]]) for r in rows]


def set_task_status(
    conn: psycopg.Connection,
    task_id: UUID,
    new_status: TaskStatus,
    *,
    output: str | None = None,
    error: str | None = None,
) -> Task:
    is_running = new_status == TaskStatus.RUNNING
    row = conn.execute(
        """
        UPDATE tasks
        SET status = %s,
            output = COALESCE(%s, output),
            last_error = COALESCE(%s, last_error),
            attempts = attempts + %s,
            started_at = CASE WHEN %s THEN now() ELSE started_at END,
            finished_at = CASE WHEN %s THEN now() ELSE finished_at END,
            updated_at = now()
        WHERE id = %s AND status = ANY(%s)
        RETURNING *
        """,
        (
            new_status.value,
            output,
            error,
            1 if is_running else 0,
            is_running,
            new_status in TASK_TERMINAL,
            task_id,
            [s.value for s in allowed_sources(new_status)],
        ),
    ).fetchone()

    if row is None:
        current = conn.execute(
            "SELECT status FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        if current is None:
            raise LookupError(f"task {task_id} not found")
        raise InvalidTransition(f"task {task_id}: {current['status']} -> {new_status.value}")

    return Task(**row, depends_on=[])


def finish_mission_if_running(
    conn: psycopg.Connection,
    mission_id: UUID,
    status: MissionStatus,
    *,
    result: str | None = None,
    error: str | None = None,
) -> Mission | None:
    """Finish a mission exactly once. Returns None if it was no longer running.

    The status check and the write are one UPDATE, so if two schedulers both decide the
    mission is over, only one of them wins.
    """
    row = conn.execute(
        """
        UPDATE missions
        SET status = %s, result = %s, error = %s, updated_at = now(), completed_at = now()
        WHERE id = %s AND status = 'running'
        RETURNING *
        """,
        (status.value, result, error, mission_id),
    ).fetchone()
    return Mission(**row) if row else None


def get_task(conn: psycopg.Connection, task_id: UUID) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        return None
    deps = conn.execute(
        "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = %s", (task_id,)
    ).fetchall()
    return Task(**row, depends_on=[d["depends_on_task_id"] for d in deps])


# ---------- guarded mission transitions, cancelling, listing ----------

def transition_mission(
    conn: psycopg.Connection,
    mission_id: UUID,
    new_status: MissionStatus,
    *,
    allowed_from: list[MissionStatus],
    result: str | None = None,
    error: str | None = None,
) -> Mission | None:
    """Move a mission to `new_status` only if it is currently in one of `allowed_from`.

    The check and the write are ONE statement, so a late writer (a planner that finished after
    the user cancelled) cannot overwrite a newer state. Returns None if the guard did not match.
    """
    row = conn.execute(
        """
        UPDATE missions
        SET status = %s,
            result = COALESCE(%s, result),
            error = COALESCE(%s, error),
            updated_at = now(),
            completed_at = CASE WHEN %s THEN now() ELSE completed_at END
        WHERE id = %s AND status = ANY(%s)
        RETURNING *
        """,
        (
            new_status.value,
            result,
            error,
            new_status in MISSION_TERMINAL,
            mission_id,
            [s.value for s in allowed_from],
        ),
    ).fetchone()
    return Mission(**row) if row else None


def cancel_mission(conn: psycopg.Connection, mission_id: UUID) -> Mission | None:
    """Cancel a mission and every task that has not finished, in the caller's transaction.

    Returns None if the mission does not exist or had already finished. A task that is RUNNING
    is marked cancelled too: its worker cannot be interrupted, but when it finishes its result
    is discarded (the guarded update no longer matches).
    """
    open_states = [s for s in MissionStatus if s not in MISSION_TERMINAL]
    mission = transition_mission(
        conn, mission_id, MissionStatus.CANCELLED, allowed_from=open_states
    )
    if mission is None:
        return None
    conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled', finished_at = now(), updated_at = now()
        WHERE mission_id = %s AND status = ANY(%s)
        """,
        (mission_id, [s.value for s in allowed_sources(TaskStatus.CANCELLED)]),
    )
    return mission


def list_missions(
    conn: psycopg.Connection,
    *,
    limit: int,
    status: MissionStatus | None = None,
    before: tuple[datetime, UUID] | None = None,
) -> list[MissionSummary]:
    """Newest first. `before` is the (created_at, id) of the last row of the previous page,
    so paging is stable even while new missions are being created."""
    conditions, params = [], []
    if status is not None:
        conditions.append("m.status = %s")
        params.append(status.value)
    if before is not None:
        conditions.append("(m.created_at, m.id) < (%s, %s)")
        params.extend(before)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT m.*,
               (SELECT count(*) FROM tasks t WHERE t.mission_id = m.id) AS tasks_total,
               (SELECT count(*) FROM tasks t
                 WHERE t.mission_id = m.id AND t.status = 'completed') AS tasks_completed
        FROM missions m
        {where}
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT %s
        """,  # only fixed fragments are interpolated above; every value is a parameter
        (*params, limit),
    ).fetchall()
    return [MissionSummary(**r) for r in rows]
