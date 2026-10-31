# 0003: LLM gateway

Status: accepted

## Why a provider interface instead of calling one API directly?
All LLM calls go through one interface (for example `complete(messages,
task_type) -> LLMResponse`), with one adapter per provider (OpenAI,
a local model, a fake for tests).

- Swappable providers: changing models or vendors means writing a new
  adapter, not editing every agent that calls an LLM.
- Testability: unit tests use a fake provider that returns canned
  responses. Tests are fast, free, deterministic, and need no API key or
  network.
- One place for cross-cutting concerns: logging, token counting, timeouts,
  and cost tracking happen once in the gateway instead of being copied
  into every caller.
- No vendor lock-in: the rest of the codebase never imports a provider SDK
  directly.

## Why does every response carry token counts and latency?
LLM calls are the slowest and most expensive part of the system, so they
must be measurable from day one.

- Cost: input/output tokens multiplied by the model's price gives the cost
  per call, per task, and per mission. Without token counts, I can't
  answer "how much did this mission cost?" or enforce a budget.
- Performance: latency shows which calls or models slow a mission down.
- Comparison: with the same numbers from every provider, I can compare
  models on cost and speed for the same kind of work.

Putting these fields on the standard response object means every provider
must report them, and no caller can forget to record them.

## Why does the gateway not retry?
Retries are owned by the scheduler, using the durable retry state on the
task row (attempts, max_attempts, last_error, see ADR 0002).

- One retry policy: if the gateway retried 3 times and the scheduler
  retried 3 times, one failure could become 9 calls. Retries multiply
  and become hard to reason about.
- Durability: gateway retries live in memory and are lost if the worker
  crashes. Task-level retries survive crashes.
- Visibility and cost control: every attempt is recorded in Postgres, so
  hidden retries can't silently burn tokens.
- Separation of concerns: the gateway does one thing. It makes a call
  and reports success or a clear error. Deciding whether to try again is
  a scheduling decision.

## What is task_type for?
Callers describe what kind of work they need (for example planning,
execution, summarizing) instead of naming a specific model.

- Routing: the gateway maps each task_type to a model. Hard reasoning
  like planning can use a strong model, while simple work like
  summarizing uses a cheaper, faster one. Changing that mapping is a
  config change, not a code change.
- Cost and metrics breakdown: token counts and latency are recorded per
  task_type, so I can see which kind of work drives cost.
- Future tuning: per-type settings (temperature, max tokens, fallback
  model) can be added without touching callers.