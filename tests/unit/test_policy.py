"""The policy engine is pure, so these tests need no database and no mocks."""

import itertools
import random
from pathlib import Path

import pytest

from core.policy import (
    Effect,
    Policy,
    PolicyError,
    PolicyRequest,
    Risk,
    load_policy,
    parse_policy,
)

SHIPPED = Path("config/policy.yaml")


def policy(body: str, default: str = "deny") -> Policy:
    return parse_policy(f"version: 1\ndefault: {default}\n{body}")


def decide(p: Policy, capability: str, agent: str = "a", **kw):
    return p.evaluate(PolicyRequest(agent, capability, **kw))


# -- the basic verdicts ------------------------------------------------------------------


def test_a_listed_capability_is_allowed():
    p = policy("capabilities:\n  web.search: {effect: allow, risk: low}")
    d = decide(p, "web.search")
    assert d.effect is Effect.ALLOW and d.allowed and d.risk is Risk.LOW and d.rule == "web.search"


def test_an_unlisted_capability_gets_the_default_which_is_deny():
    d = decide(policy("capabilities: {}"), "mystery.thing")
    assert d.effect is Effect.DENY and d.rule == "default" and "no policy rule" in d.reason


def test_the_default_can_be_changed_but_the_file_must_say_so():
    assert decide(policy("capabilities: {}", default="allow"), "x.y").effect is Effect.ALLOW


def test_approval_is_a_distinct_verdict():
    p = policy("capabilities:\n  email.send: {effect: approval, risk: high}")
    d = decide(p, "email.send")
    assert d.effect is Effect.APPROVAL and not d.allowed and d.risk is Risk.HIGH


def test_explicit_deny():
    p = policy("capabilities:\n  database.delete: {effect: deny, risk: critical}")
    d = decide(p, "database.delete")
    assert d.effect is Effect.DENY and d.risk is Risk.CRITICAL


# -- wildcards and combining rules --------------------------------------------------------


def test_wildcards_match_a_family_of_capabilities():
    p = policy("capabilities:\n  'database.*': {effect: deny}")
    assert decide(p, "database.read").effect is Effect.DENY
    assert decide(p, "database.write").effect is Effect.DENY
    assert decide(p, "databases.read").effect is Effect.DENY  # default, still a deny
    assert decide(policy("capabilities:\n  'database.*': {effect: allow}"), "databasex").rule == "default"


def test_the_most_restrictive_matching_rule_wins():
    p = policy(
        "capabilities:\n"
        "  'database.*': {effect: allow, risk: low}\n"
        "  database.write: {effect: approval, risk: high}\n"
        "  'database.d*': {effect: deny, risk: critical}\n"
    )
    assert decide(p, "database.read").effect is Effect.ALLOW
    assert decide(p, "database.write").effect is Effect.APPROVAL
    assert decide(p, "database.delete").effect is Effect.DENY
    # the risk reported is the highest among everything that matched
    assert decide(p, "database.write").risk is Risk.HIGH
    assert decide(p, "database.delete").risk is Risk.CRITICAL


def test_a_broad_deny_cannot_be_overridden_by_a_narrow_allow():
    p = policy(
        "capabilities:\n  '*': {effect: deny}\n  web.search: {effect: allow}\n"
    )
    assert decide(p, "web.search").effect is Effect.DENY


# -- agent restrictions -----------------------------------------------------------------


def test_an_agent_rule_takes_a_capability_away():
    p = policy(
        "capabilities:\n  web.search: {effect: allow}\nagents:\n  coder: {deny: [web.search]}"
    )
    assert decide(p, "web.search", agent="researcher").effect is Effect.ALLOW
    d = decide(p, "web.search", agent="coder")
    assert d.effect is Effect.DENY and d.rule == "agent:coder"


def test_an_agent_rule_cannot_grant_anything():
    p = policy("capabilities: {}\nagents:\n  coder: {deny: []}")
    assert decide(p, "web.search", agent="coder").effect is Effect.DENY


