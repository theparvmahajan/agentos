import json
from uuid import uuid4

import pytest

from core.events import CHANNEL, parse_notice


def payload(**overrides) -> str:
    body = {"kind": "task", "mission_id": str(uuid4()), "task_id": str(uuid4()), "status": "ready"}
    return json.dumps({**body, **overrides})


def test_a_task_notice_is_parsed():
    notice = parse_notice(payload())
    assert notice.kind == "task" and notice.status == "ready"
    assert notice.task_id is not None


def test_a_mission_notice_needs_no_task_id():
    body = json.dumps({"kind": "mission", "mission_id": str(uuid4()), "status": "running"})
    notice = parse_notice(body)
    assert notice.kind == "mission" and notice.task_id is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        "42",
        '{"kind": "task"}',
        payload(kind="agent"),
        payload(mission_id="not-a-uuid"),
        payload(status=None),
    ],
)
def test_anything_malformed_is_ignored_not_raised(text):
    assert parse_notice(text) is None


def test_the_channel_name_matches_the_trigger_in_the_migration():
    from pathlib import Path

    migration = Path("infrastructure/db/migrations/002_change_notifications.sql").read_text()
    assert f"pg_notify('{CHANNEL}'" in migration
