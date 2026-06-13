"""Extended tests for Functions — timer, blob, and service-bus triggers."""

import pytest

from openazure.store import Store
from openazure.queue import QueueService
from openazure.servicebus import ServiceBusService
from openazure.functions import FunctionRunner
from openazure.errors import NotFound


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def queue(store):
    return QueueService(store)


@pytest.fixture
def sb(store):
    return ServiceBusService(store)


@pytest.fixture
def functions(queue, sb):
    return FunctionRunner(queue, service_bus_service=sb)


# ---------------------------------------------------------------------------
# Timer triggers
# ---------------------------------------------------------------------------

def test_register_and_fire_timer(functions):
    fired = []

    @functions.timer_function("hourly-report")
    def hourly(timer_info):
        fired.append(timer_info["name"])

    functions.fire_timer("hourly-report")
    assert fired == ["hourly-report"]


def test_timer_info_structure(functions):
    info_received = {}

    @functions.timer_function("check")
    def check(timer_info):
        info_received.update(timer_info)

    functions.fire_timer("check")
    assert "fired_at" in info_received
    assert info_received["is_past_due"] is False
    assert info_received["name"] == "check"


def test_fire_missing_timer(functions):
    with pytest.raises(NotFound):
        functions.fire_timer("nonexistent")


def test_list_timer(functions):
    functions.register_timer("t1", lambda ti: None)
    functions.register_timer("t2", lambda ti: None)
    assert functions.list_timer() == ["t1", "t2"]


def test_timer_decorator(functions):
    @functions.timer_function("daily")
    def daily(ti):
        pass

    assert "daily" in functions.list_timer()


# ---------------------------------------------------------------------------
# Blob triggers
# ---------------------------------------------------------------------------

def test_register_and_fire_blob(functions):
    received = []

    @functions.blob_function("process-upload", "uploads")
    def process(blob_info):
        received.append(blob_info)

    blob = {"container": "uploads", "name": "photo.jpg",
            "content_type": "image/jpeg", "size": 12345}
    functions.trigger_blob("process-upload", blob)
    assert len(received) == 1
    assert received[0]["name"] == "photo.jpg"


def test_trigger_blob_for_container(functions):
    calls = []

    functions.register_blob("fn1", "images", lambda b: calls.append("fn1"))
    functions.register_blob("fn2", "images", lambda b: calls.append("fn2"))
    functions.register_blob("fn3", "docs", lambda b: calls.append("fn3"))

    count = functions.trigger_blob_for_container(
        "images",
        {"container": "images", "name": "x.png", "content_type": "image/png", "size": 1},
    )
    assert count == 2
    assert "fn1" in calls and "fn2" in calls
    assert "fn3" not in calls


def test_fire_missing_blob_trigger(functions):
    with pytest.raises(NotFound):
        functions.trigger_blob("nonexistent", {})


def test_list_blob_triggers(functions):
    functions.register_blob("b1", "c1", lambda b: None)
    functions.register_blob("b2", "c2", lambda b: None)
    assert functions.list_blob() == ["b1", "b2"]


# ---------------------------------------------------------------------------
# Service Bus triggers
# ---------------------------------------------------------------------------

def test_service_bus_queue_trigger(functions, sb):
    sb.create_queue("work")
    sb.send_queue("work", "job-1")
    sb.send_queue("work", "job-2")

    seen = []

    @functions.service_bus_function("worker", "work")
    def worker(msg):
        seen.append(msg["body"])

    processed = functions.poll_service_bus("worker")
    assert processed == 2
    assert "job-1" in seen and "job-2" in seen
    # Messages should have been completed (deleted)
    assert sb.queue_message_count("work")["active"] == 0


def test_service_bus_subscription_trigger(functions, sb):
    sb.create_topic("events")
    sb.create_subscription("events", "processor")
    sb.publish_topic("events", "event-a")
    sb.publish_topic("events", "event-b")

    seen = []

    @functions.service_bus_function("event-handler", "events/processor")
    def handler(msg):
        seen.append(msg["body"])

    processed = functions.poll_service_bus("event-handler")
    assert processed == 2
    assert "event-a" in seen and "event-b" in seen


def test_service_bus_trigger_failure_leaves_message(functions, sb):
    """A handler that raises should not complete the message."""
    sb.create_queue("q")
    sb.send_queue("q", "bad")

    @functions.service_bus_function("bad-handler", "q")
    def bad(msg):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        functions.poll_service_bus("bad-handler", lock_duration=60)

    # Message should still be active (locked, but let's check via count)
    c = sb.queue_message_count("q")
    assert c["active"] == 1


def test_service_bus_trigger_no_service_raises():
    fr = FunctionRunner(queue_service=None, service_bus_service=None)
    fr.register_service_bus("h", "q", lambda m: None)
    with pytest.raises(RuntimeError):
        fr.poll_service_bus("h")


def test_service_bus_trigger_missing_function(functions):
    with pytest.raises(NotFound):
        functions.poll_service_bus("nonexistent")


def test_list_service_bus_triggers(functions):
    functions.register_service_bus("sb1", "q1", lambda m: None)
    functions.register_service_bus("sb2", "q2", lambda m: None)
    assert functions.list_service_bus() == ["sb1", "sb2"]
