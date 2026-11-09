-- Retries with backoff, a sweeper for "ready but never queued", and idempotent mission creation.
--
-- Both features live in NEW tables instead of new columns on tasks/missions, for two reasons:
--   * ALTER TABLE needs an exclusive lock that waits behind any session holding the table open
--     (even an idle transaction left by a database tool). CREATE TABLE does not, which is the rule
--     scripts/migrate.py and tests/integration/test_migrate_safety.py protect.
--   * Every UPDATE of tasks fires the notify trigger (migration 002). Dispatch bookkeeping changes
--     often and means nothing to the dashboard; a separate table has no trigger.
-- Every statement is safe to run twice.

-- When may a READY task run, and when was a queue message last published for it.
--   run_after   : NULL (or no row) means "now". A retry sets now() + a backoff delay, so waiting is
--                 stored in the database, not in a sleeping process that a restart would forget.
--   enqueued_at : NULL (or no row) means "no message is known to exist". The sweeper publishes READY
--                 tasks that are due and have no recent message, which repairs every window where a
--                 message could have been lost (a crash between "mark ready" and "publish").
CREATE TABLE IF NOT EXISTS task_dispatch (
    task_id     UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    run_after   TIMESTAMPTZ,
    enqueued_at TIMESTAMPTZ
);

-- Makes POST /missions safe to repeat. One row per (scope, key); scope is the tenant (an all-zero
-- id while there is a single tenant). The hash lets us refuse the same key used for a different
-- request. The primary key is what decides a race between two identical requests.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope        UUID NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    mission_id   UUID NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
