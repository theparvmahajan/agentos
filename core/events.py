"""Listening for change notifications from Postgres (see migration 002).

The database announces every committed change to a task or a mission as a tiny message:
which kind, which mission, which task, the new status. `PgListener` receives them on a
background thread and hands each one to a callback.

Why a thread and not asyncio: psycopg's async mode does not work on Windows' default event
loop, and a plain blocking connection in its own thread works everywhere.

Notifications are fire-and-forget: if the listener is disconnected, changes made meanwhile are
not replayed. After every reconnect `on_reconnect` is called so consumers can resynchronize
from the database, which stays the source of truth.
"""

import json
import logging
import threading
from collections.abc import Callable
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import BaseModel, ValidationError

from core.config import settings

log = logging.getLogger(__name__)

CHANNEL = "agentos_events"


class ChangeNotice(BaseModel):
    """What the trigger sends. Deliberately small: see the note in migration 002."""

    kind: Literal["task", "mission"]
    mission_id: UUID
    task_id: UUID | None = None
    status: str


def parse_notice(payload: str) -> ChangeNotice | None:
    """None for anything that is not a well-formed notice (never raises)."""
    try:
        return ChangeNotice.model_validate(json.loads(payload))
    except (ValueError, ValidationError):
        log.warning("ignoring malformed change notification: %.200s", payload)
        return None


class PgListener(threading.Thread):
    """LISTEN on the change channel; call `on_notice` for each notification.

    Reconnects by itself (and calls `on_reconnect` once the new LISTEN is in place).
    Stop it with `stop()`, which returns promptly because the wait loop wakes every 0.5 s.
    """

    def __init__(
        self,
        on_notice: Callable[[ChangeNotice], None],
        *,
        on_reconnect: Callable[[], None] | None = None,
        database_url: str | None = None,
        channel: str = CHANNEL,
        application_name: str = "agentos-listener",
        reconnect_delay: float = 1.0,
    ):
        super().__init__(daemon=True, name="pg-listener")
        self._on_notice = on_notice
        self._on_reconnect = on_reconnect
        self._database_url = database_url
        self._channel = channel
        self._application_name = application_name
        self._reconnect_delay = reconnect_delay
        self._stop_requested = threading.Event()
        self.listening = threading.Event()  # set while a LISTEN is active

    def stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        connection_lost = False
        while not self._stop_requested.is_set():
            try:
                with psycopg.connect(
                    self._database_url or settings.database_url,
                    autocommit=True,
                    application_name=self._application_name,
                ) as conn:
                    conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
                    self.listening.set()
                    if connection_lost and self._on_reconnect:
                        self._safely(self._on_reconnect)
                    connection_lost = False
                    while not self._stop_requested.is_set():
                        for notify in conn.notifies(timeout=0.5):
                            notice = parse_notice(notify.payload)
                            if notice is not None:
                                self._safely(self._on_notice, notice)
                            if self._stop_requested.is_set():
                                break
            except (psycopg.Error, OSError) as e:
                connection_lost = True
                log.warning("change listener disconnected (%s); retrying", e)
            finally:
                self.listening.clear()
            self._stop_requested.wait(self._reconnect_delay)

    @staticmethod
    def _safely(callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception:
            log.exception("change listener callback failed")
