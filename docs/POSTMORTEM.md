# Post-mortem: how AgentOS learned to survive crashes

This is a blameless review of the reliability problems found between milestones M2 and M4, in the order
they were found. For each: what happened, why, how it was found, how it was fixed, and what it
taught. The runtime's current guarantees are in [RELIABILITY.md](RELIABILITY.md); how they are tested
is in [FAILURE_TESTING.md](FAILURE_TESTING.md).

## Summary

| # | Problem | Found by | Fixed in |
|---|---|---|---|
| 1 | A worker killed mid-task left its task `running` for ever; the mission never finished | a manual `kill -9` experiment (M2) | #17 leases, heartbeats, reaper; #18 retries |
| 2 | Two sweepers could publish the same task | **CI** (multi-core), after every local run passed | the advisory lock in `claim_due_ready` (added by the author after CI failed) |
| 3 | A scheduler crash in a one-millisecond gap stalled a mission for a full minute | **failure injection** (a flaky process-kill test) | #19: stamp after publishing, plus a short grace |
| 4 | A failed acknowledgement ended the scheduler loop silently | reading the loop while designing the outage tests, then a test | #19 |
| 5 | Redis and Postgres calls could wait for ever | designing the partition test; measured 14 s per call even with a timeout | #19 (bounded; one case remains, see below) |
| 6 | A mission could stay `pending`, `planning` or `running` for ever after a lost event or a stopped API | asking "what if this message is lost?" for each message | #19 mission sweep and watchdog |
| 7 | A Postgres connection that stays open but never answers hangs the caller | the chaos tool's own bug accidentally modelled it; then proven on purpose | **open**, documented as an expected-failure test |

## 1. The original bug: a killed worker loses its task

**What happened.** A worker claims a task (`ready` to `running`) and later saves the result. If the
process dies between the two, nothing ever moves the task again. The restarted worker receives the
message, finds the task already `running`, treats it as a duplicate and drops it. The mission never
completes. Found in M2 by killing a worker by hand.

**Why.** The design could answer "is this task already taken?" but not "is whoever took it still
alive?". Liveness cannot be asked of a dead process; it has to be inferred from something the worker
keeps doing.

**Fix.** A lease with a heartbeat (`task_leases`), a reaper that recovers expired leases, and a
fencing token (the attempt number) so a slow worker that wakes up late cannot overwrite its
replacement (ADR 0016). Retries with backoff, timeouts and idempotency keys followed (ADR 0017).

**Lesson.** Every "claim" needs a way to un-claim. Anything one process can mark as "in progress"
needs an expiry or a liveness check.

## 2. Two sweepers published the same task (caught by CI)

**What happened.** A test asserting that two concurrent sweepers publish each task once passed in
every local run and failed in CI.

**Why.** `claim_due_ready` selected rows with `FOR UPDATE SKIP LOCKED` and stamped a *different*
table (`task_dispatch`). In PostgreSQL's default isolation (read committed), a statement that began
before another committed can still lock a row after the other's lock is released, and the re-check
of a locked row looks only at the locked table, not at the joined table that was stamped. So the
second sweeper still saw "not stamped" and published again.

**Why it was missed.** The developer machine and the sandbox where the code was verified had one CPU:
threads interleave there, but rarely overlap, so a 15-round race test could not trigger it. This was
also the reviewer's mistake: the test was presented as evidence of race safety, and it was not.

