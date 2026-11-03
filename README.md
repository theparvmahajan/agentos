# AgentOS

A stateful multi-agent execution runtime: it plans a natural-language objective into a task graph,
schedules the tasks across worker processes over a message queue, enforces permissions on every
tool call, and runs agent-written code in a locked-down container.

> The model decides what it wants to do; the runtime decides whether it can.

**Status:** M0-M2 complete: planning, DAG scheduling, distributed workers, three agents
(researcher, coder, reviewer), sandboxed code execution. Next: M3 (live dashboard).
See [docs/ROADMAP.md](docs/ROADMAP.md).

## Architecture

```mermaid
flowchart LR
    Client -->|POST /missions| API
    API -->|1. one LLM call: the plan| PG[(Postgres)]
    API -->|2. event: mission planned| EV[[Redis stream: events]]
    EV --> S[Scheduler]
    S -->|pending to ready: guarded UPDATE| PG
    S -->|task id| TK[[Redis stream: tasks]]
    TK --> W1[Worker 1]
    TK --> W2[Worker 2]
    TK --> WN[Worker N]
    W1 & W2 & WN -->|claim, run, save result| PG
    W1 & W2 & WN -->|event: task finished| EV
    W1 & W2 & WN --> T["Tools via registry: web_search, run_python (Docker)"]
```

- **Postgres is the source of truth.** Redis streams carry only ids ("look at this task").
- **Effectively-once execution on at-least-once delivery.** Every status change is a guarded
  `UPDATE`, so when a message is delivered twice, only one worker wins the claim.
- **Workers scale by running more of them.** Parallelism is measured, not claimed:
  see [docs/BENCHMARKS.md](docs/BENCHMARKS.md) (generate it with `make bench`).
- **The registry, not the prompt, decides what an agent may do.** Agents hold capabilities;
  a tool call without the matching capability is refused.
- **Agent-written code runs in a container** (no network, read-only filesystem, no capabilities,
  non-root, memory/CPU/process limits) and the tool fails closed if Docker is unavailable.

## Quick start

```bash
cp .env.example .env              # then fill in TAVILY_API_KEY (and LLM settings) locally
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make up                           # postgres + redis
make migrate && make seed         # schema and the three agents
make sandbox                      # one-time: pull the code-execution image (needs Docker)
make test
```

Run it (four terminals):

```bash
make serve                        # API on http://localhost:8000  (docs at /docs)
make scheduler
WORKER_NAME=w1 make worker        # a second one: WORKER_NAME=w2 make worker
python -m scripts.try_api "Compare PostgreSQL and MySQL for a small startup"
```

## Safety notes

- Never commit `.env`. Secrets live there only, never as defaults in code.
- `CODE_EXEC_BACKEND=docker` is the default. `CODE_EXEC_BACKEND=local` runs agent code on your own
  machine with no isolation and exists only as an explicit opt-in.
- Containers share the host kernel: strong isolation for a dev machine, not for hostile strangers.

## What works / what's planned
See the roadmap. Updated at the end of every milestone.
