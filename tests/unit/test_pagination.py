import base64
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from services.api.pagination import InvalidCursor, decode_cursor, encode_cursor


def test_a_cursor_round_trips_exactly():
    created, ident = datetime(2026, 11, 4, 12, 30, 15, 123456, tzinfo=UTC), uuid4()
    assert decode_cursor(encode_cursor(created, ident)) == (created, ident)


def test_it_keeps_the_timezone_offset():
    created = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    decoded, _ = decode_cursor(encode_cursor(created, uuid4()))
    assert decoded == created and decoded.utcoffset() == timedelta(hours=5, minutes=30)


def test_a_cursor_is_url_safe():
    cursor = encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), uuid4())
    assert all(c.isalnum() or c in "-_" for c in cursor)


def raw(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "!!!",
        "AAAA",
        raw("garbage"),
        raw("2026-01-01T00:00:00+00:00|not-a-uuid"),
        raw(f"not-a-date|{uuid4()}"),
        raw(f"2026-01-01T00:00:00|{uuid4()}"),  # no timezone: ambiguous, so refused
        raw("2026-01-01T00:00:00+00:00"),  # no id
    ],
)
def test_a_bad_cursor_is_rejected(cursor):
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor)
