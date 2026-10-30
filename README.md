# AgentOS

A stateful multi-agent execution runtime: plans missions into task graphs, schedules them
across worker processes over a message queue, enforces permissions on every tool call,
and exposes everything through a live dashboard.

> The model decides what it wants to do; the runtime decides whether it can.

**Status:** Milestone 1 (walking skeleton) in progress. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Quick start
```bash
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make up        # postgres + redis
make test
make run       # http://localhost:8000/health
```

## Architecture
_Diagram goes here after M2._

## What works / what's planned
See the roadmap. Updated at the end of every milestone.
