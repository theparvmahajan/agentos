import logging
import time
from collections import Counter

from pydantic import ValidationError

from core.audit import AuditEntry, AuditSink, Completion, fingerprint
from core.models import Agent
from core.policy import Effect, Policy, PolicyDecision, PolicyRequest
from core.tools.base import (
    ApprovalRequired,
    Tool,
    ToolContext,
    ToolDenied,
    ToolError,
    ToolInputError,
    ToolPermissionError,
    ToolResult,
    UnknownTool,
)

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 8000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[output truncated: {len(text)} characters in total]"


class ToolRegistry:
    """The only way an agent reaches a tool.

    Every call passes the same gate, in this order, and each step can only say no:

        tool exists -> agent holds the capability -> policy allows it -> arguments validate
        -> (guardrails) -> the decision is written to the audit trail -> handler runs

    `policy` and `audit` are optional so small tests can use a bare registry, but the processes
    that run real agents are built with both (build_runtime_registry).

    A registry is shared by everything in a process. For one task attempt the worker calls
    `bind(context)`, which returns a view of the same tools that remembers WHO is calling
    (for the audit) and WHAT this task has done so far (call counts, for the policy).
    """

    def __init__(self, *, policy: Policy | None = None, audit: AuditSink | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._policy = policy
        self._audit = audit
        self._context: ToolContext | None = None
        self._calls: Counter[str] = Counter()  # calls made so far, per capability

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def bind(self, context: ToolContext) -> "ToolRegistry":
        """The same tools, policy and audit, for one task attempt. Counts start from zero."""
        bound = ToolRegistry(policy=self._policy, audit=self._audit)
        bound._tools = self._tools  # shared: tools are only added at startup
        bound._context = context
        return bound

    def tools_for(self, agent: Agent) -> list[Tool]:
        """The tools this agent may EVER use: it holds the capability and the policy does not
        deny it outright. Hiding the rest saves the model from planning around a closed door."""
        return [
            t
            for t in self._tools.values()
            if t.capability in agent.capabilities
            and not (self._policy and self._policy.is_hidden_from(agent.name, t.capability))
        ]

    def describe_for(self, agent: Agent) -> list[dict]:
        return [t.schema() for t in self.tools_for(agent)]

    # -- auditing ------------------------------------------------------------------------

    def _record(
        self, agent: Agent, tool_name: str, tool: Tool | None, raw_args: object, *,
        decision: str, rule: str, reason: str, risk: str,
    ) -> object | None:
        if self._audit is None:
            return None
        digest, keys = fingerprint(raw_args)
        ctx = self._context
        return self._audit.record(
            AuditEntry(
                agent=agent.name,
                tool=tool_name[:100],
                capability=tool.capability if tool else "",
                decision=decision,
                rule=rule,
                reason=reason,
                risk=risk,
                arg_digest=digest,
                arg_keys=keys,
                tenant_id=ctx.tenant_id if ctx else None,
                mission_id=ctx.mission_id if ctx else None,
                task_id=ctx.task_id if ctx else None,
                attempt=ctx.attempt if ctx else None,
            )
        )

    def _refuse(
        self, agent: Agent, tool_name: str, tool: Tool | None, raw_args: object,
        error: ToolError, *, decision: str, rule: str, risk: str,
    ) -> ToolError:
        """Write the refusal to the audit trail, log it, and hand back the error to raise."""
        self._record(
            agent, tool_name, tool, raw_args,
            decision=decision, rule=rule, reason=str(error), risk=risk,
        )
        log.warning("tool call refused: agent=%s tool=%s rule=%s", agent.name, tool_name[:100], rule)
        return error

    # -- the call ------------------------------------------------------------------------

    def call(self, agent: Agent, tool_name: str, raw_args: dict) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            # Only list tools this agent may use, so the error doesn't leak the rest.
            allowed = ", ".join(t.name for t in self.tools_for(agent)) or "none"
            raise self._refuse(
                agent, tool_name, None, raw_args,
                UnknownTool(f"unknown tool '{tool_name}'; available tools: {allowed}"),
                decision="deny", rule="unknown_tool", risk="low",
            )

        if tool.capability not in agent.capabilities:
            raise self._refuse(
                agent, tool_name, tool, raw_args,
                ToolPermissionError(
                    f"agent '{agent.name}' lacks capability '{tool.capability}' "
                    f"required by tool '{tool.name}'"
                ),
                decision="deny", rule="capability_not_held", risk="medium",
            )

        if self._policy is not None:
            verdict = self._policy.evaluate(
                PolicyRequest(
                    agent=agent.name,
                    capability=tool.capability,
                    calls_made=self._calls[tool.capability],
                    tainted=self._tainted,
                )
            )
            if verdict.effect is not Effect.ALLOW:
                raise self._refuse_by_policy(agent, tool_name, tool, raw_args, verdict)

        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            raise ToolInputError(f"invalid arguments for {tool.name}: {problems}") from e

        # The decision is recorded BEFORE the tool runs. If that fails, the tool does not run.
        audit_id = self._record(
            agent, tool_name, tool, raw_args,
            decision="allow", rule="policy" if self._policy else "capability",
            reason="allowed", risk=self._risk(agent, tool),
        )
        self._calls[tool.capability] += 1

        start = time.perf_counter()
        try:
            output = tool.handler(args)
        except ToolError:
            self._finish(audit_id, "error", start)
            raise
        except Exception as e:
            self._finish(audit_id, "error", start)
            raise ToolError(f"tool {tool.name} crashed: {e!r}") from e
        latency_ms = self._finish(audit_id, "ok", start)

        return ToolResult(tool=tool.name, output=_truncate(output), latency_ms=latency_ms)

    # -- helpers -------------------------------------------------------------------------

    @property
    def _tainted(self) -> bool:
        return bool(self._context and self._context.tainted)

    def _risk(self, agent: Agent, tool: Tool) -> str:
        if self._policy is None:
            return "medium"
        return self._policy.evaluate(PolicyRequest(agent.name, tool.capability)).risk.value

    def _refuse_by_policy(
        self, agent: Agent, tool_name: str, tool: Tool, raw_args: object, verdict: PolicyDecision
    ) -> ToolError:
        if verdict.effect is Effect.APPROVAL:
            error: ToolError = ApprovalRequired(
                f"{verdict.reason}; no approver is available, so the call was not made",
                rule=verdict.rule,
            )
            decision = "approval_required"
        else:
            error = ToolDenied(verdict.reason, rule=verdict.rule)
            decision = "deny"
        return self._refuse(
            agent, tool_name, tool, raw_args, error,
            decision=decision, rule=verdict.rule, risk=verdict.risk.value,
        )

    def _finish(self, audit_id: object | None, outcome: str, start: float) -> int:
        latency_ms = int((time.perf_counter() - start) * 1000)
        if self._audit is not None and audit_id is not None:
            self._audit.complete(audit_id, Completion(outcome=outcome, latency_ms=latency_ms))
        return latency_ms
