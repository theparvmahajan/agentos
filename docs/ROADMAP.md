# AgentOS Roadmap

Rule: `main` is always runnable. Every milestone ends in something demoable.

| Milestone | Scope | Target |
|---|---|---|
| M0 Setup | repo, compose, CI, health endpoint | Week 0 |
| M1 Walking skeleton | models, LLM gateway, planner, 1 agent, sequential run | Weeks 1-2 |
| M2 Orchestration | DAG, Redis Streams workers, 3 agents, parallelism | Weeks 3-5 |
| M3 Dashboard | WebSocket events, React Flow live graph, deploy (= v1) | Weeks 6-7 |
| M4 Reliability | checkpoints, retries, timeouts, idempotency | Weeks 8-10 |
| M5 Security | capabilities, policy engine, tool guardrails | Weeks 11-13 |
| M6 Human-in-the-loop | approvals, pause/resume | Weeks 14-15 |
| M7 Observability | metrics, traces, cost tracking, evaluation suite | Weeks 16-18 |
| M8 Sandbox + hardening | Docker sandbox, circuit breaker, DLQ, load tests | Weeks 19-22 |
| M9 Stretch | pgvector memory, model routing, K8s | only if time remains |
