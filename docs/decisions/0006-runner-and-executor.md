# 0006: Runners and the sequential executor

Status: accepted

## Why is the Runner interface defined in core/ and implemented in agents/?
core/runner.py defines what "something that does a task" looks like:
a callable that takes (agent, instruction, dependency outputs) and
returns an AgentResult. agents/researcher.py implements it. The
executor looks up runners[agent.name] and only knows the interface.

- One-way dependency: agents/ imports core/, never the reverse. The
  executor stays small and doesn't change when agents change.
- Extensible: Coder and Reviewer (M2) are new runners registered by
  name. No executor edits.
- Testable: executor tests use fake runners that succeed or raise on
  demand, with no LLM involved.
- Clear split: the Agent row in Postgres is the registry entry (name,
  capabilities, timeout). The runner is the code behind it. The name
  links them.

This is the same pattern as the LLM provider (ADR 0003) and the search
provider (ADR 0005): depend on an interface, plug in implementations.

## Why does the researcher reply with JSON actions in a loop, and what are the trade-offs versus native function calling?
A research task usually needs several steps: search, read the result,
maybe search again, then answer. So the researcher runs a loop
(the ReAct pattern). Each turn the model returns exactly one JSON
object:
- {"action": "tool", "tool": ..., "args": ...}: our code runs the tool
  through the registry and appends the result to the scratchpad.
- {"action": "final", "answer": ...}: the task is done.
The model decides; our code acts and feeds the result back.

The gateway is stateless (one prompt in, one response out), so each
step rebuilds the full prompt from the instruction, dependency outputs,
and scratchpad.

Why JSON instead of native function calling:
- Portable: works with every provider and small local models through
  the existing json_mode. Native tool APIs differ per vendor, and many
  local models don't support them.
- Keeps the gateway simple: no provider-specific tool formats.
- Every tool call still goes through the registry gate (ADR 0005).

Trade-offs:
- We parse and validate the reply ourselves (malformed JSON, unknown
  actions), which native APIs partly do for us.
- Native function calling is often more reliable on large models.
- The prompt grows each step, so budgets are required: at most 6 LLM
  calls per task, tools forbidden on the last step (forcing an answer),
  tool results clipped to 4,000 characters and dependency outputs to
  6,000.

## Why are tool errors fed back to the model but LLM errors are not?
The rule: send an error to whoever can fix it.

- Tool errors (bad arguments, permission denied, search down) are
  problems the model can act on. It can fix its arguments, try a
  different query, or answer without searching and say so. So they go
  back as an observation ("ERROR: ...") and the loop continues. The
  registry's errors are written for this (ADR 0005).
- LLMError means the model itself is unreachable. There's no model to
  show the error to, so it propagates and the task fails.

Retrying a failed LLM call would be a hidden retry. Retry policy belongs
to the scheduler, using durable attempt counts in Postgres (ADRs 0002,
0003). This mirrors the planner's split between repairing bad output and
retrying failed calls (ADR 0004).

## Why does the executor derive its order from dependency edges instead of trusting the order rows come back from the database?
Database order is incidental, not meaningful. Tasks created in one
transaction share a created_at timestamp, so get_tasks falls back to
sorting by key. A task named a_summary that depends on z_research comes
back first and would run before its input exists.

The dependency edges are the real source of truth, so the executor runs
topological_order (core/dag.py, reused from ADR 0004) on them. A
regression test with exactly that a_summary / z_research case stops
anyone from "simplifying" back to row order.

General rule: never depend on the order rows happen to come back in.
Derive order from what the data means.

## What happens to dependents of a failed task, and to independent branches? Why?
- Dependents are marked skipped, transitively. If t1 fails, t2 (which
  needs t1) and t3 (which needs t2) are skipped. They can't produce a
  correct result without their inputs, and running them would waste
  tokens on guesses.
- Independent branches keep running. A task that doesn't depend on the
  failure still does useful work.
- skipped is different from failed (ADR 0002): failed means it tried
  and errored; skipped means it never had a chance. That distinction
  shows where the real problem is.
- The mission ends failed, and its error lists which tasks failed and
  which were skipped (for example "failed: t1; skipped: t2, t3").
  If all tasks complete, the mission completes and its result is the
  output of the final task(s).

## What can go wrong today that the later milestones fix? (retries, timeouts, crash recovery, parallelism)
This version is deliberately simple. Known gaps:

- No retries (M4): one transient failure, like a timeout or rate
  limit, fails the task permanently and skips everything downstream.
  M4 uses the attempts / max_attempts / last_error columns, plus
  backoff (next_retry_at).
- No timeouts (M4): timeout_seconds is stored but not enforced. A hung
  tool or LLM call can block the whole mission indefinitely.
- No crash recovery (M4): the executor runs in one process. If it dies
  mid-mission, tasks stay in running and the mission never finishes.
  M4 adds recovery that detects and resumes or fails stuck work.
- No parallelism (M2): tasks run one at a time, even independent ones
  the planner arranged to run in parallel. Missions take as long as the
  sum of all tasks instead of the longest chain.
- Token counts not saved (M7): AgentResult carries them, but they
  aren't persisted, so per-mission cost isn't queryable yet.
- Concurrent execution of one mission isn't guarded: like the planner's
  pending check (ADR 0004), two callers could run the same mission.
  Fixed with guarded status updates in M4.