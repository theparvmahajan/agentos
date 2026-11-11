"""The audit trail: a durable record of every decision the runtime makes about a tool call.

Two rules shape this module.

1. Values never reach the table. We store a SHA-256 of the arguments and the names of their
   fields, so a reviewer can see "run_python was called with {code}" and tell two identical
   calls apart, without the table ever holding a secret that an agent put in an argument.

2. The record of an allowed call is written BEFORE the tool runs, and fails closed. If the audit
   cannot be written, the tool does not run (AuditUnavailable, which is retryable: the task goes
   back for another attempt). The completion update afterwards is best effort: the call has
   already happened, and failing it now would make the worker retry a side effect.
"""

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

import psycopg

from core.db import get_conn

log = logging.getLogger(__name__)

DECISIONS = ("allow", "deny", "approval_required")


class AuditUnavailable(Exception):
    """The audit record could not be written, so the action was not allowed to happen."""

    retryable = True  # a database blip: the task attempt is retried, nothing was executed


def fingerprint(raw_args: object) -> tuple[str, list[str]]:
    """(sha256 of the canonical JSON, sorted top-level field names). Never the values."""
    try:
        canonical = json.dumps(raw_args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(type(raw_args))
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()
    keys = sorted(str(k) for k in raw_args) if isinstance(raw_args, dict) else []
    return digest, keys[:50]


@dataclass(frozen=True)
class AuditEntry:
    agent: str
    tool: str
    capability: str
    decision: str  # allow | deny | approval_required
    rule: str
    reason: str
    risk: str
    arg_digest: str
    arg_keys: list[str] = field(default_factory=list)
    tenant_id: UUID | None = None
    mission_id: UUID | None = None
    task_id: UUID | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class Completion:
    outcome: str  # ok | error
    latency_ms: int
    untrusted_output: bool = False
    injection_suspected: bool = False


class AuditSink(Protocol):
    def record(self, entry: AuditEntry) -> UUID: ...

    def complete(self, audit_id: UUID, completion: Completion) -> None: ...


class MemoryAuditSink:
    """For tests and for running without a database. Keeps everything in a list."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self.completions: dict[UUID, Completion] = {}
        self.ids: list[UUID] = []
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> UUID:
        with self._lock:
            audit_id = uuid4()
            self.entries.append(entry)
            self.ids.append(audit_id)
            return audit_id

    def complete(self, audit_id: UUID, completion: Completion) -> None:
        with self._lock:
            self.completions[audit_id] = completion


class PgAuditSink:
    """Writes to the tool_audit table, each write on its own short connection (no connection is
    ever held open while a tool runs) and committed at once."""

    def __init__(self, database_url: str | None = None):
        self._database_url = database_url

    def record(self, entry: AuditEntry) -> UUID:
        try:
            with get_conn(self._database_url) as conn:
                row = conn.execute(
                    """
                    INSERT INTO tool_audit
                        (tenant_id, mission_id, task_id, attempt, agent, tool, capability,
                         decision, rule, reason, risk, arg_digest, arg_keys)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        entry.tenant_id, entry.mission_id, entry.task_id, entry.attempt,
                        entry.agent, entry.tool, entry.capability, entry.decision, entry.rule,
                        entry.reason, entry.risk, entry.arg_digest, entry.arg_keys,
                    ),
                ).fetchone()
        except psycopg.Error as e:
            raise AuditUnavailable(f"audit record could not be written: {e}") from e
        return row["id"]

    def complete(self, audit_id: UUID, completion: Completion) -> None:
        try:
            with get_conn(self._database_url) as conn:
                conn.execute(
                    """
                    UPDATE tool_audit
                    SET outcome = %s, latency_ms = %s, untrusted_output = %s,
                        injection_suspected = %s
                    WHERE id = %s AND outcome IS NULL
                    """,
                    (
                        completion.outcome, completion.latency_ms, completion.untrusted_output,
                        completion.injection_suspected, audit_id,
                    ),
                )
        except psycopg.Error:
            # The call already happened; do not turn it into a failure. The row still shows
            # the decision, with no outcome, which is itself the evidence of an interrupted call.
            log.exception("audit %s: could not record the outcome", audit_id)
