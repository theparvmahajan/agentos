-- The audit trail of tool calls: one row per decision the runtime made about a tool call, kept
-- whether the call was allowed, denied or stopped by a guardrail. A new table (not a new column)
-- for the same reason as migration 004.
--
-- Deliberate choices:
--   * NO foreign keys. An audit record must outlive the thing it describes: deleting a mission
--     (which cascades to its tasks) must not erase the evidence of what its agents tried to do.
--   * Arguments are never stored, only a SHA-256 of them and the NAMES of the fields. A tool
--     argument can carry a secret or private data; the digest still proves "the same call twice".
--   * `decision` is what the policy said, `outcome` is what happened afterwards (only for
--     allowed calls, filled in when the tool returns).
-- Every statement is safe to run twice.
CREATE TABLE IF NOT EXISTS tool_audit (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id           UUID,
    mission_id          UUID,
    task_id             UUID,
    attempt             INTEGER,
    agent               TEXT NOT NULL,
    tool                TEXT NOT NULL,
    capability          TEXT NOT NULL,
    decision            TEXT NOT NULL CHECK (decision IN ('allow', 'deny', 'approval_required')),
    rule                TEXT NOT NULL,
    reason              TEXT NOT NULL,
    risk                TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high', 'critical')),
    arg_digest          TEXT NOT NULL,
    arg_keys            TEXT[] NOT NULL DEFAULT '{}',
    outcome             TEXT CHECK (outcome IN ('ok', 'error')),
    latency_ms          INTEGER,
    -- Set when an allowed call returned content from outside the system (a web page, database
    -- text). It is what marks a task as "tainted" for the policy rule deny_when_tainted.
    untrusted_output    BOOLEAN NOT NULL DEFAULT FALSE,
    -- Set when that content matched a known prompt-injection phrase: a signal for humans.
    injection_suspected BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS tool_audit_mission_idx ON tool_audit (mission_id, created_at);
CREATE INDEX IF NOT EXISTS tool_audit_task_idx ON tool_audit (task_id);
CREATE INDEX IF NOT EXISTS tool_audit_denied_idx ON tool_audit (created_at) WHERE decision <> 'allow';
