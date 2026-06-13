"""Tests for the Event Hubs emulation."""

import pytest

from openazure.store import Store
from openazure.eventhubs import EventHubsService, _partition_for_key
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def eh(store):
    return EventHubsService(store)


# ---------------------------------------------------------------------------
# partition_for_key helper
# ---------------------------------------------------------------------------

def test_partition_for_key_deterministic():
    p1 = _partition_for_key("sensor-42", 4)
    p2 = _partition_for_key("sensor-42", 4)
    assert p1 == p2


def test_partition_for_key_in_range():
    for i in range(20):
        assert 0 <= _partition_for_key(f"key-{i}", 8) < 8


# ---------------------------------------------------------------------------
# Hub management
# ---------------------------------------------------------------------------

def test_create_list_delete_hub(eh):
    eh.create_hub("telemetry")
    eh.create_hub("logs")
    assert set(eh.list_hubs()) == {"telemetry", "logs"}
    eh.delete_hub("telemetry")
    assert eh.list_hubs() == ["logs"]


def test_duplicate_hub_raises(eh):
    eh.create_hub("h")
    with pytest.raises(Conflict):
        eh.create_hub("h")


def test_delete_missing_hub_raises(eh):
    with pytest.raises(NotFound):
        eh.delete_hub("ghost")


def test_hub_properties(eh):
    eh.create_hub("h", partition_count=8, message_retention=3)
    p = eh.get_hub_properties("h")
    assert p["partition_count"] == 8
    assert p["message_retention"] == 3


def test_hub_invalid_partition_count(eh):
    with pytest.raises(BadRequest):
        eh.create_hub("h", partition_count=0)
    with pytest.raises(BadRequest):
        eh.create_hub("bad", partition_count=33)


def test_default_consumer_group_created(eh):
    eh.create_hub("h")
    cgs = eh.list_consumer_groups("h")
    assert "$Default" in cgs


# ---------------------------------------------------------------------------
# Partitions
# ---------------------------------------------------------------------------

def test_list_partitions(eh):
    eh.create_hub("h", partition_count=3)
    assert eh.list_partitions("h") == [0, 1, 2]


def test_partition_properties_empty(eh):
    eh.create_hub("h", partition_count=2)
    p = eh.get_partition_properties("h", 0)
    assert p["partition"] == 0
    assert p["is_empty"] is True


def test_partition_out_of_range(eh):
    eh.create_hub("h", partition_count=2)
    with pytest.raises(BadRequest):
        eh.get_partition_properties("h", 5)


# ---------------------------------------------------------------------------
# Consumer groups
# ---------------------------------------------------------------------------

def test_create_list_delete_consumer_group(eh):
    eh.create_hub("h")
    eh.create_consumer_group("h", "cg1")
    eh.create_consumer_group("h", "cg2")
    cgs = eh.list_consumer_groups("h")
    assert "cg1" in cgs and "cg2" in cgs
    eh.delete_consumer_group("h", "cg1")
    assert "cg1" not in eh.list_consumer_groups("h")


def test_duplicate_consumer_group_raises(eh):
    eh.create_hub("h")
    eh.create_consumer_group("h", "cg")
    with pytest.raises(Conflict):
        eh.create_consumer_group("h", "cg")


def test_cannot_delete_default_consumer_group(eh):
    eh.create_hub("h")
    with pytest.raises(BadRequest):
        eh.delete_consumer_group("h", "$Default")


# ---------------------------------------------------------------------------
# Sending events
# ---------------------------------------------------------------------------

def test_send_single_event(eh):
    eh.create_hub("h")
    r = eh.send_event("h", "temperature=22.5")
    assert "event_id" in r
    assert "sequence_number" in r
    assert r["hub"] == "h"


def test_send_with_partition_key(eh):
    eh.create_hub("h", partition_count=4)
    r = eh.send_event("h", "msg", partition_key="sensor-1")
    expected = _partition_for_key("sensor-1", 4)
    assert r["partition"] == expected


def test_send_to_explicit_partition(eh):
    eh.create_hub("h", partition_count=4)
    r = eh.send_event("h", "msg", partition=2)
    assert r["partition"] == 2


def test_send_batch(eh):
    eh.create_hub("h", partition_count=2)
    events = [{"body": f"event-{i}"} for i in range(5)]
    results = eh.send_batch("h", events, partition=0)
    assert len(results) == 5
    # All in partition 0
    assert all(r["partition"] == 0 for r in results)


def test_send_event_invalid_partition(eh):
    eh.create_hub("h", partition_count=2)
    with pytest.raises(BadRequest):
        eh.send_event("h", "msg", partition=5)


# ---------------------------------------------------------------------------
# Receiving events
# ---------------------------------------------------------------------------

def test_receive_events_in_order(eh):
    eh.create_hub("h", partition_count=1)
    for i in range(5):
        eh.send_event("h", f"msg-{i}", partition=0)
    events = eh.receive_events("h", 0, max_events=10)
    bodies = [e["body"] for e in events]
    assert bodies == [f"msg-{i}" for i in range(5)]


def test_receive_advances_checkpoint(eh):
    eh.create_hub("h", partition_count=1)
    eh.send_event("h", "e1", partition=0)
    eh.send_event("h", "e2", partition=0)
    first = eh.receive_events("h", 0, max_events=1)
    assert len(first) == 1
    # Second call should pick up from the checkpoint
    second = eh.receive_events("h", 0, max_events=10)
    assert len(second) == 1
    assert second[0]["body"] == "e2"


def test_receive_from_sequence(eh):
    eh.create_hub("h", partition_count=1)
    eh.send_event("h", "e1", partition=0)
    r2 = eh.send_event("h", "e2", partition=0)
    eh.send_event("h", "e3", partition=0)
    # Start from sequence of e2
    events = eh.receive_events("h", 0, starting_sequence=r2["sequence_number"])
    bodies = [e["body"] for e in events]
    assert "e2" in bodies
    assert "e3" in bodies
    assert "e1" not in bodies


def test_receive_missing_consumer_group(eh):
    eh.create_hub("h")
    with pytest.raises(NotFound):
        eh.receive_events("h", 0, consumer_group="nonexistent")


def test_multiple_consumer_groups_independent(eh):
    eh.create_hub("h", partition_count=1)
    eh.create_consumer_group("h", "cg2")
    eh.send_event("h", "msg", partition=0)
    # cg1 reads
    ev1 = eh.receive_events("h", 0, consumer_group="$Default")
    assert len(ev1) == 1
    # cg2 also reads the same event independently
    ev2 = eh.receive_events("h", 0, consumer_group="cg2")
    assert len(ev2) == 1
    assert ev2[0]["body"] == "msg"


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def test_checkpoint_starts_at_minus_one(eh):
    eh.create_hub("h", partition_count=1)
    cp = eh.get_checkpoint("h", "$Default", 0)
    assert cp["sequence_number"] == -1


def test_update_checkpoint(eh):
    eh.create_hub("h", partition_count=1)
    eh.update_checkpoint("h", "$Default", 0, sequence_number=42, offset=1000)
    cp = eh.get_checkpoint("h", "$Default", 0)
    assert cp["sequence_number"] == 42
    assert cp["offset"] == 1000


def test_partition_properties_after_events(eh):
    eh.create_hub("h", partition_count=1)
    eh.send_event("h", "a", partition=0)
    eh.send_event("h", "b", partition=0)
    p = eh.get_partition_properties("h", 0)
    assert p["last_sequence_number"] == 2
    assert p["is_empty"] is False
