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
    attempt: int | None = None,
) -> Task:
    """Move a task along the state machine with ONE guarded UPDATE.

    `attempt` is a fencing token: when given, the update only matches if the task is still on
    that attempt. A worker whose lease expired (a "zombie") may still be running attempt 1 after
    the reaper queued attempt 2; its late result carries attempt=1, matches nothing, and is
    refused, so it can never overwrite the retry.
    """
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
        WHERE id = %s AND status = ANY(%s) AND (%s::int IS NULL OR attempts = %s::int)
          -- a READY task still serving its retry delay cannot be claimed early (for example by a
          -- stray duplicate message); the sweeper publishes it again when it is due
          AND (
              NOT %s OR status <> 'ready'
              OR NOT EXISTS (
                  SELECT 1 FROM task_dispatch d WHERE d.task_id = tasks.id AND d.run_after > now()
              )
          )
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
            attempt,
            attempt,
            is_running,
        ),
    ).fetchone()
    if row is not None:
        if is_running:  # claimed: any delay is served and the message is consumed
            conn.execute("DELETE FROM task_dispatch WHERE task_id = %s", (task_id,))
        elif new_status == TaskStatus.READY:  # the caller is about to publish a message
            _stamp_dispatch(conn, task_id, run_after_seconds=None, enqueued=True)

    if row is None:
        current = conn.execute(
            "SELECT status FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        if current is None:
            raise LookupError(f"task {task_id} not found")
        raise InvalidTransition(
            f"task {task_id}: {current['status']} -> {new_status.value}"
            + (f" (fenced to attempt {attempt})" if attempt is not None else "")
        )

    return Task(**row, depends_on=[])


# ---------- leases: who is running a task, and for how long ----------


def claim_task(
    conn: psycopg.Connection, task_id: UUID, owner: str, lease_seconds: float
) -> Task:
    """READY -> RUNNING and take the lease, in one transaction.

    Raises InvalidTransition if someone else got there first. The returned task's `attempts` is
    this claim's fencing token: pass it to set_task_status and renew_lease.
    """
    task = set_task_status(conn, task_id, TaskStatus.RUNNING)
    conn.execute(
        """
        INSERT INTO task_leases (task_id, owner, attempt, expires_at, renewed_at)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s), now())
        ON CONFLICT (task_id) DO UPDATE
        SET owner = EXCLUDED.owner, attempt = EXCLUDED.attempt,
            expires_at = EXCLUDED.expires_at, renewed_at = EXCLUDED.renewed_at
        """,
        (task_id, owner, task.attempts, lease_seconds),
    )
    return task


def renew_lease(
    conn: psycopg.Connection, task_id: UUID, owner: str, attempt: int, lease_seconds: float
) -> bool:
    """Push the lease's expiry forward. False means the lease is no longer ours (it was reaped
    and the task handed on, or finished); the caller should stop beating."""
    row = conn.execute(
        """
        UPDATE task_leases l
        SET expires_at = now() + make_interval(secs => %s), renewed_at = now()
        FROM tasks t
        WHERE l.task_id = %s AND l.owner = %s AND l.attempt = %s
          AND t.id = l.task_id AND t.status = 'running' AND t.attempts = l.attempt
        RETURNING l.task_id
        """,
        (lease_seconds, task_id, owner, attempt),
    ).fetchone()
    return row is not None


def find_orphaned_tasks(
    conn: psycopg.Connection, *, lease_seconds: float, limit: int = 50
) -> list[tuple[UUID, int]]:
    """(task id, attempt) of running tasks whose worker has stopped renewing.

    Orphaned = the lease for the CURRENT attempt has expired, or there is no such lease and the
    task has been running longer than a full lease (a task claimed before leases existed; the
    grace period keeps a rolling upgrade from reaping work that is still alive).
    """
    rows = conn.execute(
        """
        SELECT t.id, t.attempts
        FROM tasks t
        LEFT JOIN task_leases l ON l.task_id = t.id AND l.attempt = t.attempts
        WHERE t.status = 'running'
          AND (
                (l.task_id IS NOT NULL AND l.expires_at <= now())
             OR (l.task_id IS NULL AND t.updated_at <= now() - make_interval(secs => %s))
          )
        ORDER BY t.updated_at
        LIMIT %s
        """,
        (lease_seconds, limit),
    ).fetchall()
    return [(r["id"], r["attempts"]) for r in rows]


def recover_orphaned_task(
    conn: psycopg.Connection,
    task_id: UUID,
    attempt: int,
    *,
    lease_seconds: float,
    reason: str,
    retry_delay: float = 0.0,
) -> tuple[str, Task] | None:
    """Requeue or fail one orphaned task. Returns ("requeued"|"failed", task), or None if the
    task was no longer orphaned (it finished, was renewed, or another reaper got there first).

    Each branch is a single guarded UPDATE that re-checks "still running, still this attempt,
    still no live lease", so two reapers racing on one task produce exactly one recovery.
    """
    still_orphaned = """
        t.id = %(id)s AND t.status = 'running' AND t.attempts = %(attempt)s
        AND NOT EXISTS (
            SELECT 1 FROM task_leases l
            WHERE l.task_id = t.id AND l.attempt = t.attempts AND l.expires_at > now()
        )
        AND (
            EXISTS (SELECT 1 FROM task_leases l WHERE l.task_id = t.id AND l.attempt = t.attempts)
            OR t.updated_at <= now() - make_interval(secs => %(grace)s)
        )
    """
    params = {"id": task_id, "attempt": attempt, "grace": lease_seconds, "reason": reason}
    row = conn.execute(
        f"""
        UPDATE tasks t
        SET status = 'ready', last_error = %(reason)s, updated_at = now()
        WHERE {still_orphaned} AND t.attempts < t.max_attempts
        RETURNING t.*
        """,
        params,
    ).fetchone()
    if row is not None:
        # No message is published here: the sweeper does it once the delay is over.
        _stamp_dispatch(conn, task_id, run_after_seconds=retry_delay, enqueued=False)
        return "requeued", Task(**row, depends_on=[])
    row = conn.execute(
        f"""
        UPDATE tasks t
        SET status = 'failed', last_error = %(reason)s, finished_at = now(), updated_at = now()
        WHERE {still_orphaned} AND t.attempts >= t.max_attempts
        RETURNING t.*
        """,
        params,
    ).fetchone()
    if row is not None:
        return "failed", Task(**row, depends_on=[])
    return None


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


def _stamp_dispatch(
    conn: psycopg.Connection, task_id: UUID, *, run_after_seconds: float | None, enqueued: bool
) -> None:
    """Record when a READY task may run (now + run_after_seconds, or now) and whether a queue
    message for it has just been published."""
    conn.execute(
        """
        INSERT INTO task_dispatch (task_id, run_after, enqueued_at)
        VALUES (
            %(id)s,
            CASE WHEN %(delay)s::float IS NULL THEN NULL
                 ELSE now() + make_interval(secs => %(delay)s::float) END,
            CASE WHEN %(enqueued)s THEN now() ELSE NULL END
        )
        ON CONFLICT (task_id) DO UPDATE
        SET run_after = EXCLUDED.run_after, enqueued_at = EXCLUDED.enqueued_at
        """,
        {"id": task_id, "delay": run_after_seconds, "enqueued": enqueued},
    )


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


# ---------- retries and the sweeper ----------


def schedule_retry(
    conn: psycopg.Connection, task_id: UUID, attempt: int, *, error: str, delay_seconds: float
) -> Task | None:
    """RUNNING -> READY, to run again after `delay_seconds`. Fenced to the attempt that failed.

    Returns None when nothing matched: the task was cancelled, handed to another worker, or has
    no attempts left. The caller tells these apart by reading the task.
    The waiting is stored in task_dispatch.run_after, so it survives a restart of every process.
    No message is published here: the sweeper publishes the task when the delay has passed.
    """
    row = conn.execute(
        """
        UPDATE tasks
        SET status = 'ready', last_error = %s, updated_at = now()
        WHERE id = %s AND status = 'running' AND attempts = %s AND attempts < max_attempts
        RETURNING *
        """,
        (error, task_id, attempt),
    ).fetchone()
    if row is None:
        return None
    _stamp_dispatch(conn, task_id, run_after_seconds=delay_seconds, enqueued=False)
    return Task(**row, depends_on=[])


def claim_due_ready(
    conn: psycopg.Connection, *, republish_after: float, limit: int = 100
) -> list[tuple[UUID, UUID]]:
    """(mission id, task id) of READY tasks that are due and have no recent queue message, and
    stamp them `enqueued_at = now()` so that no other sweeper takes them this round.

    Concurrent sweepers split the work (SKIP LOCKED) and never both publish the same task.
    Stamping BEFORE publishing means a crash between the two costs one `republish_after` of
    waiting (then the task is picked up again), never a lost task. A READY task with no
    task_dispatch row at all (the scheduler crashed before writing one) counts as due with no
    message, so it is found too.
    """
    rows = conn.execute(
        """
        WITH due AS (
            SELECT t.id, t.mission_id
            FROM tasks t
            LEFT JOIN task_dispatch d ON d.task_id = t.id
            WHERE t.status = 'ready'
              AND (d.run_after IS NULL OR d.run_after <= now())
              AND (d.enqueued_at IS NULL OR d.enqueued_at <= now() - make_interval(secs => %s))
            ORDER BY COALESCE(d.run_after, t.created_at)
            LIMIT %s
            FOR UPDATE OF t SKIP LOCKED
        ),
        stamped AS (
            INSERT INTO task_dispatch (task_id, enqueued_at)
            SELECT id, now() FROM due
            ON CONFLICT (task_id) DO UPDATE SET enqueued_at = now()
            RETURNING task_id
        )
        SELECT due.mission_id, due.id FROM due JOIN stamped ON stamped.task_id = due.id
        """,
        (republish_after, limit),
    ).fetchall()
    return [(r["mission_id"], r["id"]) for r in rows]


# ---------- idempotent mission creation ----------

NO_TENANT = UUID(int=0)  # the scope used while there is a single tenant (tenant_id is NULL)


class IdempotencyConflict(Exception):
    """The same Idempotency-Key was used with a different request."""


class _KeyAlreadyUsed(Exception):
    pass


def create_mission_idempotent(
    conn: psycopg.Connection,
    objective: str,
    key: str,
    request_hash: str,
    tenant_id: UUID | None = None,
) -> tuple[Mission, bool]:
    """Create the mission, or return the one this key already created: (mission, created).

    The primary key of idempotency_keys decides, not a check-then-insert, so two identical
    requests arriving at the same instant still create exactly one mission: the loser's INSERT
    waits for the winner to commit, matches nothing, and its own mission (made inside a savepoint)
    is rolled back. Raises IdempotencyConflict if the key exists with a different request.
    """
    scope = tenant_id or NO_TENANT
    try:
        with conn.transaction():  # a savepoint: undone as a whole if the key is taken
            mission = create_mission(conn, objective, tenant_id)
            claimed = conn.execute(
                """
                INSERT INTO idempotency_keys (scope, key, request_hash, mission_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (scope, key) DO NOTHING
                RETURNING mission_id
                """,
                (scope, key, request_hash, mission.id),
            ).fetchone()
            if claimed is None:
                raise _KeyAlreadyUsed
        return mission, True
    except _KeyAlreadyUsed:
        row = conn.execute(
            """
            SELECT m.*, k.request_hash AS stored_hash
            FROM idempotency_keys k JOIN missions m ON m.id = k.mission_id
            WHERE k.scope = %s AND k.key = %s
            """,
            (scope, key),
        ).fetchone()
        stored_hash = row.pop("stored_hash")
        if stored_hash != request_hash:
            raise IdempotencyConflict(key) from None
        return Mission(**row), False

