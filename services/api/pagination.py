"""Opaque cursors for the mission list.

A cursor names the last row of the previous page by (created_at, id). Paging by position in
the result (OFFSET) would skip or repeat rows whenever a mission is created between two requests;
paging by "everything older than this row" cannot.
"""

import base64
import binascii
from datetime import datetime
from uuid import UUID


class InvalidCursor(ValueError):
    pass


def encode_cursor(created_at: datetime, mission_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{mission_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, _, ident = raw.partition("|")
        created_at = datetime.fromisoformat(stamp)
        mission_id = UUID(ident)
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise InvalidCursor("invalid cursor") from e
    if created_at.tzinfo is None:
        raise InvalidCursor("invalid cursor")
    return created_at, mission_id
