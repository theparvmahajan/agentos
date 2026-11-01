from core.queue import Message


def test_a_published_message_can_be_read_and_acknowledged(queues):
    queues.tasks.publish({"task_id": "t1", "mission_id": "m1"})

    [message] = queues.tasks.read("worker-1", count=10)

    assert isinstance(message, Message)
    assert message.fields == {"task_id": "t1", "mission_id": "m1"}
    assert queues.tasks.pending_count() == 1  # delivered but not acknowledged yet

    queues.tasks.ack(message.id)
    assert queues.tasks.pending_count() == 0
    assert queues.tasks.length() == 0  # and removed from the stream


def test_a_message_is_delivered_to_only_one_consumer_in_a_group(queues):
    for i in range(10):
        queues.tasks.publish({"n": str(i)})

    got_a, got_b = [], []
    for _ in range(10):  # the two workers alternate, as if running side by side
        got_a += queues.tasks.read("worker-a", count=1)
        got_b += queues.tasks.read("worker-b", count=1)

    seen = [m.fields["n"] for m in got_a + got_b]
    assert sorted(seen, key=int) == [str(i) for i in range(10)]  # all delivered, none twice
    assert got_a and got_b  # and the work really was shared


def test_an_unacknowledged_message_can_be_read_again_by_the_same_consumer(queues):
    queues.tasks.publish({"task_id": "t1"})
    [first] = queues.tasks.read("worker-1")
    assert queues.tasks.read("worker-1") == []  # nothing new

    [again] = queues.tasks.read("worker-1", pending=True)  # what a restarted worker does
    assert again.id == first.id

    queues.tasks.ack(again.id)
    assert queues.tasks.read("worker-1", pending=True) == []


def test_pending_messages_belong_to_the_consumer_that_received_them(queues):
    queues.tasks.publish({"task_id": "t1"})
    queues.tasks.read("worker-1")
    assert queues.tasks.read("worker-2", pending=True) == []


def test_reading_an_empty_stream_returns_nothing(queues):
    assert queues.tasks.read("worker-1") == []
    assert queues.tasks.read("worker-1", block_ms=50) == []


def test_messages_published_before_the_group_existed_are_not_lost(redis_client):
    from uuid import uuid4

    from core.queue import build_queues

    q = build_queues(redis_client, prefix=f"agentos-test-{uuid4().hex[:8]}")
    try:
        q.tasks.publish({"task_id": "early"})  # no group yet
        q.tasks.ensure_group()
        assert [m.fields["task_id"] for m in q.tasks.read("w")] == ["early"]
    finally:
        redis_client.delete(q.tasks.name, q.events.name)


def test_ensure_group_can_be_called_repeatedly(queues):
    queues.ensure_groups()
    queues.ensure_groups()


def test_publish_helpers_write_the_expected_fields(queues):
    from uuid import uuid4

    mission_id, task_id = uuid4(), uuid4()
    queues.publish_task(mission_id, task_id)
    queues.publish_event(mission_id)
    queues.publish_event(mission_id, task_id)

    [task] = queues.tasks.read("w")
    assert task.fields == {"mission_id": str(mission_id), "task_id": str(task_id)}
    events = queues.events.read("s", count=10)
    assert events[0].fields == {"mission_id": str(mission_id)}
    assert events[1].fields == {"mission_id": str(mission_id), "task_id": str(task_id)}