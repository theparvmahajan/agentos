# 0002: Schema design for missions and tasks

Status: accepted

## 1. Task statuses
A task's status is a state machine: it's in exactly one state at a time and
can only move along allowed transitions.

- pending: created, but at least one dependency hasn't finished yet.
- ready: every dependency is completed, so the scheduler can queue it.
- running: a worker has picked it up and is executing it.
- completed: finished successfully. Terminal.
- failed: errored and has no retries left. Terminal.
- skipped: an upstream task (dependency) failed, so this one can never run. Terminal.
- cancelled: the user stopped the mission. Terminal.

Missions use a smaller set: pending, planning, running, completed, failed,
cancelled. "planning" covers the time the planner LLM is building the task list.

Statuses are stored as text with a CHECK constraint, not a Postgres ENUM.
I already know I'll add states later (for example waiting_approval for
human-in-the-loop). Changing a CHECK constraint is a simple migration.
Changing an ENUM, especially removing or renaming values, is awkward.

## 2. Dependencies: join table
Dependencies form a graph, which is a many-to-many relationship, so they get
their own table (task_id, depends_on_task_id) with one row per edge.

I rejected a JSON array (depends_on = ["t1", "t2"]) because:
- Integrity: the database can't verify that the referenced tasks exist, and
  deleting a task can leave dangling references. Foreign keys on the join
  table prevent both.
- Query speed: the scheduler's most common question is "task X just finished,
  which tasks were waiting on X?" With an array, that means scanning and
  parsing every task's JSON. With an index on depends_on_task_id, it's a
  fast lookup.

The schema also forbids a task depending on itself (CHECK task_id <>
depends_on_task_id). Longer cycles (A → B → A) can't easily be prevented in
SQL, so they're detected in Python when the plan is validated.

## 3. Retry state
Retry state has to survive a worker crash. If "this is attempt 2 of 3" lived
in Python memory, a crash would lose it, and the task could retry forever or
never retry. Storing it in Postgres makes it durable and visible to any worker.

The task row holds:
- attempts: how many times it has run.
- max_attempts: the retry limit.
- last_error: why the last attempt failed, for debugging and for the user.

The scheduler decides with a single row read: if attempts < max_attempts,
retry; otherwise mark the task failed. Later milestones (M4) will add columns
such as next_retry_at for backoff through a migration.


## Check yourself
Without looking, can you answer these in one or two sentences each?
1. Why is `skipped` different from `failed`?
2. Why is an ENUM worse here than text + CHECK?
3. What query does the join table make fast, and which index makes it fast?
4. Why can't SQL easily stop A → B → A cycles, and where do you catch them instead?
5. What would go wrong if retry counts lived only in memory?
