# 0007: Missions API

Status: accepted

## Why does POST return 202 with a Location header instead of waiting for the result?
A mission (planning plus executing every task) can take minutes. Holding
the HTTP request open that long would hit client and proxy timeouts, tie
up a server worker per mission, and lose the result if the connection
dropped.

So POST /missions validates the objective, stores the mission, and
returns immediately:
- 202 Accepted means "request received, work not finished." 200 would
  imply a finished result, and 201 a fully created resource.
- The Location header (/missions/<id>) tells the client where to check.
  The client polls GET /missions/<id> until the status is completed or
  failed.

This splits the API into a fast "submit" call and a cheap "read status"
call, which is the standard pattern for long-running jobs.

## Why must the mission be committed before the background job is scheduled?
The background job opens its own database connection. Postgres only
shows a row to other connections after the transaction that created it
commits. If the job were scheduled inside the transaction, it could run
before the commit and fail with "mission not found."

So the endpoint creates the mission in a with get_conn(...) block, which
commits on exit, and only then schedules process_mission. The job then
reads committed state, like the short separate transactions in
run_planner (ADR 0004).

## Why does the background job record failures instead of raising them?
In a normal request, an exception becomes a 500 response that someone
sees. In a background job, nobody is waiting to catch it. An uncaught
exception would only show up in server logs, and the mission would stay
in planning or running forever, so a polling client would wait
indefinitely.

process_mission therefore catches everything, logs it, and marks the
mission failed with the reason. The client sees the failure through the
normal GET /missions/<id> call. This is the same "a mission can't get
stuck" rule as run_planner (ADR 0004). The only remaining way to get
stuck is the process dying mid-mission, which is the crash-recovery gap
(ADR 0006, fixed in M4).

## Why are dependencies exposed as task keys, and why are tasks sorted by dependency order in the response?
The response is a public contract, separate from the database schema:
- Task keys ("t1", "t2") are what the planner produced and what humans
  read. Internal UUIDs mean nothing to a client and would force it to
  cross-reference IDs to understand the plan. Keys make the response
  readable on its own.
- Internal fields like tenant_id aren't exposed. Hiding them lets the
  schema change without breaking clients and avoids leaking internals.

Tasks are sorted with topological_order because database order is
incidental: same-transaction rows come back sorted by key, so a task
could appear before the tasks it depends on (the a_summary / z_research
problem from ADR 0006). Sorting by edges means the response always reads
in plan order: dependencies first.

## What are the limits of FastAPI BackgroundTasks, and what replaces them in M2?
BackgroundTasks runs jobs in the API process's thread pool after the
response is sent. That's simple, but:
- Not durable: if the server restarts or crashes, in-flight missions
  are lost and stay in running. Saving a file under make run (--reload)
  does the same, which is why make serve exists.
- Shared resources: long missions occupy threads in the same pool that
  serves requests, so a few slow missions can starve the API.
- No scaling or control: you can't add workers, limit concurrency, or
  see a queue backlog separately from the web server.

M2 replaces this with a queue and worker processes. POST only enqueues
the mission; separate workers pick up jobs and run them. The API stays
fast, workers can be scaled or restarted on their own, and combined
with M4's recovery, interrupted missions can be resumed instead of lost.

## What is missing for exposing this API to anyone other than yourself?
Today it's safe only because uvicorn binds to 127.0.0.1. Before anyone
else can reach it, it needs:
- Authentication: there is none. Anyone who can reach the API can
  spend my LLM and Tavily credits and read every mission.
- Authorization and tenant isolation: users must only see their own
  missions. tenant_id exists in the schema but isn't enforced in
  queries yet.
- Rate limits and cost budgets: per-user limits on requests, missions,
  and tokens, so one user (or a loop) can't run up a large bill. Token
  counts aren't persisted until M7.
- HTTPS: credentials and results must not travel in plain text, usually
  via a reverse proxy with TLS.
- Durable execution: the M2 queue and M4 retries and recovery, so
  missions survive restarts.
- Abuse and safety controls: objectives are untrusted input that can
  steer agents (prompt injection). M5's policies, risk levels, approvals,
  and audit logs limit what a manipulated agent can do.
- Operational basics: list endpoints with pagination, cancellation,
  structured logs and metrics, and CORS rules if a browser frontend
  calls it.

Until then: keep it on localhost, no tunnels, no --host 0.0.0.0.