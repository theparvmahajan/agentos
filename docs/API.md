# AgentOS API (v1)

Base path: `/api/v1`. Interactive docs for the REST part: `/docs` (OpenAPI). Errors are
`{"detail": "..."}`. There is no authentication yet (milestone M5): bind the API to localhost.
Browsers may call it only from the origins in the `CORS_ORIGINS` setting (default: the dashboard on
localhost:3000); the WebSocket is not covered by CORS and does not check its origin.

## REST

| Method and path | Purpose | Success | Errors |
|---|---|---|---|
| `POST /missions` | Create a mission from `{"objective": "..."}` (1-2000 chars). Planning starts in the background. | `202` + `Location` header + the mission | `422` invalid body |
| `GET /missions` | List missions, newest first. Query: `limit` (1-100, default 20), `cursor`, `status`. | `200` page | `422` bad cursor / limit / status |
| `GET /missions/{id}` | One mission with all its tasks, dependencies first. | `200` | `404`, `422` bad id |
| `POST /missions/{id}/cancel` | Cancel a mission that has not finished. | `200` the cancelled mission | `404`, `409` already finished, `422` |
| `GET /health` (unversioned) | Liveness. | `200` | |

A mission's `status` is one of `pending, planning, running, completed, failed, cancelled`.
A task's `status` is one of `pending, ready, running, completed, failed, skipped, cancelled`.

### Paging

`GET /missions` returns `{"items": [...], "next_cursor": "..." | null}`. Pass `next_cursor` back as
`?cursor=` for the next page; `null` means there is none. Items carry `tasks_total` and
`tasks_completed` instead of the tasks themselves. A cursor names the last row you received, so
missions created while you page never cause repeats or gaps. Treat cursors as opaque.

### Cancelling

Every task that has not finished is marked `cancelled` immediately and the mission becomes
`cancelled`. A task that is already running cannot be interrupted: it finishes its current work,
but its result is discarded. Finished tasks (`completed`, `failed`) keep their results.

## WebSocket: `GET /api/v1/missions/{id}/stream`

One connection follows one mission live. Every message is JSON with a `type`.

```jsonc
// 1. Always first (and again after a resync): the whole mission. Replace everything you hold.
{"type": "snapshot", "mission": {"id": "...", "objective": "...", "status": "running",
  "result": null, "error": null, "created_at": "...", "updated_at": "...", "completed_at": null,
  "tasks": [{"id": "...", "key": "t1", "agent": "researcher", "instruction": "...",
             "status": "running", "depends_on": [], "attempts": 1, "output": null,
             "error": null, "started_at": "...", "finished_at": null}]}}

// 2. One per task change: the task's complete CURRENT state. A key you have not seen is a new node.
{"type": "task", "mission_id": "...", "task": { /* same shape as a task above */ }}

// 3. One per mission change. Its tasks arrive as task messages.
{"type": "mission", "mission": {"id": "...", "status": "completed", "result": "...",
  "error": null, "updated_at": "...", "completed_at": "..."}}
```

Messages are states, not deltas, so applying one twice or out of order is harmless: just replace
what you hold for that task or mission.

### Close codes

| Code | Meaning | What a client does |
|---|---|---|
| `1000` | The mission reached `completed`, `failed` or `cancelled` (or was already finished when you connected). Nothing more will change. | Stop. |
| `1013` | You fell too far behind and were cut off. | Reconnect: you get a new snapshot. |
| `4404` | No such mission. | Stop. |
| `1008` | The id was not a valid UUID. | Fix the request. |
| `1011` | The server's event feed is not running. | Retry later. |
| anything else | Network or server trouble. | Reconnect with a short, growing delay. |

Reconnecting is always safe: the first message is a snapshot of the current state.

### Try it

```bash
python -m scripts.try_stream "Compare PostgreSQL and MySQL for a small startup"
```
