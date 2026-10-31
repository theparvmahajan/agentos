-- Agents: the registry of what can do work
CREATE TABLE agents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    capabilities    TEXT[] NOT NULL DEFAULT '{}',
    model           TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 60 CHECK (timeout_seconds > 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Missions: one per user request
CREATE TABLE missions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID,
    objective    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','planning','running',
                                   'completed','failed','cancelled')),
    result       TEXT,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Tasks: nodes of the mission's graph
CREATE TABLE tasks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID,
    mission_id   UUID NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,
    agent_id     UUID NOT NULL REFERENCES agents(id),
    instruction  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','ready','running','completed',
                                   'failed','skipped','cancelled')),
    output       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    last_error   TEXT,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mission_id, key)
);
CREATE INDEX idx_tasks_mission ON tasks (mission_id);
CREATE INDEX idx_tasks_status  ON tasks (status);

-- Edges of the graph: "task_id waits for depends_on_task_id"
CREATE TABLE task_dependencies (
    task_id            UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);
CREATE INDEX idx_task_deps_reverse ON task_dependencies (depends_on_task_id);