"""The registry's gate with a policy and an audit trail, using the in-memory sink."""

from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel

from core.audit import AuditEntry, AuditUnavailable, MemoryAuditSink, fingerprint
from core.models import Agent
from core.policy import parse_policy
from core.tools import (
    ApprovalRequired,
    Tool,
    ToolContext,
    ToolDenied,
    ToolError,
    ToolInputError,
    ToolPermissionError,
    ToolRegistry,
    UnknownTool,
)

POLICY = parse_policy(
    """
version: 1
default: deny
capabilities:
  echo.use:   {effect: allow, risk: low, max_calls_per_task: 2}
  mail.send:  {effect: approval, risk: high}
  nuke.use:   {effect: deny, risk: critical}
agents:
  restricted: {deny: ['echo.*']}
"""
)


class In(BaseModel):
    text: str = "x"


def make_agent(name="agent-x", *caps) -> Agent:
    return Agent(id=uuid4(), name=name, capabilities=list(caps), created_at=datetime(2026, 1, 1))


def tool(name="echo", capability="echo.use", handler=None) -> Tool:
    return Tool(name, "d", capability, In, handler or (lambda a: a.text))


@pytest.fixture
def audit():
    return MemoryAuditSink()


@pytest.fixture
def reg(audit):
    r = ToolRegistry(policy=POLICY, audit=audit)
    r.register(tool())
    r.register(tool("mail", "mail.send"))
    r.register(tool("nuke", "nuke.use"))
    return r


def decisions(audit):
    return [(e.tool, e.decision, e.rule) for e in audit.entries]


# -- the order of the gates ---------------------------------------------------------------


def test_an_allowed_call_runs_and_is_audited(reg, audit):
    result = reg.call(make_agent("a", "echo.use"), "echo", {"text": "hi"})
    assert result.output == "hi"
    [e] = audit.entries
    assert (e.decision, e.tool, e.capability, e.agent, e.risk) == ("allow", "echo", "echo.use", "a", "low")
    assert audit.completions[audit.ids[0]].outcome == "ok"


def test_the_capability_gate_comes_first_and_is_audited(reg, audit):
    with pytest.raises(ToolPermissionError) as e:
        reg.call(make_agent("a"), "echo", {})
    assert not isinstance(e.value, ToolDenied)  # it is the capability gate, not the policy
    assert decisions(audit) == [("echo", "deny", "capability_not_held")]


def test_a_denied_capability_is_refused_even_though_the_agent_holds_it(reg, audit):
    with pytest.raises(ToolDenied) as e:
        reg.call(make_agent("a", "nuke.use"), "nuke", {})
    assert e.value.rule == "nuke.use"
    assert audit.entries[0].risk == "critical"


def test_approval_required_is_refused_for_now_and_says_so(reg, audit):
    with pytest.raises(ApprovalRequired) as e:
        reg.call(make_agent("a", "mail.send"), "mail", {})
    assert "approval" in str(e.value) and isinstance(e.value, ToolDenied)
    assert decisions(audit) == [("mail", "approval_required", "mail.send")]


def test_an_agent_rule_denies_a_held_capability(reg, audit):
    with pytest.raises(ToolDenied):
        reg.call(make_agent("restricted", "echo.use"), "echo", {})
    assert audit.entries[0].rule == "agent:restricted"


def test_the_policy_is_checked_before_the_arguments_so_no_schema_leaks(reg):
    with pytest.raises(ToolDenied):  # not ToolInputError, even though the args are garbage
        reg.call(make_agent("a", "nuke.use"), "nuke", {"text": 123, "junk": 1})


def test_an_unknown_tool_is_audited_and_lists_only_visible_tools(reg, audit):
    with pytest.raises(UnknownTool) as e:
        reg.call(make_agent("a", "echo.use"), "nope", {})
    assert "echo" in str(e.value) and "nuke" not in str(e.value) and "mail" not in str(e.value)
    assert decisions(audit) == [("nope", "deny", "unknown_tool")]


def test_invalid_arguments_are_not_an_audited_decision_and_cost_no_budget(reg, audit):
    agent = make_agent("a", "echo.use")
    for _ in range(5):
        with pytest.raises(ToolInputError):
            reg.call(agent, "echo", {"text": 123})
    assert audit.entries == []
    assert reg.call(agent, "echo", {}).output == "x"  # the budget is untouched


# -- visibility ---------------------------------------------------------------------------


def test_tools_the_policy_denies_are_not_offered_to_the_model(reg):
    names = {t.name for t in reg.tools_for(make_agent("a", "echo.use", "nuke.use", "mail.send"))}
    assert names == {"echo", "mail"}  # nuke is denied outright; mail may still be requested
    assert reg.tools_for(make_agent("restricted", "echo.use")) == []


def test_a_bare_registry_behaves_as_before():
    r = ToolRegistry()
    r.register(tool())
    assert [t.name for t in r.tools_for(make_agent("a", "echo.use"))] == ["echo"]
    assert r.call(make_agent("a", "echo.use"), "echo", {}).output == "x"


