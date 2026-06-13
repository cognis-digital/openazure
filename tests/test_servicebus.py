"""Tests for the Service Bus emulation."""

import time

import pytest

from openazure.store import Store
from openazure.servicebus import ServiceBusService, _SqlFilter
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def sb(store):
    return ServiceBusService(store)


# ---------------------------------------------------------------------------
# SQL filter unit tests
# ---------------------------------------------------------------------------

class TestSqlFilter:
    def _ev(self, **kw):
        return kw

    def test_trivial_true(self):
        assert _SqlFilter("1=1").evaluate({}) is True

    def test_empty(self):
        assert _SqlFilter("").evaluate({}) is True

    def test_equality_string(self):
        f = _SqlFilter("label = 'orders'")
        assert f.evaluate({"label": "orders"})
        assert not f.evaluate({"label": "invoices"})

    def test_inequality(self):
        f = _SqlFilter("label <> 'orders'")
        assert f.evaluate({"label": "invoices"})
        assert not f.evaluate({"label": "orders"})

    def test_numeric_gt(self):
        f = _SqlFilter("amount > 100")
        assert f.evaluate({"amount": 150})
        assert not f.evaluate({"amount": 50})

    def test_numeric_lte(self):
        f = _SqlFilter("amount <= 100")
        assert f.evaluate({"amount": 100})
        assert f.evaluate({"amount": 99})
        assert not f.evaluate({"amount": 101})

    def test_and(self):
        f = _SqlFilter("label = 'orders' AND amount > 10")
        assert f.evaluate({"label": "orders", "amount": 20})
        assert not f.evaluate({"label": "orders", "amount": 5})
        assert not f.evaluate({"label": "other", "amount": 20})

    def test_or(self):
        f = _SqlFilter("label = 'A' OR label = 'B'")
        assert f.evaluate({"label": "A"})
        assert f.evaluate({"label": "B"})
        assert not f.evaluate({"label": "C"})

    def test_not(self):
        f = _SqlFilter("NOT label = 'spam'")
        assert f.evaluate({"label": "orders"})
        assert not f.evaluate({"label": "spam"})

    def test_missing_property(self):
        f = _SqlFilter("amount > 10")
        assert not f.evaluate({})  # missing → False

    def test_user_properties(self):
        f = _SqlFilter("priority = 'high'")
        assert f.evaluate({"properties": {"priority": "high"}})
        assert not f.evaluate({"properties": {"priority": "low"}})


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def test_create_list_delete_queue(sb):
    sb.create_queue("orders")
    sb.create_queue("events")
    assert set(sb.list_queues()) == {"orders", "events"}
    sb.delete_queue("orders")
    assert sb.list_queues() == ["events"]


def test_duplicate_queue_raises(sb):
    sb.create_queue("q")
    with pytest.raises(Conflict):
        sb.create_queue("q")


def test_delete_missing_queue_raises(sb):
    with pytest.raises(NotFound):
        sb.delete_queue("ghost")


def test_queue_properties(sb):
    sb.create_queue("q", max_size_mb=512, lock_duration=30,
                    max_delivery_count=5, requires_session=True)
    p = sb.get_queue_properties("q")
    assert p["max_size_mb"] == 512
    assert p["lock_duration"] == 30
    assert p["max_delivery_count"] == 5
    assert p["requires_session"] is True


# ---------------------------------------------------------------------------
# Sending / receiving from queue
# ---------------------------------------------------------------------------

def test_send_receive_complete_roundtrip(sb):
    sb.create_queue("orders")
    r = sb.send_queue("orders", "order-1")
    assert "message_id" in r
    msgs = sb.receive_queue("orders")
    assert len(msgs) == 1
    assert msgs[0]["body"] == "order-1"
    assert msgs[0]["delivery_count"] == 1
    lock = msgs[0]["lock_token"]
    sb.complete_message("orders", lock)
    # dead-letter should be empty
    assert sb.receive_dead_letter("orders") == []


def test_receive_makes_message_invisible(sb):
    sb.create_queue("q")
    sb.send_queue("q", "x")
    sb.receive_queue("q", lock_duration=30)
    # second receive should see nothing (still locked)
    assert sb.receive_queue("q") == []


def test_abandon_requeues_message(sb):
    sb.create_queue("q")
    sb.send_queue("q", "x")
    msgs = sb.receive_queue("q", lock_duration=30)
    lock = msgs[0]["lock_token"]
    sb.abandon_message("q", lock)
    # now it should be immediately receivable
    again = sb.receive_queue("q")
    assert len(again) == 1
    assert again[0]["body"] == "x"
    assert again[0]["delivery_count"] == 2


def test_dead_letter_explicit(sb):
    sb.create_queue("q")
    sb.send_queue("q", "bad-msg")
    msgs = sb.receive_queue("q")
    sb.dead_letter_message("q", msgs[0]["lock_token"], reason="ToxicMessage")
    dl = sb.receive_dead_letter("q")
    assert len(dl) == 1
    assert dl[0]["dead_letter_reason"] == "ToxicMessage"


