# 0016: Leases, heartbeats and the reaper

Status: accepted
Issue: #17
Related: ADR 0010 (workers), ADR 0017 (retries, timeouts, idempotency)

## Context

ADR 0010 gave us workers that claim a task with a guarded `UPDATE` (`ready -> running`) and later
save the result. It left one known hole, reproduced by hand with `kill -9`: if a worker dies
between those two steps the task stays `running` forever. Nothing ever moves it. When the worker
restarts, the redelivered message finds the task already `running`, is treated as a duplicate and
is dropped. The mission never finishes.

So the system needs to answer a question it could not answer before: *is the worker that is running
this task still alive?* A process cannot be asked (it may be dead), so liveness has to be inferred
from something the worker keeps doing while it is alive.

## Decision

**1. A lease: "worker X is running attempt N and will renew this until T".**
A worker claims a task and takes the lease in the same transaction (`claim_task`). While the agent
runs, a background thread renews the lease every few seconds (the *heartbeat*). If the worker dies,
the renewals stop and the lease runs out.

**2. The lease lives in its own table (`task_leases`, migration 003), not in columns on `tasks`.**
Every `UPDATE` of `tasks` fires the notify trigger from migration 002, which wakes the API's
listener and the dashboard. A heartbeat every few seconds for every running task would flood that
feed with messages that change nothing. The table has no trigger, and creating a table (unlike
`ALTER TABLE tasks`) does not need a lock that can queue behind a reader.

**3. The database clock decides when a lease has expired.**
`expires_at` is always `now() + interval` evaluated by Postgres, and "expired" is `expires_at <= now()`
evaluated by Postgres. If each worker compared against its own clock, a machine whose clock ran a
minute fast would think a healthy neighbour's lease was dead (and take its task), and one running a
minute slow would keep a dead worker's lease "alive". One clock removes the whole class of problem.

**4. Timing: lease 30 s, heartbeat 10 s, reaper every 10 s; the heartbeat must be at most half the
lease.** A heartbeat only slightly shorter than the lease means one late beat (a slow database
round trip) expires a healthy worker, so the setting is validated at startup and the process refuses
to start otherwise. Worst-case time to notice a dead worker is the lease plus one reaper interval
(about 40 s). Shorter is faster but risks reaping a worker that was merely paused.

**5. The reaper recovers orphaned tasks.** It runs inside the scheduler loop (no new process). For
each running task whose lease for the *current* attempt has expired it does one guarded `UPDATE`:
back to `ready` if attempts remain, `failed` if they are used up. The `UPDATE` re-checks "still
running, still this attempt, still no live lease", so two reapers racing on one task produce exactly
one recovery, and a task that finished or was renewed a moment earlier is left alone.

**6. The attempt number is a fencing token.** `attempts` already increments on every claim. A worker
saves its result with `WHERE attempts = <the attempt I claimed>`. A *zombie* (a worker that was only
frozen: a long pause, a stalled network call) may wake up after its task was given to someone else;
its result carries the old attempt number, matches nothing, and is discarded. Without this, the
zombie could overwrite the retry's work, including while the retry is still running (tested
explicitly: `test_a_zombie_that_finishes_while_the_retry_is_still_running_is_refused`).

**7. Tasks with no lease row get one full lease period of grace.** A task claimed by code from before
this change has no lease. Treating it as dead immediately would reap work that is still alive during
a rolling upgrade.

## Alternatives considered

- **`lease_owner` / `lease_expires_at` columns on `tasks`.** Simpler to read, but it fires the notify
  trigger on every heartbeat and needs `ALTER TABLE` (see ADR 0017 for what that costs).
- **Postgres advisory locks held by the worker's connection.** Elegant: the lock disappears when the
  connection dies. But it requires holding a connection open across slow LLM and tool calls, which
  this system deliberately never does (principle 5 in the handoff).
- **Redis keys with a TTL, or relying on `XAUTOCLAIM` alone.** Redis is not the source of truth; a
  Redis restart would then decide whether tasks are alive. `XAUTOCLAIM` recovers *messages*, not the
  task *state* in Postgres that is stuck at `running`.
- **A separate supervisor process.** More to deploy for the same effect; the scheduler already runs
  continuously and every pass is race-safe, so several can reap at once.

## Consequences

- A killed worker no longer loses a mission: the task is retried (tested with a real killed
  process, `test_killing_a_worker_process_mid_task_does_not_lose_the_mission`).
- A retry repeats the task from the beginning. That is only safe when the task is safe to repeat
  (web search and the no-network sandbox are; an email tool would not be). Idempotency for effects is
  deferred to the milestones that add such tools (ADR 0017).
- The runner thread itself cannot be stopped from outside. A hung but *alive* worker keeps
  heartbeating, so leases alone do not catch it; that is what task timeouts are for (ADR 0017).
- The message a dead worker had received stays pending under its consumer name until `XAUTOCLAIM` is
  added. Harmless: the task is recovered through the database, not through the message. The test
  asserts this on purpose, so fixing it later is a deliberate change.

## How we know it works

`tests/integration/test_leases.py` (claiming, renewing, fencing, the reaper's decisions, 15 rounds of
two reapers racing) and `tests/integration/test_crash_recovery.py` (heartbeat keeps a slow task alive
and its control without a heartbeat, both zombie orderings, a task that keeps losing workers ends
failed, a real `kill` of a worker process). Ten deliberate bugs were introduced one at a time; every one
made a test fail. `python -m scripts.demo_recovery` shows it live.