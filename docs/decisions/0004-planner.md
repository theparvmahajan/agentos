# 0004: Planner

Status: accepted

## Why validate LLM output in layers instead of trusting JSON mode?
LLM output is untrusted input. JSON mode only makes valid JSON *more
likely*. It doesn't guarantee the JSON matches our schema, and it says
nothing about whether the plan makes sense. A model can still return
malformed JSON, missing fields, an agent that doesn't exist, a dependency
on a task it never defined, or a cycle.

parse_plan checks from the outside in, and each layer assumes the
previous one passed:
1. JSON: can we parse it at all? (also handles ```fences``` and prose)
2. Schema: Pydantic checks shape, types, key format, and the 1–10 task limit.
3. Meaning: no duplicate keys, only registered agents, no self-dependency,
   no dependency on an undefined task.
4. Graph: topological sort proves there are no cycles.

Stopping at the first failure gives one specific error, such as
"task t1 uses unknown agent 'wizard'; valid agents: researcher".
That message is written so the model can act on it in the repair loop,
and so a human can debug a failed mission. Nothing invalid ever reaches
the database.

## How is the repair loop different from retrying a failed call, and why keep them separate?
They handle different failures:
- Repair loop: the call *succeeded* but the answer was invalid. We send
  the model its previous answer plus the exact error and ask once more
  (max_repairs = 1). This fixes bad output, and a fresh blind call
  wouldn't, because the model wouldn't know what it got wrong.
- Retry: the call itself *failed* (network down, timeout, rate limit)
  and raised LLMError. The planner deliberately doesn't catch it.

Retries for failed calls belong to the scheduler, which stores attempt
counts durably in Postgres (ADR 0002) and is the single owner of retry
policy (ADR 0003). If the planner also retried, attempts would multiply
and hidden calls would burn tokens. The repair loop is bounded and
small, so it can't explode. The test test_llm_errors_are_not_retried_here
locks this decision in.

## Why sort tasks topologically before storing them?
Two reasons, from one algorithm (Kahn's, in core/dag.py):
- Insert order: create_task writes dependency edges with foreign keys,
  so a task's dependencies must already exist before it's inserted.
  Topological order puts every dependency before the tasks that need it,
  so store_plan can insert in a single pass.
- Cycle detection: if the sort can't output every node, the leftovers
  are waiting on each other, so the plan has a cycle. SQL can't easily
  forbid multi-step cycles (ADR 0002 only blocks self-dependency), so
  this is where we catch them.

The module is separate from the planner because the M2 scheduler reuses
the same "which tasks just became ready?" idea.

## Why isn't a database connection held open while the LLM is called?
An LLM call can take 30 seconds or more. Holding a transaction open that
long ties up a connection from a limited pool and holds row locks, so
other requests wait or the pool runs dry under load.

So run_planner uses short, separate transactions:
1. Mark the mission planning and load the agents, then commit.
2. Call the LLM with no connection held.
3. Store all tasks, edges, and the mission → running change in one
   atomic transaction, so the database never contains half a plan.

If anything fails after step 1, a broad except marks the mission failed
with the reason and re-raises, so a mission can never be stuck in
planning.

Known gap: the pending check in step 1 isn't atomic, so two concurrent
callers could both plan the same mission. That's acceptable with a
single caller and will be fixed in M4 with a guarded UPDATE
(... WHERE status = 'pending').