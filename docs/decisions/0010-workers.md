# 0010: Workers

Status: accepted

## Context

The scheduler (ADR 0009) marks tasks READY in Postgres and publishes their ids to the `tasks` stream. Workers consume that stream, run each task's agent, and report back. Postgres stays the source of truth, and messages carry only ids.

```
tasks stream ──▶ WORKER                                        events stream
  {task_id}      1. read task, dependencies, agent                  ▲
                 2. CLAIM: READY → RUNNING (guarded update)         │
                 3. run the agent                                   │
                 4. save COMPLETED/FAILED to Postgres (committed)   │
                 5. announce ───────────────────────────────────────┘
                 6. acknowledge the message
```

There is no separate results stream. The `events` stream is the results channel: a worker saves its result, then announces "task X changed", and the scheduler wakes up and re-plans.

---

## How does a worker guarantee a task runs once even if its message is delivered several times?

Redis gives at-least-once delivery, so the same message can arrive twice (retry, replay, or a duplicate publish by the scheduler). Execution is made exactly-once by the claim in step 2, which is a single guarded update:

```sql
UPDATE tasks SET status = 'running' WHERE id = :id AND status = 'ready'
```

- Exactly one worker wins this update. The agent runs only in the worker that won.
- Every other worker sees `InvalidTransition`, treats the message as a duplicate, and does not run the agent.
- The guard lives in the database, so it holds across threads, processes, and machines.
- Tested by giving the same message to 6 threads at once: the agent ran exactly once.

Caveat: this guarantees at most one *claim*. If the winner dies mid-run, the task is stuck rather than run twice (see the gap below). Once M4 adds retries, an agent may run more than once for the same task, so agents with side effects will need to be idempotent.

---

## Why does the worker read everything before claiming, and what does it do on a duplicate of a FINISHED task?

**Read before claim.** The worker loads the task, its dependencies' outputs, and its agent before step 2.

- If the database fails during these reads, nothing has changed. The message stays pending and is simply retried.
- If the worker claimed first and then failed to load its inputs, the task would sit in RUNNING although nothing ever started.
- Reads that precede the claim never leave partial state behind.

**Duplicate of a finished task.** If the redelivered message finds the task already COMPLETED or FAILED, the worker does not run anything. It announces the task again and acknowledges the message.

- The first worker may have saved the result and died before announcing. Without the re-announcement, the scheduler would never hear about the result.
- A repeated announcement is harmless, because a scheduler pass reads the full current state and is idempotent.

---

## Why the order "save result, announce, acknowledge"? Walk through a crash between each pair.

Each step is made durable before the next one, so a crash between any two steps is repaired by redelivery.

| Crash point | State left behind | On redelivery |
|---|---|---|
| During the agent run (before save) | Task RUNNING, message pending | Claim fails and the message is dropped as a duplicate. The task stays RUNNING forever ❌ (the known gap below) |
| Between **save** and **announce** | Task finished in Postgres, no event sent, message pending | The worker finds the task finished, re-announces, acknowledges ✅ |
| Between **announce** and **acknowledge** | Task finished, event sent, message pending | The worker finds the task finished, announces again (the scheduler runs one redundant, idempotent pass), acknowledges ✅ |
| Redis down while announcing | Same as the save/announce crash | The exception leaves the message pending, and the replay re-announces ✅ |

Why this order and not another:

- **Save before announce.** The scheduler pass the event triggers must see the result. Announcing first could wake the scheduler before the commit and lose the wake-up (same reasoning as ADR 0009).
- **Announce before acknowledge.** If the worker acknowledged first and died before announcing, the message would be gone and nobody would tell the scheduler.
- **Acknowledge last.** Until then the message sits in the pending list and can be replayed.

The cost of this ordering is that duplicates are possible (an extra announcement), but a lost result or lost wake-up is not.

---

## Why are failures announced to the scheduler?

A FAILED result is saved and announced exactly like a COMPLETED one. The scheduler has to hear about it:

- It must **skip the dependents** of a failed task, since they can never run.
- It must be able to **finish the mission** (as failed) once every task is either done or skipped.
- If failures were silent, the mission would stay RUNNING forever.

---

## What failure does this design NOT handle yet (a worker dying mid-task), and why can't the worker tell it apart from a live worker?

**The gap.** If a worker dies after claiming (task RUNNING) and before saving, the task is orphaned in RUNNING. The message is redelivered, but the claim fails because the task is no longer READY. The worker logs "duplicate message, nothing to run", acknowledges it, and the task is never touched again.

Reproduced: after `kill -9` on both workers mid-task and a restart, the logs said "duplicate message, nothing to run". Ten seconds later the mission was still RUNNING with `t2=running t3=running`. The same stuck state occurs if the agent finishes but saving the result fails.

**Why the worker can't tell the cases apart.** When the claim fails on a RUNNING task, the database only says "someone claimed this". It does not say whether that someone is alive.

- *Another live worker is running it:* dropping and acknowledging the duplicate is correct.
- *The worker running it died:* dropping and acknowledging the duplicate is fatal.

Both cases look identical: status `running`, claim rejected. The database holds no evidence of liveness.

**What closes it (M4):**

- **Leases and heartbeats** on RUNNING tasks, so liveness is recorded in the database.
- **Timeouts** that return an expired RUNNING task to READY (or mark it FAILED).
- **Retries** with an attempt limit.
- **Reclaiming** (`XAUTOCLAIM`), dead letters, and the sweeper, as in ADR 0009.

The duplicate-of-RUNNING branch must be replaced by lease logic ("is the lease still valid?"), not supplemented, because duplicate task messages are expected to be common.

---

## How do you scale workers, and what stops two of them taking the same task?

**Scaling.**

- Run more worker processes, or set `WORKER_CONCURRENCY=N` to run N threads in one process.
- Each worker uses its own consumer name, and the consumer group splits the messages between them.
- Consumer names must be stable across restarts (for example hostname plus slot). Otherwise pending messages are orphaned until `XAUTOCLAIM` exists.

**What stops two workers taking the same task.**

- The consumer group normally delivers each message to one consumer.
- Redelivery or duplicate publishes can still put the same task in front of two workers. The guarded `READY → RUNNING` update then lets exactly one win, and the loser drops the message.
- No locks, leader election, or coordination between workers are needed.

---

## Consequences

- Correctness rests on the guarded claim and the save → announce → acknowledge ordering, not on Redis delivery guarantees.
- Redis can duplicate or delay messages without running a task twice or losing a result.
- Until M4, a worker crash during a run leaves the task and its mission stuck in RUNNING and needs manual repair.
- The API switches from `BackgroundTasks` and the sequential executor to this scheduler and worker pipeline.
- Step 7 reproduces the stuck-RUNNING case, which is the motivation for M4.