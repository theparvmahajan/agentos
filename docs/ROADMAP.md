# AgentOS Roadmap

Rule: `main` is always runnable. Every milestone ends in something demoable.

| Milestone | Scope | Target | Status |
|---|---|---|---|
| M0 Setup | repo, compose, CI, health endpoint | Week 0 | Done |
| M1 Walking skeleton | models, LLM gateway, planner, 1 agent, sequential run | Weeks 1-2 | Done |
| M2 Orchestration | DAG, Redis Streams workers, 3 agents, parallelism, sandboxed code execution | Weeks 3-5 | Done (`m2`) |
| M3 Dashboard | WebSocket events, mission list and cancel, React Flow live graph, deploy (= v1) | Weeks 6-7 | In progress (backend #14 and dashboard #15 done; deploy and demo next) |
| M4 Reliability | checkpoints, retries, timeouts, idempotency | Weeks 8-10 | Planned |
| M5 Security | capabilities, policy engine, tool guardrails | Weeks 11-13 | Planned |
| M6 Human-in-the-loop | approvals, pause/resume | Weeks 14-15 | Planned |
| M7 Observability | metrics, traces, cost tracking, evaluation suite | Weeks 16-18 | Planned |
| M8 Sandbox + hardening | stronger isolation, circuit breaker, DLQ, load tests | Weeks 19-22 | Planned |
| M9 Stretch | pgvector memory, model routing, K8s | only if time remains | Planned |

Notes: the basic Docker code sandbox arrived early, in M2 (ADR 0011). M8 is now about going further
(gVisor/Firecracker-class isolation, load tests), not starting from nothing.
