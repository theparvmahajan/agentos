-- Tell listeners (the API's WebSocket feed) whenever a task or mission changes.
--
-- NOTIFY is transactional: the notification is delivered only if the transaction COMMITS,
-- so a client can never be told about a change that was rolled back.
--
-- The payload must stay tiny: ids and a status only. Postgres rejects a payload over
-- 8000 bytes and that error would abort the transaction doing the write. Task output and
-- other large fields are therefore NEVER put in the payload; listeners read the row instead.
--
-- This file is safe to run more than once (for example if it was also pasted into psql by hand):
-- the function is replaced, and a trigger is created only if it is missing. Checking for the
-- trigger first also avoids DROP TRIGGER, which would take a much stronger lock on the table.
CREATE OR REPLACE FUNCTION notify_change() RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'tasks' THEN
        PERFORM pg_notify('agentos_events', json_build_object(
            'kind', 'task', 'mission_id', NEW.mission_id, 'task_id', NEW.id, 'status', NEW.status
        )::text);
    ELSE
        PERFORM pg_notify('agentos_events', json_build_object(
            'kind', 'mission', 'mission_id', NEW.id, 'status', NEW.status
        )::text);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'tasks_notify' AND tgrelid = 'tasks'::regclass
    ) THEN
        CREATE TRIGGER tasks_notify
            AFTER INSERT OR UPDATE ON tasks
            FOR EACH ROW EXECUTE FUNCTION notify_change();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'missions_notify' AND tgrelid = 'missions'::regclass
    ) THEN
        CREATE TRIGGER missions_notify
            AFTER INSERT OR UPDATE ON missions
            FOR EACH ROW EXECUTE FUNCTION notify_change();
    END IF;
END
$$;
