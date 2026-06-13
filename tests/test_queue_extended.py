"""Extended tests for Queue Storage — update_message operation."""

import time

import pytest

from openazure.store import Store
from openazure.queue import QueueService
from openazure.errors import NotFound, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def queue(store):
    return QueueService(store)


def test_update_message_visibility(queue):
    """update_message should extend the visibility timeout."""
    queue.create_queue("q")
    queue.enqueue("q", "task")
    msgs = queue.dequeue("q", visibility_timeout=30)
    m = msgs[0]
    # Update to hide for much longer
    res = queue.update_message("q", m["id"], m["pop_receipt"],
                               visibility_timeout=120)
    assert "pop_receipt" in res
    # Old receipt is invalidated; use new one
    new_receipt = res["pop_receipt"]
    # Confirm the message is still invisible
    assert queue.dequeue("q") == []
    # We can delete it with the new receipt
    queue.delete_message("q", m["id"], new_receipt)
    assert queue.count("q") == 0


def test_update_message_body(queue):
    """update_message can change the message body."""
    queue.create_queue("q")
    queue.enqueue("q", "original")
    msgs = queue.dequeue("q", visibility_timeout=30)
    m = msgs[0]
    res = queue.update_message("q", m["id"], m["pop_receipt"],
                               body="updated")
    new_receipt = res["pop_receipt"]
    # Make message visible by setting vt=0
    queue.update_message("q", m["id"], new_receipt,
                         visibility_timeout=0)
    # Now dequeue to verify body changed
    time.sleep(0.05)
    msgs2 = queue.dequeue("q")
    assert msgs2[0]["body"] == "updated"


def test_update_message_wrong_receipt(queue):
    queue.create_queue("q")
    queue.enqueue("q", "x")
    msgs = queue.dequeue("q")[0]
    with pytest.raises(BadRequest):
        queue.update_message("q", msgs["id"], "bad-receipt")


def test_update_message_missing_id(queue):
    queue.create_queue("q")
    with pytest.raises(NotFound):
        queue.update_message("q", "nonexistent-id", "any-receipt")


def test_update_invalidates_old_pop_receipt(queue):
    """After update_message the old pop_receipt must not work."""
    queue.create_queue("q")
    queue.enqueue("q", "x")
    msgs = queue.dequeue("q", visibility_timeout=30)
    m = msgs[0]
    res = queue.update_message("q", m["id"], m["pop_receipt"],
                               visibility_timeout=60)
    old_receipt = m["pop_receipt"]
    # Old receipt should now fail to delete
    with pytest.raises(BadRequest):
        queue.delete_message("q", m["id"], old_receipt)
