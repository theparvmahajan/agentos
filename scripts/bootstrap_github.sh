#!/usr/bin/env bash
# Creates labels, milestones and starter issues for AgentOS.
# Prereqs: `gh auth login` done, and run from inside the cloned repo.
# Usage: bash scripts/bootstrap_github.sh
set -euo pipefail

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
echo "Bootstrapping $REPO"

# ---- labels ----
for l in "task:0e8a16" "bug:d73a4a" "learning:5319e7" "decision:fbca04" "backend:1d76db" "frontend:c5def5" "infra:bfd4f2" "security:b60205"; do
  name="${l%%:*}"; color="${l##*:}"
  gh label create "$name" --color "$color" --force >/dev/null
done

# ---- milestones ----
ms() { gh api "repos/$REPO/milestones" -f title="$1" -f description="$2" >/dev/null 2>&1 || true; }
ms "M0 Setup" "Repo, compose, CI, health endpoint"
ms "M1 Walking skeleton" "Models, LLM gateway, planner, one agent, sequential run"
ms "M2 Orchestration" "DAG, Redis Streams workers, 3 agents, parallelism"
ms "M3 Dashboard (v1)" "WebSocket events, live DAG, deploy"
ms "M4 Reliability" "Checkpoints, retries, timeouts, idempotency"
ms "M5 Security" "Capabilities, policy engine, tool guardrails"
ms "M6 Human-in-the-loop" "Approvals, pause/resume"
ms "M7 Observability" "Metrics, traces, cost tracking, eval suite"
ms "M8 Sandbox + hardening" "Docker sandbox, circuit breaker, DLQ, load tests"

# ---- issues ----
issue() { # title | milestone | labels | body
  gh issue create --title "$1" --milestone "$2" --label "$3" --body "$4" >/dev/null
  echo "  + $1"
}

M0="M0 Setup"
issue "Repo skeleton, compose, CI green" "$M0" "task,infra" "Done when: docker compose up works, /health returns 200, CI passes on main."

M1="M1 Walking skeleton"
issue "Design Postgres schema: missions, tasks, agents" "$M1" "task,backend,decision" "Include status enums, tenant_id (nullable), timestamps, retries. Write ADR for choices. Spec sections 8, 9, 11."
issue "Pydantic models + DB access layer" "$M1" "task,backend" "Mission, Task, Agent models. Create/read functions with tests."
issue "Model gateway: generate(prompt, task_type) behind a provider interface" "$M1" "task,backend" "One provider (Ollama or hosted). Token usage returned. Spec section 16."
issue "Planner: request -> validated JSON task list" "$M1" "task,backend" "Strict schema, retry once on malformed output with error appended."
issue "Tool registry + web search tool" "$M1" "task,backend" "Spec section 23."
issue "Researcher agent + sequential executor" "$M1" "task,backend" "Run tasks in order, persist outputs and status."
issue "POST /missions and GET /missions/{id}" "$M1" "task,backend" "Done when curl creates a mission and the result is stored."

M2="M2 Orchestration"
issue "Task DAG with depends_on + readiness logic (heavily unit-tested)" "$M2" "task,backend" "Cycle detection, ready-set computation. Spec section 10."
issue "Scheduler service pushing ready tasks to Redis Streams" "$M2" "task,backend" "Spec sections 13, 21."
issue "Worker process with consumer group, ack, result stream" "$M2" "task,backend" "Separate process, N replicas possible."
issue "Coder and Reviewer agents" "$M2" "task,backend" "Three agents total is enough."
issue "Parallel execution proof: timing test with 2 workers" "$M2" "task,learning" "Write up what you observed."

M3="M3 Dashboard (v1)"
issue "Event model + WebSocket stream per mission" "$M3" "task,backend" ""
issue "Next.js dashboard with React Flow live DAG" "$M3" "task,frontend" "Node colors by status, tokens, latency panel."
issue "Deploy v1, record demo gif, README with architecture diagram" "$M3" "task,infra" "This is the resume-ready milestone."

M4="M4 Reliability"
issue "Checkpoint persistence and resume after worker crash" "$M4" "task,backend" "Kill a worker mid-task; mission must recover. Spec section 20."
issue "Retries with backoff, per-task timeouts, idempotency keys" "$M4" "task,backend" "Spec sections 38, 39."
issue "Failure-injection tests + write-up" "$M4" "task,learning" "Document each failure found, like the rag-okf bug notes."

M5="M5 Security"
issue "Capability model + permission check on every tool call" "$M5" "task,security" "Spec section 25."
issue "Policy engine (YAML) with allow / approval / deny" "$M5" "task,security" "Spec section 26."
issue "Deterministic tool guardrail: block destructive SQL" "$M5" "task,security" "Parser-based, not LLM-based. Spec section 29."

echo "Done. Later milestones (M6-M8) get issues when you reach them."
