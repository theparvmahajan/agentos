import threading
from uuid import uuid4

from services.api.stream import OVERFLOW, RESYNC, EventHub, _is_final


def drain(sub) -> list:
    items = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    return items


def test_a_message_reaches_only_the_subscribers_of_that_mission():
    hub, a, b = EventHub(), uuid4(), uuid4()
    watcher_a, other_a, watcher_b = hub.subscribe(a), hub.subscribe(a), hub.subscribe(b)

    hub.publish(a, {"n": 1})

    assert drain(watcher_a) == [{"n": 1}] and drain(other_a) == [{"n": 1}]
    assert drain(watcher_b) == []


def test_messages_keep_their_order():
    hub, m = EventHub(), uuid4()
    sub = hub.subscribe(m)
    for n in range(5):
        hub.publish(m, {"n": n})
    assert [x["n"] for x in drain(sub)] == [0, 1, 2, 3, 4]


def test_unsubscribing_stops_delivery_and_forgets_the_mission():
    hub, m = EventHub(), uuid4()
    sub = hub.subscribe(m)
    assert hub.is_watched(m) and hub.count() == 1

    hub.unsubscribe(sub)
    hub.publish(m, {"n": 1})

    assert drain(sub) == [] and not hub.is_watched(m) and hub.count() == 0
    hub.unsubscribe(sub)  # twice is harmless


def test_a_mission_stays_watched_until_its_last_subscriber_leaves():
    hub, m = EventHub(), uuid4()
    first, second = hub.subscribe(m), hub.subscribe(m)
    hub.unsubscribe(first)
    assert hub.is_watched(m)
    hub.unsubscribe(second)
    assert not hub.is_watched(m)


def test_a_client_that_cannot_keep_up_is_cut_off_without_affecting_others():
    hub, m = EventHub(queue_size=3), uuid4()
    slow, quick = hub.subscribe(m), hub.subscribe(m)

    received_by_quick = []
    for n in range(8):
        hub.publish(m, {"n": n})
        received_by_quick += drain(quick)  # the quick client reads as fast as messages arrive

    assert received_by_quick == [{"n": n} for n in range(8)]  # nothing lost, nothing slowed
    assert drain(slow)[0] == OVERFLOW  # the slow one's backlog was dropped and it was told to go


def test_resync_reaches_every_subscriber_of_every_mission():
    hub = EventHub()
    subs = [hub.subscribe(uuid4()), hub.subscribe(uuid4()), hub.subscribe(uuid4())]
    hub.resync_all()
    assert all(drain(s) == [RESYNC] for s in subs)


def test_is_watched_can_be_asked_from_another_thread_while_the_loop_changes_it():
    hub, m = EventHub(), uuid4()
    answers: list[bool] = []
    stop = threading.Event()

    def ask():
        while not stop.is_set():
            answers.append(hub.is_watched(m))

    thread = threading.Thread(target=ask)
    thread.start()
    for _ in range(200):
        sub = hub.subscribe(m)
        hub.unsubscribe(sub)
    stop.set()
    thread.join(2)
    assert not thread.is_alive() and answers  # no exception, no deadlock


def test_only_a_terminal_mission_message_ends_the_stream():
    def mission(status):
        return {"type": "mission", "mission": {"status": status}}

    assert all(_is_final(mission(s)) for s in ("completed", "failed", "cancelled"))
    assert not any(_is_final(mission(s)) for s in ("pending", "planning", "running"))
    assert not _is_final({"type": "task", "task": {"status": "completed"}})
