-- A running task is held under a LEASE: "worker X is running attempt N and will renew this
-- until expires_at". If the worker dies the lease runs out and the reaper can recover the task.
--
-- Why a separate table and not columns on tasks: every UPDATE of tasks fires the notify trigger
-- (migration 002). A heartbeat every few seconds per running task would flood the dashboard's
-- live feed with messages that change nothing. This table has no trigger.
--
-- expires_at is always computed with the DATABASE's clock (now()), never a worker's, so machines
-- whose clocks disagree cannot make a live lease look dead or a dead one look alive.
CREATE TABLE IF NOT EXISTS task_leases (
    task_id    UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    owner      TEXT NOT NULL,
    attempt    INT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    renewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS task_leases_expires_idx ON task_leases (expires_at);
