# What AgentOS guarantees, and what it does not

This page is the contract for the runtime's failure behaviour. Every row of the failure table
names the test that proves it; a claim without a test is listed under "Not yet".

## The model in four sentences

1. **Postgres is the truth.** Redis streams only carry ids and act as nudges; if a message is
   lost, duplicated or late, the database still says what is true.
2. **Delivery is at-least-once, execution is effectively-once.** A task may be *delivered* more than
   once, but every state change is a guarded `UPDATE`, so only one worker can start it and only the
   current attempt can finish it.
3. **A running task is held under a lease.** The worker renews it every few seconds (the
   heartbeat). If the worker dies the renewals stop, the lease expires, and the reaper gives the task
   to someone else, or fails it when its attempts are used up.
4. **Every attempt has a number, and that number is a fencing token.** A result is only accepted from
   the attempt that is still current. A worker that was merely slow ("zombie") cannot overwrite the
   retry that replaced it.

## Timing (all measured on the database's clock)

| Setting | Default | Meaning |
|---|---|---|
| `TASK_LEASE_SECONDS` | 30 | a worker silent for this long is presumed dead |
| `LEASE_HEARTBEAT_SECONDS` | 10 | how often a live worker renews (must be at most half the lease; checked at startup) |
| `REAPER_INTERVAL_SECONDS` | 10 | how often the scheduler looks for dead workers |

**Worst-case time to notice a dead worker** is the lease plus one reaper interval (about 40 s with the
defaults). A shorter lease recovers faster but risks reaping a healthy worker that was merely paused
(a long garbage collection, a stalled network); the fencing token makes that safe but wasteful.

## Failure table

| What goes wrong | What happens | Test |
|---|---|---|
| Worker process is killed mid-task | lease expires, task goes back to `ready`, another worker finishes it as attempt 2, mission completes | `test_killing_a_worker_process_mid_task_does_not_lose_the_mission` (a real `kill`) |
| Worker is slow but alive | heartbeat keeps the lease; the task is never taken away | `test_a_slow_task_keeps_its_lease_and_is_never_reaped` (and its control without a heartbeat) |
| Worker froze, its task was handed on, then it wakes up | its result is refused and discarded, whether the retry has finished or is still running | `test_a_worker_that_wakes_up_late_...`, `test_a_zombie_that_finishes_while_the_retry_is_still_running_is_refused` |
| Worker keeps dying on one task | after `max_attempts` the task is `failed`, dependents are skipped, the mission fails: no endless loop | `test_a_task_that_keeps_losing_its_worker_ends_up_failed_...` |
| Two reapers run at once | the task is recovered exactly once, one message published | `test_two_reapers_racing_...` (15 rounds) |
| Same message delivered to many workers | exactly one runs it | `test_six_workers_given_the_same_message_...` |
| Result saved but worker died before announcing | the redelivered message re-announces; nothing re-runs | `test_if_announcing_fails_...` |
| Task cancelled while running | its result is discarded | `test_cancel_flow` |
| Task claimed by code that predates leases (upgrade) | one full lease period of grace, then recovered like any other | `test_a_running_task_with_no_lease_is_reaped_only_after_...` |

## Not yet (honest list)

| Gap | Planned |
|---|---|
| A task that is `ready` but was never published (crash between "mark ready" and "publish", or after the reaper's commit and before its publish) waits forever | sweeper for stale `ready` tasks (next M4 issue) |
| No backoff: a requeued task is retried immediately | M4: retries with exponential backoff and jitter |
| `agents.timeout_seconds` is stored but not enforced: a hung (not dead) runner keeps heartbeating forever | M4: timeouts |
| The message a dead worker had received stays pending under its consumer name | M4: `XAUTOCLAIM` (harmless meanwhile: the reaper republishes the task; asserted in the kill test) |
| A poison message is redelivered forever | M4/M8: delivery limit and dead-letter queue |
| Agent progress is lost on a crash: the retry starts the task from scratch (the research and LLM calls are repeated) | next: checkpoint/resume of the agent loop |
| Retrying is only safe for tasks that are safe to repeat. Today's tools are (web search; a sandbox with no network). A tool with real-world effects (email, writes) must not be re-run blindly | M5/M6: idempotency keys and approvals |
| `run_planner` can plan one mission twice if called twice concurrently | M4 |

## What this does not claim

- Not exactly-once *side effects*: only effectively-once *state transitions*. A killed worker's
  half-done work (an LLM call already paid for, a search already made) happens again on the retry.
- Not protection against a database outage: if Postgres is down, workers cannot renew and leases may
  expire; recovery then depends on the reaper after Postgres returns. Redis being down stops
  scheduling but loses no state.
