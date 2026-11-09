# 0017: Retries with backoff, task timeouts and idempotency keys

Status: accepted
Issue: #18
Related: ADR 0016 (leases and the reaper), docs/RELIABILITY.md

## Context

After ADR 0016 a dead worker was recovered, but four things were still wrong:

1. **One transient failure killed a task.** A single Ollama HTTP 500 (observed for real) failed a
   mission that a retry would have saved. `tasks.max_attempts` existed but was only used by the reaper.
2. **Recovery was immediate.** A task that crashed its worker every time would be retried in a tight
   loop. Nothing spaced retries out.
3. **`agents.timeout_seconds` was stored but never enforced.** A hung (not dead) task kept its lease
   alive forever.
4. **`POST /missions` was not safe to repeat.** A client that timed out and retried created two
   missions, each costing LLM calls.

There was also an open gap from ADR 0016: a task that is `ready` but whose queue message was lost
(a crash between "mark ready" and "publish") waited forever.

## Decision

**1. Retry only failures that say they are transient.** An exception carries `retryable = True`
(`core/retry.py`). An exception that says nothing is *not* retried. `LLMError` sets it from the
failure: HTTP 429 and 5xx, timeouts and connection errors are retryable; other 4xx (bad key, bad
request) and unreadable replies are not. A task timeout is retryable. A bug in a runner, a refused
permission or a malformed plan fails at once: running it again cannot change the answer and costs
time and money. A retryable failure with attempts left goes back to `ready`; otherwise the task fails
with the cause plus "gave up after N of M attempts".

**2. Exponential backoff with "equal jitter".** The delay before attempt n+1 is
`min(cap, base * 2^(n-1))`, half fixed and half random (defaults: base 2 s, cap 60 s). The doubling
gives a struggling server room to recover; the random half stops many tasks that failed together from
retrying at the same instant. We use equal jitter rather than full jitter because the fixed half
guarantees a retry really is delayed (full jitter can return nearly zero).

**3. The wait is stored in the database, and a sweeper publishes due tasks.** A retried task is `ready`
with `run_after = now() + delay` (table `task_dispatch`). Nothing sleeps in a process, so a restart
of every service forgets nothing. A sweeper, running inside the scheduler loop every 2 s, publishes
`ready` tasks that are due and have no recent message: `run_after <= now()` and `enqueued_at` older
than `READY_REPUBLISH_SECONDS` (60 s) or never set. Three details matter:
- It stamps `enqueued_at` *before* publishing, in the same statement that selects the tasks
  (`FOR UPDATE SKIP LOCKED`), so concurrent sweepers split the work and never publish a task twice,
  and a crash between stamp and publish costs one minute of waiting, never a lost task.
- A claim refuses a `ready` task whose `run_after` is still in the future, so a stray duplicate
  message cannot skip the backoff.
- The reaper now only *records* a retry (state plus delay); it no longer publishes. That gives the
  system a single rule for "put a ready task on the queue", and it also repairs the old gap above:
  any `ready` task without a message is found by the same query.

**4. The bookkeeping lives in new tables, not new columns.** We first added `run_after` and
`enqueued_at` as columns on `tasks`. The existing test
`test_a_session_that_only_reads_does_not_block_the_migration` failed: `ALTER TABLE` needs an exclusive
lock that waits behind any session holding the table open, even an idle transaction left by a
database tool. That is exactly what `scripts/migrate.py` is designed not to do. Separately, every
update of `tasks` fires the notify trigger, and dispatch bookkeeping changes far more often than
anything the dashboard cares about. So the data moved to `task_dispatch` (and idempotency keys to
`idempotency_keys`): `CREATE TABLE` does not queue behind readers and the tables have no trigger. A
`ready` task with no `task_dispatch` row counts as "due, no message", so a crash before the row was
written is still repaired.