**Fix.** Sweepers are serialized with a transaction-level advisory lock taken in its own statement,
so the claim query gets a fresh snapshot after the lock (the author's fix after CI failed). A
sharper alternative for later is to make the claim a single compare-and-set on the stamped table.

**Lesson.** A concurrency test that passes on one core proves little. CI on several cores is part of
the evidence, and "passed locally" is not.

## 3. A one-millisecond crash window cost a minute

**What happened.** In the process-kill test that kills the scheduler and the worker together, one run
in several stalled for 60 s before recovering.

**Why.** To make the sweeper safe, a task was stamped "message published" the moment it became
`ready`, before the scheduler actually published it. If the scheduler died in between, the task looked
recently queued and the sweeper waited the full 60 s trust window. The design choice had been
accepted for the sweeper's own publishes, but it also applied to the scheduler's everyday path, so the
most common crash window cost as much as the worst case.

**How it was found.** Failure injection plus a diagnostics dump in the test's failure message: the
database state showed a `running` task claimed exactly 60 s after the crash, and the sweeper's log
line at the same second.

**Fix.** The scheduler stamps after publishing; an unstamped `ready` task is left alone for a short
grace (5 s) so the sweeper does not race the scheduler, then published. Regression test:
`test_a_crash_between_marking_ready_and_publishing_waits_for_the_grace_not_the_republish_interval`.

**Lesson.** When a safeguard waits for a timer, ask what ordinary failure will make it wait the whole
timer. Order the steps so that the common crash leaves the state that is repaired fastest.

## 4. A failed acknowledgement ended the scheduler

The scheduler acknowledged events without a guard. A Redis error at that instant raised out of the
loop and ended it: the process would have stayed up, scheduling nothing, with no error visible except
one log line. It is now caught and the event is simply handled again (a scheduling pass is
idempotent). Found by reading the loop before writing the outage tests; pinned by a test that fails
one acknowledgement.

## 5. Waiting for ever

Neither Redis nor Postgres calls had timeouts: a network partition gives no error, only silence, and
would have hung a worker. Both are now bounded. A measurement along the way: with a 1 s Redis socket
timeout one call into a black hole took 14.4 s, because the client library retries internally with
growing pauses; that hidden retry layer is switched off since the loops already retry.

## 6. Missions that waited for ever

Every message in the system can be lost: a worker that saved a result and died before announcing it,
a reaper that failed a task and died before telling the scheduler, an API that stopped before planning
a new mission. Each would leave a mission waiting with nothing to wake it. The scheduler now also
looks at the database, not only at messages: a running mission with nothing in flight and no activity
for a while gets a scheduling pass (idempotent); a mission stuck `pending` or `planning` beyond the
longest plausible planning time is declared failed, with an error that says to resubmit.

**Not fixed:** a mission whose planner stopped is failed, not resumed.

## 7. Open: the silent connection

A Postgres connection that stays open but never answers (a middlebox or proxy that swallows traffic)
hangs the caller indefinitely. Only connecting and a network that truly vanishes (TCP keepalives and a
user timeout) are bounded. The fix needs client-side query timeouts. It is recorded as an expected
failure in `tests/chaos/test_known_gaps.py` so that fixing it turns the test red until the marker is
removed.

## Mistakes in the tooling and process (also worth recording)

- **The chaos proxy had two bugs.** Closing a socket does not wake a thread blocked on it, so a "reset"
  connection could stay half alive and swallow a COMMIT (a test hung for 79 s); and an idle timeout
  left over from connecting reset any connection quiet for 5 s. Both were found because a test
  misbehaved, and both would have produced false confidence if they had failed the other way.
- **A killed mutation-testing run left a deliberately broken line in the source** and leftover rows
  that made every later run fail the same way, which first looked like "all mutants caught". The script
  now backs up and restores files, and refuses to run if the unmutated baseline fails.
- **A helper script deleted the lock file of a healthy database** (it tested for the wrong command), which
  restarted the database under the tests and caused spurious "not available" failures.
- **Two tests of this issue were timing-sensitive** and passed on a quiet machine: one asserted a
  failure had happened a moment before it had. A CPU-starved run found it.

## What went well

- The fixes were found by experiments that killed things on purpose, not by reading code.
- Each guarantee is tied to a named test, and the new logic was mutation-tested (deliberate bugs were
  introduced one at a time and each made a test fail).
- The documented gaps stayed honest: where something is not fixed, a test or a line in RELIABILITY.md
  says so.

## Action items

| Action | Status |
|---|---|
| Leases, heartbeats, reaper, fencing | done (#17) |
| Retries, backoff, timeouts, idempotency keys, sweeper | done (#18) |
| Lock concurrent sweepers | done (by the author, after CI) |
| Stamp after publishing, grace period | done (#19) |
| Bounded Redis/Postgres connections, scheduler ack guard | done (#19) |
| Mission sweep and planning watchdog | done (#19) |
| Client-side query timeouts for a silent Postgres | open |
| Resume (not fail) a mission whose planner stopped | open |
| `XAUTOCLAIM` for messages owned by a dead consumer, dead-letter queue | open |
| Run the concurrency tests on several cores in CI every time | already true (GitHub Actions); keep it that way |
