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
| `RETRY_BASE_SECONDS` / `RETRY_MAX_SECONDS` | 2 / 60 | a retry waits `min(max, base * 2^(n-1))` seconds, half of it random |
| `SWEEP_INTERVAL_SECONDS` | 2 | how often due `ready` tasks without a message are published |
| `READY_REPUBLISH_SECONDS` | 60 | how long a published message is trusted before the task is published again |
| `TASK_TIMEOUT_SCALE` | 1.0 | multiplies every agent's `timeout_seconds` (researcher 90, coder 120, reviewer 60) |

**Worst-case time to notice a dead worker** is the lease plus one reaper interval (about 40 s with the
defaults); the retry then waits its backoff delay (1-2 s for a first retry) and up to one sweep (2 s)
before another worker starts it. A shorter lease recovers faster but risks reaping a healthy worker that was merely paused
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
| An LLM call fails with a 5xx, 429, timeout or connection error | the task goes back to `ready` and runs again after a backoff delay (2 s, 4 s, ... up to 60 s, half random); after `max_attempts` it fails with the cause and "gave up after N of M attempts" | `test_a_transient_failure_is_scheduled_for_a_retry_...`, `test_a_task_that_keeps_failing_gives_up_...`, `test_a_mission_survives_two_transient_failures_end_to_end` |
| A failure that cannot be fixed by retrying (bad key, bug, refused permission) | the task fails at once, no retry | `test_a_permanent_failure_is_not_retried`, `test_a_bug_in_a_runner_is_not_retried` |
| A task runs longer than its agent's timeout | the worker stops waiting, counts it as a transient failure (retry, then fail); the stuck thread is abandoned and its late result is ignored | `test_a_task_that_runs_too_long_times_out_...`, `test_an_abandoned_runner_that_finishes_later_changes_nothing` |
| A duplicate or old message arrives while a retry is still waiting | it is refused; the task waits for its delay | `test_a_stray_message_cannot_run_a_task_before_its_delay_is_over` |
| A `ready` task never got a queue message (crash between "mark ready" and "publish") | the sweeper finds it (due, no recent message) and publishes it within `READY_REPUBLISH_SECONDS` | `test_a_ready_task_whose_message_was_lost_is_published`, `test_a_ready_task_with_no_dispatch_row_at_all_is_found` |
| Two sweepers run at once | every task is published once | `test_two_sweepers_racing_publish_each_task_once` (15 rounds) |
| A client repeats `POST /missions` with the same `Idempotency-Key` | the same mission is returned (`Idempotent-Replay: true`), nothing new starts; a different objective with the same key is 409 | `tests/integration/test_idempotency.py` (including 8 simultaneous requests) |
| Task claimed by code that predates leases (upgrade) | one full lease period of grace, then recovered like any other | `test_a_running_task_with_no_lease_is_reaped_only_after_...` |

## Not yet (honest list)

| Gap | Planned |
|---|---|
| A retry repeats the task from scratch: the research and LLM calls are made again | next: checkpoint/resume of the agent loop |
| A `failed` event lost between the reaper's commit and its publish can leave a mission waiting (no periodic mission sweep) | M4 |
| A timed-out runner thread cannot be killed, only abandoned (bounded by the LLM and sandbox timeouts); many timeouts can accumulate threads | M8 (per-task processes or containers) |
| The message a dead worker had received stays pending under its consumer name (harmless: the task is recovered through the database) | M4: `XAUTOCLAIM` |
| A poison message is redelivered forever | M4/M8: delivery limit and dead-letter queue |
| Planner (LLM) failures are not retried: a failed plan fails the mission | later |
| Retrying is only safe for tasks that are safe to repeat. Today's tools are (web search; a sandbox with no network). A tool with real-world effects must not be re-run blindly: it needs an idempotency key derived from mission, task and call number | M5/M6 |
| Idempotency keys never expire | cleanup job, later |
| `run_planner` can plan one mission twice if called twice concurrently | M4 |

## What this does not claim

- Not exactly-once *side effects*: only effectively-once *state transitions*. A killed worker's
  half-done work (an LLM call already paid for, a search already made) happens again on the retry.
- Not protection against a database outage: if Postgres is down, workers cannot renew and leases may
  expire; recovery then depends on the reaper after Postgres returns. Redis being down stops
  scheduling but loses no state.
