# 0012: The API hands missions to the scheduler; proving parallelism

Status: accepted, 2026-11-04
Issue: #13

## Context

Through issue 12 the API ran a mission inside its own background thread with the sequential
executor: one task at a time, lost if the API restarted. The scheduler and worker processes
(issues 10 and 11) existed but the API did not use them. A multi-agent runtime has to show two
things: missions survive the process that started them, and independent tasks really run at the
same time.

## Decision

- **`plan_and_publish`** replaces `process_mission` as the API's background job. It runs the
  planner (one LLM call), then publishes one event. The scheduler and workers do the rest. If the
  event cannot be published after 3 attempts (0, 0.2 and 0.5 seconds apart), the mission is
  failed with the reason and its pending tasks are cancelled, so nothing is left looking
  runnable. The job never raises: nobody is waiting to catch an exception.
- **A deterministic `sleeper` agent** (`sleep 0.5`) makes the benchmark measure orchestration
  instead of LLM latency. Its runner is registered, but no agent row is ever seeded, so the
  planner cannot offer it.
- **`scripts/benchmark.py`** (`make bench`) compares the sequential executor with the scheduler
  plus 1, 2 and 4 worker **processes** on chain, diamond and fan-out graphs, reports median and
  range, and compares with the ideal time. It writes `docs/BENCHMARKS.md`.
- **`execute_mission` stays**: it is the sequential baseline and still backs `try_mission`.

## Design questions and answers

**1. What happens on three kinds of restart?**
*The API during planning:* the mission stays `planning` and nothing retries it. This is **not
handled**; it needs claiming and a sweeper in milestone 4. *The API after planning:* nothing is
lost. The event is already on the stream and everything else lives in the scheduler, the workers,
Postgres and Redis; the API only serves reads. This is new: before, a restart killed the whole run.
*A worker in the middle of a task:* its message is delivered but not acknowledged, so it stays
pending in Redis, and the same worker name re-reads its own pending messages on restart and
finishes or retries the task; every status change is a guarded update, so doing it twice is
safe. What is **not** handled: a worker that dies and never returns under the same name. Another
worker does not take over its message yet. That is also milestone 4.

**2. Why fail the mission when Redis is unreachable?**
Leaving it `running` would be a silent hang: the user would see a running mission that nothing
will ever schedule. Retrying briefly covers a short blip, and after that failing loudly with the
cause is more honest. Cancelling the pending tasks keeps the state consistent (no failed mission
with runnable-looking tasks). A sweeper in milestone 4 would make failing unnecessary: it would
periodically find running missions with no activity and re-publish their event, which is safe
because scheduling is idempotent. Until it exists, failing is the correct behaviour.

**3. Why sleeping tasks, and what do they not measure?**
A sleeping task takes the same time every run, so any extra time in a run is the cost of the
runtime: scheduler wake-up, queue hops, the guarded claims, status writes and event publishing.
They do not measure LLM latency and its variance, tool time, CPU or memory contention, or failure
handling. A benchmark with real LLM calls on a laptop with one 4 GB GPU would mostly measure the
GPU: parallel requests queue at the model server, the speedup would be close to 1x, and the
runtime would be blamed for something it does not control.

**4. Reading the table.**
A chain of four dependent tasks cannot start a task before its predecessor finishes, so extra
workers sit idle and the speedup is about 1x (slightly below it because of overhead). That is
correct: it shows the dependency logic holds back work that is not ready. Fan-out-8 falls short
of the ideal speedup because the ideal assumes scheduling is free, while every round of tasks
pays for it: the event reaches the scheduler, the scheduler reads the graph and enqueues, a worker
wakes and claims the task (a guarded update), writes the result and publishes the next event.
From my own table: chain-4 reached 0.71x, fan-out-8 with 4 workers reached
2.42x against an ideal of 3.0x, which is about
277 ms of overhead per round of tasks. Two sources of it:
the scheduler round trip (event, database read, enqueue) and the worker's claim and write cycle;
a smaller part is the benchmark's own 10 ms polling for completion.

**5. Why test with threads and benchmark with processes?**
The tests run the scheduler and workers as threads, which makes them fast and deterministic enough
for CI: they assert overlap, ordering and generous time bounds (a forced lower bound and an upper
bound with 3x slack). But threads share a process, so they cannot show problems that only appear
across process boundaries, and their timing is not a measurement. The benchmark uses real
processes with their own database connections and a real queue, so its numbers describe the
deployed shape. It is slow, depends on the machine, and is not suitable as a pass/fail test.

**6. The honest resume claim.**
Something like: "Built a Redis Streams and Postgres task scheduler; with 4 worker processes a
fan-out-8 workflow finished in 2.33 s against 5.64 s sequentially
(2.42x, ideal 3.0x), measured with deterministic sleep
tasks." Not claimed: any speedup for real LLM agents; scaling across machines; behaviour with
several schedulers (safe by design, not benchmarked); or "exactly-once" in general. The accurate
phrase is effectively-once state transitions on at-least-once delivery.

## Consequences

- The API process needs only the LLM and Redis; it no longer imports agents or tools.
- A mission survives an API restart once it is planned. A mission stuck in planning does not.
- Benchmarks are reproducible with `make bench`, and `docs/BENCHMARKS.md` records the machine.
- Planning still runs inside the API process; a slow LLM occupies one API thread.
- The benchmark has been run on Linux; Windows is untested until the author runs it.