def test_agent_deny_accepts_wildcards():
    p = policy(
        "capabilities:\n  web.search: {effect: allow}\n  web.read: {effect: allow}\n"
        "agents:\n  coder: {deny: ['web.*']}"
    )
    assert decide(p, "web.read", agent="coder").effect is Effect.DENY


# -- limits and taint --------------------------------------------------------------------


def test_the_call_limit_applies_from_the_nth_call():
    p = policy("capabilities:\n  web.search: {effect: allow, max_calls_per_task: 3}")
    assert decide(p, "web.search", calls_made=2).effect is Effect.ALLOW
    d = decide(p, "web.search", calls_made=3)
    assert d.effect is Effect.DENY and d.rule == "web.search:max_calls_per_task"
    assert "3 calls per task" in d.reason


def test_the_smallest_matching_limit_wins():
    p = policy(
        "capabilities:\n"
        "  'web.*': {effect: allow, max_calls_per_task: 10}\n"
        "  web.search: {effect: allow, max_calls_per_task: 2}\n"
    )
    assert decide(p, "web.search", calls_made=2).effect is Effect.DENY


def test_a_tainted_task_cannot_use_a_deny_when_tainted_capability():
    p = policy("capabilities:\n  email.send: {effect: allow, deny_when_tainted: true}")
    assert decide(p, "email.send", tainted=False).effect is Effect.ALLOW
    d = decide(p, "email.send", tainted=True)
    assert d.effect is Effect.DENY and d.rule == "email.send:deny_when_tainted"


def test_taint_does_not_affect_other_capabilities():
    p = policy(
        "capabilities:\n  web.search: {effect: allow}\n  email.send: {effect: allow, deny_when_tainted: true}"
    )
    assert decide(p, "web.search", tainted=True).effect is Effect.ALLOW


def test_taint_turns_even_an_approval_into_a_denial():
    p = policy("capabilities:\n  email.send: {effect: approval, deny_when_tainted: true}")
    assert decide(p, "email.send", tainted=True).effect is Effect.DENY


def test_hidden_only_for_static_denials():
    p = policy(
        "capabilities:\n  a.b: {effect: allow, max_calls_per_task: 1}\n  c.d: {effect: deny}\n"
        "  e.f: {effect: approval}"
    )
    assert not p.is_hidden_from("x", "a.b")  # a limit is decided at call time
    assert p.is_hidden_from("x", "c.d")
    assert not p.is_hidden_from("x", "e.f")  # can be requested


# -- the property: nothing depends on the order of the rules ------------------------------


def _spec(entries) -> str:
    lines = ["capabilities:"]
    for pattern, effect, risk, limit in entries:
        extra = f", max_calls_per_task: {limit}" if limit else ""
        lines.append(f"  '{pattern}': {{effect: {effect}, risk: {risk}{extra}}}")
    return "\n".join(lines)


def test_rule_order_never_changes_a_decision():
    entries = [
        ("db.*", "allow", "low", None),
        ("db.write", "approval", "high", 5),
        ("db.d*", "deny", "critical", None),
        ("*.read", "allow", "medium", 2),
        ("web.*", "allow", "low", 10),
    ]
    probes = ["db.read", "db.write", "db.delete", "web.search", "x.read", "other"]
    baseline = policy(_spec(entries))
    expected = {
        (c, n): (d := decide(baseline, c, calls_made=n)).effect and (d.effect, d.risk, d.allowed)
        for c in probes
        for n in (0, 2, 5)
    }
    for ordering in itertools.permutations(entries):  # all 120 orders
        p = policy(_spec(ordering))
        for (c, n), want in expected.items():
            d = decide(p, c, calls_made=n)
            assert (d.effect, d.risk, d.allowed) == want, (ordering, c, n)


