"""The live feed of one mission: WS /api/v1/missions/{id}/stream

How a change reaches a browser:

    a worker commits a status change in Postgres
      -> the trigger NOTIFYs (ids and status only)             core/events.py
      -> PgListener (a thread) receives it
      -> it reads the CURRENT row and builds a full message    build_message
      -> the EventHub hands it to every connection watching that mission
      -> the WebSocket sends it as JSON

A connection first receives a SNAPSHOT of the whole mission, then one message per change.
The hub subscribes BEFORE the snapshot is read, so no change can fall between the two; at worst
a change is reported twice, which is harmless because every message is a complete state.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool

from core import repository as repo
from core.db import get_conn
from core.events import ChangeNotice, PgListener
from core.state import MISSION_TERMINAL
from services.api.deps import DatabaseUrl
from services.api.schemas import (
    MissionMessage,
    MissionOut,
    SnapshotMessage,
    TaskMessage,
)
from services.api.views import to_mission_out, to_mission_patch, to_task_out

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/missions", tags=["stream"])

QUEUE_SIZE = 200  # messages one slow client may fall behind before it is cut off
CLOSE_NOT_FOUND = 4404
CLOSE_TOO_SLOW = 1013  # "try again later": the client should reconnect and get a new snapshot
TERMINAL_VALUES = {s.value for s in MISSION_TERMINAL}

# Markers placed in a subscriber's queue next to ordinary messages.
RESYNC = "resync"  # the database listener reconnected: send a fresh snapshot
OVERFLOW = "overflow"  # this client could not keep up


class Subscription:
    def __init__(self, mission_id: UUID, maxsize: int):
        self.mission_id = mission_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)


class EventHub:
    """Fan-out from the single database listener to many WebSocket connections.

    `subscribe`, `unsubscribe`, `publish` and `resync_all` run on the event loop thread.
    `is_watched` may be called from any thread (the listener asks it before doing any work).
    """

    def __init__(self, queue_size: int = QUEUE_SIZE):
        self._queue_size = queue_size
        self._subs: dict[UUID, set[Subscription]] = {}
        self._lock = threading.Lock()

    def subscribe(self, mission_id: UUID) -> Subscription:
        sub = Subscription(mission_id, self._queue_size)
        with self._lock:
            self._subs.setdefault(mission_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            watchers = self._subs.get(sub.mission_id)
            if watchers is not None:
                watchers.discard(sub)
                if not watchers:
                    del self._subs[sub.mission_id]

    def is_watched(self, mission_id: UUID) -> bool:
        with self._lock:
            return mission_id in self._subs

    def count(self) -> int:
        with self._lock:
            return sum(len(w) for w in self._subs.values())

    def publish(self, mission_id: UUID, message: dict) -> None:
        with self._lock:
            watchers = list(self._subs.get(mission_id, ()))
        for sub in watchers:
            self._put(sub, message)

    def resync_all(self) -> None:
        with self._lock:
            everyone = [s for watchers in self._subs.values() for s in watchers]
        for sub in everyone:
            self._put(sub, RESYNC)

    @staticmethod
    def _put(sub: Subscription, item) -> None:
        try:
            sub.queue.put_nowait(item)
        except asyncio.QueueFull:
            # A client that cannot keep up must not grow memory without bound, and must not
            # slow down the others. Drop its backlog and tell it to reconnect.
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.queue.put_nowait(OVERFLOW)


# ---------- reading the database (synchronous: run in a thread) ----------

def load_snapshot(database_url: str | None, mission_id: UUID) -> MissionOut | None:
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is None:
            return None
        tasks = repo.get_tasks(conn, mission_id)
        agent_names = {a.id: a.name for a in repo.list_agents(conn)}
    return to_mission_out(mission, tasks, agent_names)


def build_message(notice: ChangeNotice, database_url: str | None = None) -> dict | None:
    """Turn a tiny notice into a full message by reading the CURRENT state of the row.
    None if the row is gone (for example the mission was deleted)."""
    with get_conn(database_url) as conn:
        if notice.kind == "mission":
            mission = repo.get_mission(conn, notice.mission_id)
            if mission is None:
                return None
            return MissionMessage(mission=to_mission_patch(mission)).model_dump(mode="json")
        tasks = repo.get_tasks(conn, notice.mission_id)
        agent_names = {a.id: a.name for a in repo.list_agents(conn)}
    task = next((t for t in tasks if t.id == notice.task_id), None)
    if task is None:
        return None
    return TaskMessage(
        mission_id=notice.mission_id, task=to_task_out(task, tasks, agent_names)
    ).model_dump(mode="json")


def make_listener(
    hub: EventHub, loop: asyncio.AbstractEventLoop, database_url: str | None = None
) -> PgListener:
    """Connect the database listener (a thread) to the hub (the event loop)."""

    def call_on_loop(fn: Callable, *args) -> None:
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop is closed: the server is shutting down
            pass

    def on_notice(notice: ChangeNotice) -> None:
        if not hub.is_watched(notice.mission_id):
            return  # nobody is looking: do not even read the database
        message = build_message(notice, database_url)
        if message is not None:
            call_on_loop(hub.publish, notice.mission_id, message)

    return PgListener(
        on_notice,
        on_reconnect=lambda: call_on_loop(hub.resync_all),
        database_url=database_url,
    )


# ---------- the endpoint ----------

def _is_final(message: dict) -> bool:
    return message.get("type") == "mission" and message["mission"]["status"] in TERMINAL_VALUES


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    """Returns when the client goes away. Anything the client sends is ignored."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:  # already closed
        pass


@router.websocket("/{mission_id}/stream")
async def stream_mission(websocket: WebSocket, mission_id: UUID, database_url: DatabaseUrl):
    await websocket.accept()
    hub: EventHub | None = getattr(websocket.app.state, "hub", None)
    if hub is None:  # the app was started without its lifespan: there is no feed
        await _close(websocket, 1011, "event feed not running")
        return

    sub = hub.subscribe(mission_id)  # BEFORE the snapshot: nothing can fall in between
    watcher: asyncio.Task | None = None
    try:
        async def send_snapshot() -> MissionOut | None:
            snapshot = await run_in_threadpool(load_snapshot, database_url, mission_id)
            if snapshot is not None:
                await websocket.send_json(SnapshotMessage(mission=snapshot).model_dump(mode="json"))
            return snapshot

        snapshot = await send_snapshot()
        if snapshot is None:
            await _close(websocket, CLOSE_NOT_FOUND, "mission not found")
            return
        if snapshot.status.value in TERMINAL_VALUES:
            await _close(websocket, 1000)  # nothing more will ever change
            return

        watcher = asyncio.create_task(_wait_for_disconnect(websocket))
        while True:
            getter = asyncio.create_task(sub.queue.get())
            done, _ = await asyncio.wait({getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                getter.cancel()
                return
            item = getter.result()
            if item == OVERFLOW:
                await _close(websocket, CLOSE_TOO_SLOW, "too slow: reconnect for a new snapshot")
                return
            if item == RESYNC:
                snapshot = await send_snapshot()
                if snapshot is None or snapshot.status.value in TERMINAL_VALUES:
                    await _close(websocket, 1000)
                    return
                continue
            await websocket.send_json(item)
            if _is_final(item):
                await _close(websocket, 1000)
                return
    finally:
        hub.unsubscribe(sub)
        if watcher is not None:
            watcher.cancel()
