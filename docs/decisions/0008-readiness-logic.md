# 0008: Readiness logic as a pure function

Status: accepted

## Why is "what can run next" a pure function of task statuses instead of state held inside the scheduler?
plan_next takes each task's current status and dependencies and returns
a Decision (ready, skip, finished, succeeded). It uses no database, no
clock, and keeps no memory between calls.

- One source of truth: the statuses in Postgres are the only state. If
  the scheduler kept its own in-memory view ("I already started b"),
  the two could drift apart, causing double-starts, missed tasks, or
  missions that never finish.
- Crash recovery (M4): a scheduler that restarts has nothing to
  rebuild. It reads statuses from the database and calls plan_next
  again. In-memory state would be lost in a crash.
- Testability: a test builds a dict of statuses and checks the result,
  with no fixtures, fakes, or timing. That makes it cheap to cover many
  edge cases, including randomized simulation.
- Reuse: the sequential executor (ADR 0006) and the M2 parallel
  scheduler share the same decision logic, so the rules live in one
  place.

The design is "pure core, side effects at the edges": the scheduler does
the I/O (reading statuses, updating rows, starting work), and plan_next
does the thinking.

## Why does plan_next only consider PENDING tasks, and how does that prevent a task starting twice?
READY and RUNNING tasks are already in flight. plan_next never reports
them again, so a task appears in ready at most once per trip through
PENDING.

The caller's job is to move each ready task out of PENDING (to READY)
before it starts work. After that, no later call can return the task
again, so acting on decisions repeatedly is harmless.

One gap remains: two schedulers could read the same PENDING status
before either updates it. M4 closes this with a guarded update
(UPDATE ... SET status = 'ready' WHERE id = ... AND status = 'pending').
Only one update succeeds, and only the winner starts the task.
plan_next decides what may start; the database decides who starts it.

## What does "fail fast" mean here, and why skip a task whose other dependency is still running?
A task needs all of its dependencies to complete. If any dependency is
dead (failed, skipped, or cancelled), that output will never exist, so
the task can never run, whatever happens to its other dependencies.

So plan_next skips it immediately instead of waiting. In the example
(a → b → d, a → c → d), when c fails while b is still running, d is
skipped right away. Waiting for b would change nothing, and it would
leave doomed tasks in PENDING, delay the mission's final status, and
make the state misleading for anyone watching.

Skips are transitive in one pass. plan_next walks tasks in dependency
order and tracks each task's effective status, including skips it just
decided. So d and everything behind it are skipped together, instead of
one level per call.

## What exactly does `finished` mean, and what must a caller do before trusting it?
finished = True means: once the returned skips are applied, no task can
make progress. Nothing is ready to start, nothing is in flight, and
everything left is either done or doomed. succeeded says whether every
task completed.

It describes the state after the decision is applied, not the state in
the database right now. A task can still be PENDING in the database
while plan_next reports finished = True, because it's in this
decision's skip list.

Callers must:
1. Apply skip (mark those tasks skipped).
2. Start the ready tasks.
3. Only then act on finished: mark the mission completed if succeeded,
   otherwise failed.

This contract came from a wrong expectation in my own test (I expected
finished = False while a skippable task was still pending). The mistake
showed the meaning was ambiguous, so it's now documented here and locked
in with a test.

## What does the randomized simulation check that example tests can't, and what is "liveness"?
Example tests check cases I thought of. The randomized simulation
generates many random DAGs, then repeatedly calls plan_next, applies
the decision, and randomly completes or fails in-flight tasks until the
mission finishes. Random graphs and outcomes reach combinations nobody
would write by hand.

At every step it checks invariants, which are rules that must always
hold:
- Safety (nothing bad happens): a task is never reported ready twice;
  a task becomes ready only when all its dependencies are completed;
  a task is skipped only if a dependency is dead; finished never
  appears while a task is still in flight.
- Liveness (something good eventually happens): the system never gets
  stuck. As long as the mission isn't finished, either some task is
  ready or something is in flight, so every run reaches finished.
  A deadlock (tasks pending, nothing ready, nothing running, but not
  finished) is a liveness failure.
- Correct outcome: succeeded is True exactly when every task completed,
  and every task ends in a terminal status.

Example tests prove specific scenarios work. The simulation gives
confidence that the rules hold for every graph shape and order of
events, which matters most for the M2 parallel scheduler, where timing
is unpredictable.