# -- budgets and context ------------------------------------------------------------------


def ctx(**kw):
    return ToolContext(task_id=uuid4(), mission_id=uuid4(), attempt=1, **kw)


def test_the_per_task_call_limit_is_enforced_and_audited(reg, audit):
    bound, agent = reg.bind(ctx()), make_agent("a", "echo.use")
    bound.call(agent, "echo", {})
    bound.call(agent, "echo", {})
    with pytest.raises(ToolDenied) as e:
        bound.call(agent, "echo", {})
    assert e.value.rule == "echo.use:max_calls_per_task"
    assert decisions(audit)[-1] == ("echo", "deny", "echo.use:max_calls_per_task")


def test_each_bound_view_has_its_own_budget(reg):
    agent = make_agent("a", "echo.use")
    first, second = reg.bind(ctx()), reg.bind(ctx())
    first.call(agent, "echo", {})
    first.call(agent, "echo", {})
    with pytest.raises(ToolDenied):
        first.call(agent, "echo", {})
    assert second.call(agent, "echo", {}).output == "x"  # a new attempt starts fresh


def test_refused_calls_do_not_consume_budget(reg):
    bound, agent = reg.bind(ctx()), make_agent("a", "echo.use")
    for _ in range(4):
        with pytest.raises(UnknownTool):
            bound.call(agent, "nope", {})
    assert bound.call(agent, "echo", {}).output == "x"


def test_the_bound_view_stamps_every_audit_row_with_its_task(reg, audit):
    c = ToolContext(task_id=uuid4(), mission_id=uuid4(), tenant_id=uuid4(), attempt=3)
    bound, agent = reg.bind(c), make_agent("a", "echo.use", "nuke.use")
    bound.call(agent, "echo", {})
    with pytest.raises(ToolDenied):
        bound.call(agent, "nuke", {})
    for e in audit.entries:
        assert (e.task_id, e.mission_id, e.tenant_id, e.attempt) == (
            c.task_id, c.mission_id, c.tenant_id, 3,
        )


def test_the_unbound_registry_audits_without_a_task(reg, audit):
    reg.call(make_agent("a", "echo.use"), "echo", {})
    assert audit.entries[0].task_id is None and audit.entries[0].mission_id is None


def test_a_tainted_context_blocks_deny_when_tainted_capabilities(audit):
    pol = parse_policy(
        "version: 1\ncapabilities:\n  mail.send: {effect: allow, deny_when_tainted: true}\n"
    )
    r = ToolRegistry(policy=pol, audit=audit)
    r.register(tool("mail", "mail.send"))
    agent = make_agent("a", "mail.send")
    assert r.bind(ctx()).call(agent, "mail", {}).output == "x"
    with pytest.raises(ToolDenied) as e:
        r.bind(ctx(tainted=True)).call(agent, "mail", {})
    assert e.value.rule == "mail.send:deny_when_tainted"


# -- the audit trail never holds values, and it fails closed --------------------------------


def test_argument_values_never_reach_the_audit(reg, audit):
    secret = "sk-live-SUPERSECRET-123"
    reg.call(make_agent("a", "echo.use"), "echo", {"text": secret})
    blob = repr(audit.entries)
    assert secret not in blob
    [e] = audit.entries
    assert e.arg_keys == ["text"] and e.arg_digest == fingerprint({"text": secret})[0]


def test_fingerprint_is_stable_and_order_independent():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1})[0] != fingerprint({"a": 2})[0]
    assert fingerprint("not a dict")[1] == []
    assert len(fingerprint({"a": 1})[0]) == 64


class DownSink(MemoryAuditSink):
    def record(self, entry: AuditEntry):
        raise AuditUnavailable("database is down")


def test_when_the_audit_cannot_be_written_the_tool_does_not_run():
    ran = []
    r = ToolRegistry(policy=POLICY, audit=DownSink())
    r.register(tool(handler=lambda a: ran.append(1) or "done"))
    with pytest.raises(AuditUnavailable) as e:
        r.call(make_agent("a", "echo.use"), "echo", {})
    assert ran == [] and e.value.retryable is True
    assert not isinstance(e.value, ToolError)  # so the agent loop cannot swallow it


def test_the_decision_is_written_before_the_tool_runs(audit):
    seen = []
    r = ToolRegistry(policy=POLICY, audit=audit)
    r.register(tool(handler=lambda a: seen.append(len(audit.entries)) or "ok"))
    r.call(make_agent("a", "echo.use"), "echo", {})
    assert seen == [1]


def test_a_tool_that_raises_is_audited_as_an_error(reg, audit):
    r = ToolRegistry(policy=POLICY, audit=audit)
    r.register(tool(handler=lambda a: 1 / 0))
    with pytest.raises(ToolError):
        r.call(make_agent("a", "echo.use"), "echo", {})
    assert audit.completions[audit.ids[0]].outcome == "error"
