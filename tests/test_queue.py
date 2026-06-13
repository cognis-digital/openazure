import time

import pytest

from openazure.errors import NotFound, Conflict, BadRequest


def test_create_list_queues(queue):
    queue.create_queue("jobs")
    queue.create_queue("events")
    assert queue.list_queues() == ["events", "jobs"]


def test_duplicate_queue_conflicts(queue):
    queue.create_queue("q")
    with pytest.raises(Conflict):
        queue.create_queue("q")


def test_enqueue_dequeue_roundtrip(queue):
    queue.create_queue("q")
    queue.enqueue("q", "task-1")
    msgs = queue.dequeue("q")
    assert len(msgs) == 1
    assert msgs[0]["body"] == "task-1"
    assert msgs[0]["dequeue_count"] == 1
    assert "pop_receipt" in msgs[0]


def test_fifo_order(queue):
    queue.create_queue("q")
    for i in range(3):
        queue.enqueue("q", f"m{i}")
    bodies = [m["body"] for m in queue.dequeue("q", max_messages=3)]
    assert bodies == ["m0", "m1", "m2"]


def test_visibility_timeout_hides_message(queue):
    queue.create_queue("q")
    queue.enqueue("q", "x")
    first = queue.dequeue("q", visibility_timeout=30)
    assert len(first) == 1
    # immediately invisible to a second consumer
    assert queue.dequeue("q") == []


def test_visibility_timeout_expiry_redelivers(queue):
    queue.create_queue("q")
    queue.enqueue("q", "x")
    queue.dequeue("q", visibility_timeout=0.3)
    assert queue.dequeue("q") == []  # still hidden
    time.sleep(0.4)
    again = queue.dequeue("q")
    assert len(again) == 1
    assert again[0]["dequeue_count"] == 2  # incremented on redelivery


def test_delete_message_with_receipt(queue):
    queue.create_queue("q")
    queue.enqueue("q", "x")
    m = queue.dequeue("q")[0]
    queue.delete_message("q", m["id"], m["pop_receipt"])
    assert queue.count("q") == 0


def test_delete_with_bad_receipt_raises(queue):
    queue.create_queue("q")
    queue.enqueue("q", "x")
    m = queue.dequeue("q")[0]
    with pytest.raises(BadRequest):
        queue.delete_message("q", m["id"], "wrong-receipt")


def test_enqueue_visibility_delay(queue):
    queue.create_queue("q")
    queue.enqueue("q", "later", visibility_delay=0.3)
    assert queue.dequeue("q") == []  # not yet visible
    time.sleep(0.4)
    assert len(queue.dequeue("q")) == 1


def test_peek_does_not_hide(queue):
    queue.create_queue("q")
    queue.enqueue("q", "x")
    peeked = queue.peek("q")
    assert peeked[0]["body"] == "x"
    # still available to dequeue
    assert len(queue.dequeue("q")) == 1


def test_clear_and_count(queue):
    queue.create_queue("q")
    queue.enqueue("q", "a")
    queue.enqueue("q", "b")
    assert queue.count("q") == 2
    queue.clear("q")
    assert queue.count("q") == 0


def test_operations_on_missing_queue(queue):
    with pytest.raises(NotFound):
        queue.enqueue("ghost", "x")


def test_dequeue_bad_max(queue):
    queue.create_queue("q")
    with pytest.raises(BadRequest):
        queue.dequeue("q", max_messages=0)