**5. Timeouts: enforce `agent.timeout_seconds` (times `TASK_TIMEOUT_SCALE`) per attempt.** The worker
runs the agent on a thread and stops waiting after the timeout, then treats it as a retryable failure.
*Python cannot kill a thread*, so a truly stuck runner is **abandoned, not stopped**: it runs on until
the blocking call it is in returns. That leak is bounded, because an LLM request has its own timeout
(`LLM_TIMEOUT_SECONDS`) and the code sandbox kills its container after its own limit. If the abandoned
thread later finishes, nothing happens: its result is never saved. The scale setting exists because
the seeded timeouts (researcher 90 s, coder 120 s, reviewer 60 s) were never enforced before and a slow
local model may legitimately need longer.

**6. Idempotency keys on `POST /missions`.** A client sends `Idempotency-Key: <1-255 visible ASCII>`.
The first request creates the mission and stores `(scope, key, hash of the request, mission id)`; a
repeat returns the same mission with `Idempotent-Replay: true` and starts nothing new. The same key
with a different objective is refused with 409, because that is a client bug and silently returning
the wrong mission would hide it. The primary key of `idempotency_keys` decides races (not a
check-then-insert): the loser's INSERT waits for the winner to commit, matches nothing, and its own
mission, created inside a savepoint, is rolled back. Eight simultaneous identical requests create
exactly one mission (tested 10 rounds).

Idempotency has three layers in this system, and only the last is *not* built yet:
(a) the API (this ADR); (b) state transitions, which are guarded updates and already idempotent
(ADR 0010); (c) side effects of tools. For (c) the plan is a key derived from
`mission : task : call number`, passed to tools that change the outside world. No such tool exists,
so building the plumbing now would be speculation; it arrives with the first one (M5/M6), and until
then retries are only safe because today's tools are safe to repeat.

## Alternatives considered

- **Retry everything.** Wastes money on permanent errors and hides bugs behind three identical failures.
- **Sleep in the worker between attempts.** Ties up a worker, and the delay is lost if it restarts.
- **Full jitter or no jitter.** Full jitter can retry immediately; none makes synchronized retries.
- **A message broker with delayed delivery.** Extra moving part; a database column plus a sweeper
  reuses what we already trust and survives a Redis flush.
- **Timeouts by running every task in its own process.** Truly killable, but heavy and a large redesign.
- **A cooperative deadline passed into each runner.** Cleaner, but changes the runner interface and
  every fake runner in the test suite, and still cannot interrupt a blocking HTTP call.
- **Idempotency keys that expire.** Real APIs expire them (for example after 24 h). Ours live as long as
  the mission, which is simpler and fine at this scale; a cleanup job can come later.

## Consequences

- A flaky LLM no longer fails a mission on the first error; worst case a task is attempted
  `max_attempts` (3) times, so wasted compute can reach three times a single attempt.
- **Enforced timeouts change behaviour for slow local models.** If real missions now time out, raise
  `TASK_TIMEOUT_SCALE` rather than editing agent rows.
- A timed-out attempt's thread can keep consuming a CPU or a request until its own limit; with many
  timeouts a worker process can accumulate such threads.
- Planner failures are *not* retried (planning is not a task); a failed plan still fails the mission.
- If the process dies after the API commits a mission but before its planner job starts, a replayed
  request returns the same mission without restarting it (the existing background-job gap).
- A `failed` event lost between the reaper's commit and its publish can leave a mission waiting;
  tracked in docs/RELIABILITY.md.

## How we know it works

`tests/unit/test_retry.py` (delay bounds per attempt, jitter floor and spread, which exceptions and
HTTP statuses are retryable), `tests/integration/test_retries.py` (transient vs permanent vs bug vs
cancelled, giving up after max attempts, a stray message cannot skip the delay, fail-wait-retry-succeed
end to end with the real scheduler and a worker, timeouts and abandoned runners, the sweeper's
throttling, lost messages and 15 rounds of two sweepers racing) and
`tests/integration/test_idempotency.py` (replay, conflict, malformed keys, tenants, 10 rounds of eight
simultaneous requests). The suite was mutation-tested with 23 deliberate bugs. `python -m
scripts.demo_retries` shows a flaky task recovering.