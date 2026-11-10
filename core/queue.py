from dataclasses import dataclass
from uuid import UUID

import redis
from redis.backoff import NoBackoff
from redis.exceptions import ResponseError
from redis.retry import Retry

from core.config import settings

TASK_GROUP = "workers"
EVENT_GROUP = "schedulers"


@dataclass(frozen=True)
class Message:
    id: str
    fields: dict[str, str]


class Stream:
    """One Redis Stream, read through one consumer group.

    Delivery is at-least-once: a message stays in the group's pending list until it is
    acknowledged, so a consumer that dies mid-message does not lose it.
    """

    def __init__(self, client: redis.Redis, name: str, group: str):
        self.client = client
        self.name = name
        self.group = group

    def ensure_group(self) -> None:
        # id="0": a new group starts from the beginning, so nothing published earlier is missed.
        try:
            self.client.xgroup_create(self.name, self.group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):  # the group already exists: fine
                raise

    def publish(self, fields: dict[str, str]) -> str:
        return self.client.xadd(self.name, fields)

    def read(
        self,
        consumer: str,
        *,
        count: int = 1,
        block_ms: int | None = None,
        pending: bool = False,
    ) -> list[Message]:
        """Next messages for this consumer.

        pending=False: messages nobody in the group has received yet (waits up to block_ms;
        None means do not wait). pending=True: messages THIS consumer already received but
        never acknowledged, which is how a consumer retries after a failure or a restart.
        """
        response = self.client.xreadgroup(
            self.group,
            consumer,
            {self.name: "0" if pending else ">"},
            count=count,
            block=None if pending else block_ms,
        )
        messages: list[Message] = []
        for _stream, entries in response or []:
            messages += [Message(id=i, fields=f) for i, f in entries]
        return messages

    def ack(self, message_id: str) -> None:
        """Mark a message as handled and remove it. (One group per stream, so removal is safe.)"""
        pipe = self.client.pipeline()
        pipe.xack(self.name, self.group, message_id)
        pipe.xdel(self.name, message_id)
        pipe.execute()

    def pending_count(self) -> int:
        return self.client.xpending(self.name, self.group)["pending"]

    def length(self) -> int:
        return self.client.xlen(self.name)


@dataclass(frozen=True)
class Queues:
    tasks: Stream  # scheduler -> workers: "run this task"
    events: Stream  # workers (and the planner) -> scheduler: "something changed in this mission"

    def ensure_groups(self) -> None:
        self.tasks.ensure_group()
        self.events.ensure_group()

    def publish_task(self, mission_id: UUID, task_id: UUID) -> str:
        return self.tasks.publish({"mission_id": str(mission_id), "task_id": str(task_id)})

    def publish_event(self, mission_id: UUID, task_id: UUID | None = None) -> str:
        fields = {"mission_id": str(mission_id)}
        if task_id is not None:
            fields["task_id"] = str(task_id)
        return self.events.publish(fields)


def make_redis(url: str | None = None, *, socket_timeout: float | None = None) -> redis.Redis:
    """A Redis client that fails fast instead of waiting forever.

    - socket_timeout: a call that gets no answer raises after this long. Without it a network
      partition (no error, only silence) hangs the caller indefinitely.
    - no client-side retries: redis-py would otherwise retry a failed call several times with
      growing pauses (measured: one call into a black hole took 14 s with a 1 s timeout). Our loops
      already retry on their own schedule, and a hidden second retry layer only delays them.
    """
    return redis.Redis.from_url(
        url or settings.redis_url,
        decode_responses=True,
        socket_timeout=socket_timeout or settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
    )


def get_redis() -> redis.Redis:
    return make_redis()


def build_queues(client: redis.Redis | None = None, prefix: str | None = None) -> Queues:
    client = client or get_redis()
    prefix = prefix or settings.queue_prefix
    return Queues(
        tasks=Stream(client, f"{prefix}:tasks", TASK_GROUP),
        events=Stream(client, f"{prefix}:events", EVENT_GROUP),
    )