def test_random_policies_are_order_independent_and_never_less_strict_than_any_rule():
    rng = random.Random(7)
    effects = ["allow", "approval", "deny"]
    risks = ["low", "medium", "high", "critical"]
    patterns = ["a.*", "a.b", "*.b", "*", "c.d", "a.*b"]
    rank = {"allow": 0, "approval": 1, "deny": 2}
    for _ in range(60):
        entries = [
            (pat, rng.choice(effects), rng.choice(risks), None)
            for pat in rng.sample(patterns, rng.randint(1, len(patterns)))
        ]
        shuffled = entries[:]
        rng.shuffle(shuffled)
        p1, p2 = policy(_spec(entries)), policy(_spec(shuffled))
        for cap in ["a.b", "a.x", "c.d", "z.b", "q"]:
            d1, d2 = decide(p1, cap), decide(p2, cap)
            assert (d1.effect, d1.risk) == (d2.effect, d2.risk)
            from fnmatch import fnmatchcase

            matched = [e for e in entries if fnmatchcase(cap, e[0])]
            if matched:  # restrictive-wins: never more permissive than any matching rule
                assert rank[d1.effect.value] >= max(rank[m[1]] for m in matched)


# -- loading is strict ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("- just\n- a list", "mapping"),
        ("version: 2\n", "unsupported policy version"),
        ("default: deny\n", "version"),
        ("version: 1\ndefault: maybe\n", "default"),
        ("version: 1\ncapabilities:\n  x.y: {effect: permit}\n", "effect"),
        ("version: 1\ncapabilities:\n  x.y: {effect: allow, risks: low}\n", "risks"),
        ("version: 1\ncapabilities:\n  'X.Y': {effect: allow}\n", "bad capability pattern"),
        ("version: 1\ncapabilities:\n  x.y: {effect: allow, max_calls_per_task: 0}\n", "max_calls"),
        ("version: 1\nextra_section: {}\n", "extra_section"),
        ("version: 1\nagents:\n  a: {deny: ['Bad Pattern']}\n", "bad capability pattern"),
        ("version: 1\ncapabilities:\n  'a.?': {effect: allow}\n", "bad capability pattern"),
        ("version: 1\ncapabilities:\n  'a.[bc]': {effect: allow}\n", "bad capability pattern"),
        ("version: [", "not valid YAML"),
    ],
)
def test_invalid_policies_are_rejected_with_a_useful_message(text, fragment):
    with pytest.raises(PolicyError) as e:
        parse_policy(text, "test.yaml")
    assert fragment in str(e.value) and "test.yaml" in str(e.value)


def test_yaml_cannot_construct_python_objects_or_run_code(tmp_path):
    marker = tmp_path / "pwned"
    payload = f"version: 1\ndefault: !!python/object/apply:os.mkdir ['{marker}']\n"
    with pytest.raises(PolicyError) as e:
        parse_policy(payload)
    assert "not valid YAML" in str(e.value)  # refused by the loader itself...
    assert not marker.exists()  # ...and, the point of the test, nothing was executed


def test_a_missing_policy_file_is_an_error_never_an_empty_policy(tmp_path):
    with pytest.raises(PolicyError) as e:
        load_policy(tmp_path / "nope.yaml")
    assert "cannot read policy file" in str(e.value)


# -- the policy that ships in the repo -----------------------------------------------------


def test_the_shipped_policy_is_valid_and_fails_closed():
    p = load_policy(SHIPPED)
    assert p.default is Effect.DENY
    assert decide(p, "totally.new.capability").effect is Effect.DENY


def test_the_shipped_policy_allows_what_the_agents_need():
    p = load_policy(SHIPPED)
    for cap in ("web.search", "code.execute", "database.read"):
        assert decide(p, cap).effect is Effect.ALLOW, cap


def test_the_shipped_policy_blocks_the_dangerous_capabilities():
    p = load_policy(SHIPPED)
    assert decide(p, "database.delete").effect is Effect.DENY
    assert decide(p, "shell.execute").effect is Effect.DENY
    assert decide(p, "email.send").effect is Effect.APPROVAL
    assert decide(p, "database.write").effect is Effect.APPROVAL
    # and an injected page cannot talk the agent into either one
    assert decide(p, "email.send", tainted=True).effect is Effect.DENY
    assert decide(p, "database.write", tainted=True).effect is Effect.DENY
