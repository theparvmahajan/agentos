# 0018: Failure injection, connection limits and the mission watchdog

Status: accepted
Issue: #19
Related: ADR 0016 (leases), ADR 0017 (retries, sweeper), docs/FAILURE_TESTING.md, docs/POSTMORTEM.md

## Context

By the end of #18 the runtime recovered from a killed worker and retried failed work, and every
guarantee had a unit or integration test. But those tests simulated failures inside one process
(an exception raised by a fake). They could not show what happens when Redis or Postgres really
disappears, when a process is killed at an awkward instant, or when a message is simply lost. The
engineering addendum asks for exactly that: ten named failure scenarios, each with an expected state
and recovery behaviour, and a record of what the system can actually guarantee.

## Decision

**1. Inject failures at the network and process level, not by mocking exceptions.**
`tests/chaos/proxy.py` is a TCP proxy between a component and Redis or Postgres with three modes: up,
reset (connections cut), blackhole (open but silent). Processes are killed with `Popen.kill()` and
replaced. The tests check the final state in Postgres, not just that nothing crashed.

**2. Write the pure-Python proxy instead of using Toxiproxy or `docker network` tricks.** It runs
the same on the author's Windows machine, Linux CI and macOS with no extra binary or container, so
the tests can run in the normal CI job. The price is realism: it models resets and silence, not packet
loss or latency, and it had two bugs of its own (a "reset" that did not reset, an idle timeout) found
because tests misbehaved.

**3. Bound every wait.** A Redis client gets a socket timeout and a connect timeout, and client-side
retries are off (our loops already retry, and the library's hidden retries made one call into a black
hole take 14 s with a 1 s timeout). Postgres connections get a connect timeout, TCP keepalives and
`tcp_user_timeout`. This is deliberately partial: a Postgres connection that stays open but never
answers still hangs the caller. That is recorded as an expected-failure test rather than hidden.

**4. A scheduler pass for the database, not only for messages (`sweep_missions`).** Events wake the
scheduler, and any event can be lost (a crash between a commit and its publish). Every
`MISSION_SWEEP_INTERVAL_SECONDS` (15 s) the scheduler also runs its idempotent scheduling pass for
every running mission that has nothing in flight and has been quiet for `MISSION_STALE_SECONDS`
(30 s). This repairs a lost "task finished" or "task failed" announcement without trying to prevent
every loss.

**5. Declare a mission failed if planning never finishes.** Planning runs as a background job in the
API process. If that process stops after the mission was saved, nothing would ever plan it. A mission
that stays `pending` or `planning` longer than `PLANNING_STALE_SECONDS` (900 s, safely above two LLM
calls at their timeout) is failed with an error that says to resubmit. We chose failing over resuming
because resuming needs the planner to run outside the API process, which is a larger change; a planner
that was merely slow and finishes later is harmless (`run_planner` is already guarded and discards its
plan).

**6. Stamp "message published" after publishing, with a short grace (amends ADR 0017).** Found by
a flaky process-kill test: stamping at the moment a task becomes `ready` made a crash in the gap cost
a whole minute. See the amendment at the end of ADR 0017.

**7. Make the scheduler survive a failed acknowledgement.** An unguarded `XACK` ended the loop on a
Redis error. It is now caught; the event is handled again, which is safe because a scheduling pass is
idempotent.

## Alternatives considered

- **Mock exceptions only.** Fast, but would never have found the hung connection, the half-alive
  "reset", or the 14 s hidden retry. Real sockets are the point.
- **Toxiproxy, `docker network disconnect`, `pg_terminate_backend`.** More realistic (or simpler for
  Postgres resets) but add tools to install or need Docker in the test job; the last cannot model a
  silent connection.
- **Make every message delivery reliable** (an outbox with transactional publishing) so no message
  can be lost. Correct but a large redesign. Safety nets that look at the database repair the same
  failures for a fraction of the code, at the price of recovery measured in seconds, not milliseconds.
- **Resume interrupted planning.** Deferred (see decision 5).
- **Fail fast with no watchdog.** Leaves missions in `running` for ever after any lost message, which
  is the worst failure mode for a user: no result and no error.

## Consequences

- Missions no longer wait for ever after the failures covered; the worst case is bounded by the
  settings (a lost event is repaired within about `MISSION_STALE_SECONDS + MISSION_SWEEP_INTERVAL_SECONDS`,
  45 s by default).
- The chaos tests add about 45 s to the suite and use real processes, so they are the likeliest to
  flake on a slow runner; each waits for conditions rather than sleeping.
- Timing-sensitive and concurrency tests are only trustworthy when run on several cores. The sweeper
  race of #18 was invisible on one core.
- Still open: a silent Postgres connection; planner restarts fail rather than resume; messages owned by
  a dead consumer are not reclaimed; no dead-letter queue.

## How we know it works

`tests/chaos/` (outages, process kills, stuck missions, the known gap), `tests/unit/test_connection_limits.py`
and the settings tests. The new logic was mutation-tested with 12 deliberate bugs, all caught.
`python -m scripts.measure_recovery` measures recovery time; on the development sandbox it recovered a
killed worker in 32.1 to 40.6 s (median 37.3 s, 4 trials) with the shipped defaults, inside the 21 to 44 s
range the settings predict. The outage tests and the reliability documents state what remains untested.