def test_auto_dead_letter_on_max_delivery(sb):
    sb.create_queue("q", max_delivery_count=2, lock_duration=0)
    sb.send_queue("q", "retry-me")
    # First receive
    msgs = sb.receive_queue("q", lock_duration=0)
    lock1 = msgs[0]["lock_token"]
    sb.abandon_message("q", lock1)
    # Second receive
    msgs2 = sb.receive_queue("q", lock_duration=0)
    lock2 = msgs2[0]["lock_token"]
    sb.abandon_message("q", lock2)
    # Third receive should trigger auto-DL (delivery_count now > max=2)
    msgs3 = sb.receive_queue("q", lock_duration=0)
    # Message should now be dead-lettered
    dl = sb.receive_dead_letter("q")
    assert any(d["dead_letter_reason"] == "MaxDeliveryCountExceeded" for d in dl)


def test_complete_with_wrong_token_raises(sb):
    sb.create_queue("q")
    sb.send_queue("q", "x")
    with pytest.raises(NotFound):
        sb.complete_message("q", "bad-token")


def test_queue_message_count(sb):
    sb.create_queue("q")
    sb.send_queue("q", "a")
    sb.send_queue("q", "b")
    c = sb.queue_message_count("q")
    assert c["active"] == 2
    assert c["dead_letter"] == 0


# ---------------------------------------------------------------------------
# Session-enabled queues
# ---------------------------------------------------------------------------

def test_session_queue_requires_session_id(sb):
    sb.create_queue("sessq", requires_session=True)
    with pytest.raises(BadRequest):
        sb.send_queue("sessq", "no-session")


def test_session_send_receive(sb):
    sb.create_queue("sessq", requires_session=True)
    sb.send_queue("sessq", "for-alice", session_id="alice")
    sb.send_queue("sessq", "for-bob", session_id="bob")
    alice_msgs = sb.receive_session("sessq", "alice")
    assert len(alice_msgs) == 1
    assert alice_msgs[0]["body"] == "for-alice"
    assert alice_msgs[0]["session_id"] == "alice"
    bob_msgs = sb.receive_session("sessq", "bob")
    assert len(bob_msgs) == 1
    assert bob_msgs[0]["body"] == "for-bob"


# ---------------------------------------------------------------------------
# Topics / subscriptions
# ---------------------------------------------------------------------------

def test_create_topic_and_list(sb):
    sb.create_topic("orders")
    sb.create_topic("events")
    assert set(sb.list_topics()) == {"orders", "events"}


def test_duplicate_topic_raises(sb):
    sb.create_topic("t")
    with pytest.raises(Conflict):
        sb.create_topic("t")


def test_delete_topic_cascades(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "sub1")
    sb.send_queue  # not called; fan-out goes through publish_topic
    sb.delete_topic("t")
    assert "t" not in sb.list_topics()


def test_subscription_default_rule_allows_all(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "all")
    sb.publish_topic("t", "hello")
    msgs = sb.receive_subscription("t", "all")
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hello"


def test_subscription_sql_filter(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "orders")
    sb.create_subscription("t", "events")
    # Replace default rule for "orders" sub with a filter
    sb.remove_rule("t", "orders", "$Default")
    sb.add_rule("t", "orders", "OrdersOnly", "label = 'order'")
    # "events" keeps $Default = allow all

    sb.publish_topic("t", "order-1", label="order")
    sb.publish_topic("t", "event-1", label="event")

    order_msgs = sb.receive_subscription("t", "orders", max_messages=10)
    event_msgs = sb.receive_subscription("t", "events", max_messages=10)

    assert len(order_msgs) == 1
    assert order_msgs[0]["body"] == "order-1"
    # events sub gets both (no filter)
    assert len(event_msgs) == 2


def test_subscription_property_filter(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "high-pri")
    sb.remove_rule("t", "high-pri", "$Default")
    sb.add_rule("t", "high-pri", "HighPriority", "priority > 5")

    sb.publish_topic("t", "urgent", properties={"priority": 10})
    sb.publish_topic("t", "normal", properties={"priority": 2})

    msgs = sb.receive_subscription("t", "high-pri")
    assert len(msgs) == 1
    assert msgs[0]["body"] == "urgent"


def test_list_rules(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "s")
    sb.add_rule("t", "s", "R2", "amount > 0")
    rules = sb.list_rules("t", "s")
    names = [r["name"] for r in rules]
    assert "$Default" in names
    assert "R2" in names


def test_remove_rule(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "s")
    sb.remove_rule("t", "s", "$Default")
    rules = sb.list_rules("t", "s")
    assert all(r["name"] != "$Default" for r in rules)


def test_delete_subscription(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "s")
    sb.delete_subscription("t", "s")
    assert "s" not in sb.list_subscriptions("t")


def test_publish_with_no_subscriptions(sb):
    sb.create_topic("t")
    result = sb.publish_topic("t", "msg")
    assert result["delivered_to"] == []


def test_complete_subscription_message(sb):
    sb.create_topic("t")
    sb.create_subscription("t", "s")
    sb.publish_topic("t", "msg")
    msgs = sb.receive_subscription("t", "s")
    dest = "t/s"
    sb.complete_message(dest, msgs[0]["lock_token"])
    assert sb.receive_subscription("t", "s") == []
