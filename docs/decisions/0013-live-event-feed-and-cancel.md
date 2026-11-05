# 0013: Live event feed, mission list, and cancelling

Status: accepted, 2026-11-04
Issue: #14

## Context

The dashboard (issue 15) needs three things from the backend: a live view of one mission as its
tasks change, a list of missions to choose from, and a way to stop a mission. Until now a client
could only poll `GET /missions/{id}`. Progress is written by several independent processes (the
planner in the API, the scheduler, many workers), and more writers will come (a sweeper in M4,
approvals in M6), so any design that depends on each writer remembering to announce its changes
will eventually miss one.

## Decision

- **The database announces its own changes.** A trigger (migration 002) calls `pg_notify` after
  every insert or update on `tasks` and `missions`. The payload is only ids and a status.
- **One listener per API process.** `PgListener` runs on a thread (psycopg's async mode does not
  work on Windows' default event loop), reads the notice, loads the CURRENT row, builds a full
  message and hands it to an in-process `EventHub`, which feeds each WebSocket watching that
  mission. It does no work for missions nobody is watching.
- **Snapshot, then stream.** `WS /api/v1/missions/{id}/stream` subscribes to the hub first, then
  sends a snapshot, then forwards messages. Every message is a complete state, so duplicates and
  reordering are harmless. The server closes with 1000 once the mission is finished.
- **Never trust the stream to be complete.** After a listener reconnect the hub tells every
  watcher to resync (a fresh snapshot). A client that falls 200 messages behind is cut off
  (close 1013) and reconnects into a new snapshot.
- **Cancel is a guarded bulk update**, and the planner and worker were made safe against it.
- **`/api/v1` prefix and cursor pagination** were added now, before a frontend hardcodes URLs.

## Design questions and answers

**1. Why a database trigger with NOTIFY instead of polling, or publishing from each writer?**
Polling adds latency and load per viewer. Publishing from each writer (say to a Redis channel) is
fast but fragile: each place that changes a status must remember to publish, and a writer can
publish a change that is later rolled back, or commit a change and crash before publishing.
NOTIFY inside the transaction fixes both: the notice is delivered only if the transaction
commits, and every writer is covered with no code in it, including writers that do not exist yet.
The costs: it is specific to Postgres; delivery is fire-and-forget (a disconnected listener
misses notices); the payload is limited; each API process needs one connection for LISTEN; and
transactions that notify commit one at a time, which can become a bottleneck at high write rates.
That is far above our scale, and if it ever mattered the answer would be an outbox table feeding
a stream.

**2. Why does the payload carry only ids and a status? What would go wrong otherwise?**
Postgres rejects a notification payload over 8000 bytes, and that error aborts the transaction
that issued it. A task's output can be hundreds of kilobytes, so putting it in the payload would
make a worker's result write fail and the result would be lost. With ids only, a 300 KB output
saves normally and still produces a notice (there is a test for exactly that). The listener
reads the row to build the message, so the large fields reach the client through the database,
not through the notification.

**3. Why subscribe before taking the snapshot, and why are messages states instead of deltas?**
If the snapshot were read first and the subscription made second, a change committed in between
would be in neither, and the client would show stale data until the next change to that task,
possibly never. Subscribing first closes the gap; the price is that a change can be reported
twice, once inside the snapshot and once as a message. That is harmless because each message is
the complete current state of a task or mission: applying it twice, or out of order, just
replaces what the client holds. The ordering guarantee is by construction rather than by a
deterministic test; what is tested is that a client rebuilt only from the stream equals what the
REST endpoint reports, and that per-task status never moves backwards.

**4. What if the listener loses its connection?**
Notices sent while it was away are gone, so the stream cannot promise completeness. Instead of
trying, the design stays correct without it: after reconnecting and re-issuing LISTEN, the
listener calls `on_reconnect`, the hub puts a resync marker in every watcher's queue, and each
connection sends a fresh snapshot from the database, which remains the source of truth. A test
kills the listener's backend connection and checks that it reconnects, signals, and carries on.

**5. How are slow clients handled?**
Each connection has a bounded queue of 200 messages. If it overflows, that client's backlog is
dropped and it is closed with code 1013 ("try again later"); on reconnecting it receives a new
snapshot. This bounds memory, stops one slow browser from delaying the others, and loses
nothing because the snapshot restores the state.

**6. What does "cancel" really do, and what could go wrong?**
One transaction marks the mission cancelled and every unfinished task cancelled; finished tasks
keep their results. It cannot stop work already in progress: a running task finishes (and the LLM
tokens are spent), but its result write no longer matches the guarded update, so the worker
discards it quietly, acknowledges the message, and the scheduler ignores a cancelled mission.
Two races had to be closed. A planner that finished after the user cancelled used to overwrite
`cancelled` with `running`; now the plan is stored only if the mission is still `planning`
(checked under a row lock) and a late planning failure cannot overwrite a cancellation. As a
side effect the planner's claim is now a single guarded update, which also fixes the old
limitation that two callers could both plan the same mission. Not solved: actually interrupting a
running task, which needs leases and timeouts (M4).

**7. Why cursor pagination and `/api/v1` now?**
Paging with OFFSET repeats or skips rows when a mission is created between two requests.
A cursor names the last row seen by (created_at, id), so "everything older than that row" is
stable, and the id breaks ties between missions created in the same instant. The URL prefix is
cheap today and expensive once a frontend and any scripts depend on bare paths.

**8. Limits to state honestly.**
(a) There is no authentication or origin check on the WebSocket (M5), and CORS is not configured
yet (needed for the dashboard in issue 15). (b) Every task event re-reads the whole mission's
tasks, so cost grows with graph size; fine for tens of tasks. (c) The hub is per process: several
API processes each hold their own listener, which is correct but means resource use scales with
the number of API processes. (d) A worker that dies mid-task leaves its task `running` forever
(see ADR 0012), and the dashboard will show it as running: recovery is M4. (e) The listener and
WebSocket were tested on Linux; Windows is untested until the author runs the suite.

## Consequences

- Any future writer of task or mission state is streamed to the UI automatically.
- The API process now owns a background thread and a database connection for LISTEN, started and
  stopped by the app's lifespan; `app.state.hub` is None when no feed is running.
- Migration 002 is safe to run twice (it creates a trigger only if missing, instead of dropping
  and recreating, which would wait for any session reading the table). The migration runner now
  gives up after 10 seconds when another session holds a lock and lists the blocking sessions,
  instead of waiting silently forever.
- A mission's REST and stream views are built by the same functions (`services/api/views.py`),
  so they cannot drift apart.
