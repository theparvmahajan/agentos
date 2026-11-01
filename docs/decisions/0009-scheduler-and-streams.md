# 0009: Scheduler on Redis Streams

Status: accepted

## Context

Missions and tasks live in Postgres. We need a scheduler that decides which tasks can run next, and workers that run them. The two sides communicate through two Redis Streams:

- **`events`** carries "something changed in mission X" from the planner and workers to the scheduler.
- **`tasks`** carries "task T is ready" from the scheduler to workers.

```
Postgres (source of truth: missions, tasks, statuses, outputs)
   ▲ read + guarded updates          ▲ claim, finish
planner/workers ─▶ [events] ─▶ SCHEDULER ─▶ [tasks] ─▶ workers
                   (ids only)  plan_next()  (ids only)
```

---

## Why does Postgres stay the source of truth, with streams carrying only ids?

A message is a **nudge** ("look at mission X"), never data. The database always says what is true.

- **Duplicated, late, or lost messages cannot corrupt state.** The scheduler re-reads the full current state on every pass, so a stale or repeated nudge just produces a pass that finds nothing new to do.
- **There is one authority for state.** The state machine and constraints live in one place. Redis can be flushed or restarted without losing missions, tasks, or outputs.
- **Messages stay small and cheap.** Ids-only messages keep the streams light and avoid payload schema and versioning problems.
- **There is no data to go stale.** A message carrying a status or output could be out of date by the time it is consumed. An id always resolves to the current row.

The cost is one extra DB read per message, which is acceptable because a pass reads the mission state anyway.

---

## What does at-least-once delivery mean, and which mechanisms make a duplicate message harmless?

**Meaning.** A consumer group delivers each message to exactly one consumer. The message stays in that consumer's pending list until it calls `XACK`. If the consumer dies first, the message is replayed. Nothing is lost, but a message can be processed more than once.

**Mechanisms that make duplicates harmless:**

1. **Guarded updates.** `PENDING → READY` is a single `UPDATE ... WHERE status = 'PENDING'` that also enforces the state machine. If several schedulers race, exactly one wins. The others get `InvalidTransition` and do nothing.
2. **Guarded mission completion.** `UPDATE missions SET status = ... WHERE id = :id AND status = 'running' AND NOT EXISTS (unfinished tasks)`. Zero rows updated means someone else already finished it, which is fine.
3. **Guarded task claim.** A worker claims a READY task with a guarded update, so a task delivered twice runs once.
4. **One pass per mission per batch.** Five events for one mission cause one pass, because a pass reads the full current state anyway.
5. **Ids-only messages.** Replaying a message never replays stale data.

---

## Why mark a task READY before publishing it, and what failure window does that leave? How will it be closed?

**Why this order.** A worker that receives a message must find the task READY and claimable. If we published first, the message could reach a worker before the `PENDING → READY` update committed. The worker would find the task not claimable and drop or fail it, and the task would be lost.

**The failure window.** If the scheduler crashes after marking READY (and committing) but before `XADD` to `tasks`, the task is READY but never queued. No message will ever name it. A retried pass makes this worse: the retry sees the task already READY, loses the guarded `PENDING → READY`, and publishes nothing.

**How it is closed.**

- **Milestone 4 sweeper.** A sweeper periodically finds READY tasks that have been stale for too long and re-publishes them. This is safe because the worker's claim is guarded, so duplicates are harmless.
- **Cheap mitigation now (optional).** Each pass publishes *every* READY task of the mission, not only the ones it just flipped. A retried pass then re-publishes a task whose first publish failed. If we don't adopt this, we record the gap explicitly as an M4 dependency.

---

## Why must a worker publish its event only after committing to the database?

The event triggers a scheduler pass that reads Postgres. If the worker published before committing, the scheduler could run before the result is visible. It would see the old state, find nothing to schedule, and acknowledge the event. The commit would land afterwards with nothing left to wake the scheduler. That is a **lost wake-up**.

Committing first means any pass triggered by the event is guaranteed to see the new state.

**Remaining gap.** If the worker commits and then dies before `XADD events`, nobody notifies the scheduler. The M4 sweeper should also re-nudge `running` missions with no recent activity.

---

## What happens to an event whose scheduling pass fails? What is still missing (dead letters, reclaiming)?

**What happens now.**

- The event is **not acknowledged**, so it stays in the consumer's pending list.
- The loop first re-reads its own pending messages (`XREADGROUP` with id `0`, i.e. `pending=True`) and only then waits for new ones (`>`). The failed event is retried on the next iteration.
- A consumer restarted under the **same name** recovers everything it never finished.
- Acknowledgement is **per mission**. Only the message ids of missions whose pass succeeded are `XACK`ed, so one bad mission does not force the others to be replayed.
- After a failed pending pass the loop sleeps (about one second) so it doesn't spin hot.

**What is still missing (all planned for M4):**

| Gap | Consequence today | Planned fix |
|---|---|---|
| **Poison events**: no delivery limit, no dead-letter queue | A permanently failing event is retried forever, about once a second, with log noise | Cap deliveries (using the pending entry's delivery count) and move the event to a dead-letter stream |
| **No reclaiming** (`XAUTOCLAIM`) | Messages pending under a consumer name that never returns are stuck | Reclaim messages idle longer than a threshold to a live consumer |
| **Stale READY tasks** | Tasks marked READY but never published stay stuck | Sweeper re-publishes them |
| **Lost event after worker commit** | A mission can stall without a wake-up | Sweeper re-nudges inactive `running` missions |

Consumer names must be stable across restarts (for example hostname plus slot). Otherwise pending messages are orphaned until reclaiming exists.

---

## Why is it safe to run several schedulers at once?

1. **The consumer group splits the work.** Each event goes to exactly one scheduler, as long as each runs under a distinct consumer name.
2. **Overlap is still safe.** After a redelivery or reclaim, two schedulers may process the same mission at once. All state changes are guarded updates, so one wins and the others see `InvalidTransition` and do nothing.
3. **No locks or leader election are needed.** The database's `WHERE status = ...` guards provide the mutual exclusion.
4. **Passes are idempotent.** A pass derives its actions from current database state, not from the message, so doing it twice or concurrently gives the same result.
5. **Failures are isolated.** One scheduler crashing leaves its pending messages for restart or later reclaiming. The others continue unaffected.

---

## Consequences

- Correctness depends on the **guarded updates**, not on Redis delivery guarantees.
- Redis can lose or duplicate messages without corrupting state. The cost is delayed progress until the M4 sweeper exists.
- Until M4, these cases need manual attention: a crash between mark-READY and publish, a worker dying before its event, a poison event, and a permanently dead consumer name.
- The `tasks` stream has no consumer yet, and the API still uses `BackgroundTasks` with the sequential executor. Both are addressed in later issues.