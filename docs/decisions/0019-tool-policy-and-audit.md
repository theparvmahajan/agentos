# 0019: The tool policy engine and the audit trail

Status: accepted · Issue: #20 · Milestone: M5

## What we built (fill in the "why" in your own words)

Every tool call now passes: tool exists -> agent holds the capability -> **policy allows it** ->
arguments validate -> **decision written to `tool_audit`** -> handler runs.
The policy is `config/policy.yaml`, evaluated by a pure function (`core/policy.py`).
The worker gives each task attempt a bound view of the registry (`ToolRegistry.bind`) so every
audit row names its task, mission and attempt.

## Questions to answer

1. The policy is keyed by *capability*, not by tool name. What does that buy us, and what would
   break if two tools shared a capability but needed different rules?
2. When several rules match we take the most restrictive one. Why is "order of the rules never
   matters" a safety property and not just a convenience? (Think about someone editing the file.)
3. `default: deny` means a new capability does nothing until someone allows it. What is the cost
   of that, and who pays it?
4. Audit rows store a SHA-256 of the arguments and the field names, never the values. What do we
   lose by that? What would storing values risk?
5. The audit record is written *before* the tool runs and a failure stops the tool, but the
   outcome update afterwards is best effort. Why is each half the right way round?
6. `approval` is refused for now, because M6 does not exist yet. Why refuse instead of allow?

## Honest limits (do not delete)

- A policy decides *whether* a call may happen, not whether its arguments are safe. That is the job
  of guardrails (next issue) and of the sandbox.
- The audit table is append-mostly by convention: the application never deletes from it, but a
  database administrator can. It is a record for debugging and review, not tamper-proof evidence.
- Call limits are per task *attempt*: a retry gets a fresh budget.
- Anything that can edit `config/policy.yaml` can change what agents may do. Protect it like code.
