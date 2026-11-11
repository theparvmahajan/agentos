"""The tool policy engine: a deterministic decision on every tool call.

    PolicyRequest (who, which capability, how many calls so far, is the context tainted)
        -> Policy.evaluate()  ->  PolicyDecision (effect, risk, reason, which rule)

`evaluate` is a pure function of the policy and the request. No I/O, no clock, no randomness and
no LLM: the same inputs always give the same answer, which is what makes it testable and what
lets a person audit it. The policy is loaded from YAML at startup and validated strictly: a
typo in a rule stops the process from starting rather than silently weakening a rule.

Combining rules (see config/policy.yaml): every rule whose pattern matches contributes; the most
restrictive effect wins and the highest risk is reported. Nothing depends on rule order.
"""

import fnmatch
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class Effect(str, Enum):
    ALLOW = "allow"
    APPROVAL = "approval"  # needs a human; until milestone M6 exists this is refused
    DENY = "deny"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Higher number = more restrictive / riskier. Used to combine rules without caring about order.
_EFFECT_RANK = {Effect.ALLOW: 0, Effect.APPROVAL: 1, Effect.DENY: 2}
_RISK_RANK = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}

_PATTERN = re.compile(r"^[a-z0-9_.*-]+$")


class PolicyError(Exception):
    """The policy file is missing or invalid. The message says what to fix."""


class CapabilityRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: Effect
    risk: Risk = Risk.MEDIUM
    # Tool calls one task attempt may make under this capability. None = no limit.
    max_calls_per_task: int | None = Field(default=None, ge=1)
    # Refuse once this task has read untrusted content (a web page, database text, or the output
    # of a task that did). The runtime's answer to prompt injection: not "ask the model to ignore
    # it" but "after untrusted input, this action is simply not available".
    deny_when_tainted: bool = False


class AgentRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deny: list[str] = Field(default_factory=list)  # capability patterns taken away

    @field_validator("deny")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not _PATTERN.fullmatch(pattern):
                raise ValueError(f"bad capability pattern {pattern!r}")
        return value


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    default: Effect = Effect.DENY
    capabilities: dict[str, CapabilityRule] = Field(default_factory=dict)
    agents: dict[str, AgentRule] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported policy version {value} (this build reads version 1)")
        return value

    @field_validator("capabilities")
    @classmethod
    def _patterns(cls, value: dict[str, CapabilityRule]) -> dict[str, CapabilityRule]:
        for pattern in value:
            if not _PATTERN.fullmatch(pattern):
                raise ValueError(f"bad capability pattern {pattern!r} (use a-z 0-9 _ . - and *)")
        return value


@dataclass(frozen=True)
class PolicyRequest:
    agent: str
    capability: str
    calls_made: int = 0  # calls under this capability already made in this task attempt
    tainted: bool = False  # has this task read untrusted content?


@dataclass(frozen=True)
class PolicyDecision:
    effect: Effect
    risk: Risk
    reason: str
    rule: str  # which rule decided: a capability pattern, "agent:<name>", "default", or a limit

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


def _matches(pattern: str, capability: str) -> bool:
    return fnmatch.fnmatchcase(capability, pattern)


class Policy:
    def __init__(self, spec: PolicyFile):
        self._spec = spec

    @property
    def default(self) -> Effect:
        return self._spec.default

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        """The decision for one tool call. Pure; see the module docstring."""
        spec = self._spec

        # 1. Per-agent restrictions: a match is an unconditional deny.
        agent_rule = spec.agents.get(request.agent)
        if agent_rule is not None:
            for pattern in sorted(agent_rule.deny):
                if _matches(pattern, request.capability):
                    return PolicyDecision(
                        Effect.DENY,
                        self._risk_of(request.capability),
                        f"policy removes '{request.capability}' from agent '{request.agent}'",
                        f"agent:{request.agent}",
                    )

        # 2. Every capability rule that matches contributes. sorted() only makes the *reported*
        #    rule name stable; the effect and risk below are the same in any order.
        matching = [(p, r) for p, r in sorted(spec.capabilities.items()) if _matches(p, request.capability)]
        if not matching:
            return PolicyDecision(
                spec.default,
                Risk.MEDIUM,
                f"no policy rule covers capability '{request.capability}' (default: {spec.default.value})",
                "default",
            )

        effect = max((r.effect for _, r in matching), key=_EFFECT_RANK.__getitem__)
        risk = max((r.risk for _, r in matching), key=_RISK_RANK.__getitem__)
        deciding = next(p for p, r in matching if r.effect is effect)

        if effect is Effect.DENY:
            return PolicyDecision(
                effect, risk, f"policy denies '{request.capability}'", deciding
            )

        # Conditions below can only tighten an allow/approval into a deny.
        limits = [r.max_calls_per_task for _, r in matching if r.max_calls_per_task is not None]
        if limits and request.calls_made >= min(limits):
            return PolicyDecision(
                Effect.DENY,
                risk,
                f"'{request.capability}' is limited to {min(limits)} calls per task",
                f"{deciding}:max_calls_per_task",
            )
        if request.tainted and any(r.deny_when_tainted for _, r in matching):
            return PolicyDecision(
                Effect.DENY,
                risk,
                f"'{request.capability}' is not available after this task read untrusted content",
                f"{deciding}:deny_when_tainted",
            )

        if effect is Effect.APPROVAL:
            return PolicyDecision(
                effect, risk, f"'{request.capability}' requires human approval", deciding
            )
        return PolicyDecision(Effect.ALLOW, risk, f"policy allows '{request.capability}'", deciding)

    def is_hidden_from(self, agent: str, capability: str) -> bool:
        """True when this agent can NEVER use the capability (so it is not even offered to the
        model). Only static denials count; limits and taint are decided at call time."""
        return self.evaluate(PolicyRequest(agent, capability)).effect is Effect.DENY

    def _risk_of(self, capability: str) -> Risk:
        risks = [r.risk for p, r in self._spec.capabilities.items() if _matches(p, capability)]
        return max(risks, key=_RISK_RANK.__getitem__) if risks else Risk.MEDIUM


def parse_policy(text: str, source: str = "policy") -> Policy:
    """Parse and validate YAML text. Raises PolicyError; never returns a half-valid policy."""
    try:
        raw = yaml.safe_load(text)  # safe_load: a policy file can never construct Python objects
    except yaml.YAMLError as e:
        raise PolicyError(f"{source}: not valid YAML ({e})") from e
    if not isinstance(raw, dict):
        raise PolicyError(f"{source}: expected a mapping at the top level")
    try:
        return Policy(PolicyFile.model_validate(raw))
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise PolicyError(f"{source}: {problems}") from e


def load_policy(path: str | Path) -> Policy:
    """Read a policy file. A missing file is an error, never an empty (permissive) policy."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise PolicyError(f"cannot read policy file {p}: {e}") from e
    return parse_policy(text, str(p))
