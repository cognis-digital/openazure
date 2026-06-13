"""Tests for the Event Grid emulation."""

import pytest

from openazure.store import Store
from openazure.eventgrid import EventGridService
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def eg(store):
    return EventGridService(store)


# ---------------------------------------------------------------------------
# Topic management
# ---------------------------------------------------------------------------

def test_create_list_delete_topic(eg):
    eg.create_topic("orders")
    eg.create_topic("events")
    assert set(eg.list_topics()) == {"orders", "events"}
    eg.delete_topic("orders")
    assert eg.list_topics() == ["events"]


def test_duplicate_topic_raises(eg):
    eg.create_topic("t")
    with pytest.raises(Conflict):
        eg.create_topic("t")


def test_delete_missing_topic_raises(eg):
    with pytest.raises(NotFound):
        eg.delete_topic("ghost")


def test_get_topic(eg):
    eg.create_topic("t", schema="CloudEventSchema")
    info = eg.get_topic("t")
    assert info["name"] == "t"
    assert info["schema"] == "CloudEventSchema"


# ---------------------------------------------------------------------------
# Subscription management
# ---------------------------------------------------------------------------

def test_create_list_delete_subscription(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "s1")
    eg.create_subscription("t", "s2")
    subs = eg.list_subscriptions("t")
    names = [s["name"] for s in subs]
    assert "s1" in names and "s2" in names
    eg.delete_subscription("t", "s1")
    subs2 = eg.list_subscriptions("t")
    assert all(s["name"] != "s1" for s in subs2)


def test_duplicate_subscription_raises(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "s")
    with pytest.raises(Conflict):
        eg.create_subscription("t", "s")


def test_delete_missing_subscription_raises(eg):
    eg.create_topic("t")
    with pytest.raises(NotFound):
        eg.delete_subscription("t", "ghost")


def test_get_subscription(eg):
    eg.create_topic("t")
    eg.create_subscription(
        "t", "s",
        endpoint_url="http://example.com/webhook",
        event_types=["Order.Created"],
        subject_begins_with="/orders/",
        subject_ends_with=".json",
        property_filters={"region": "us-east"},
    )
    s = eg.get_subscription("t", "s")
    assert s["endpoint_url"] == "http://example.com/webhook"
    assert s["event_types"] == ["Order.Created"]
    assert s["subject_begins_with"] == "/orders/"
    assert s["subject_ends_with"] == ".json"
    assert s["property_filters"] == {"region": "us-east"}


# ---------------------------------------------------------------------------
# Publishing and filtering
# ---------------------------------------------------------------------------

def _order_event(subject="/orders/123", ev_type="Order.Created", **data):
    return {
        "id": "evt-1",
        "eventType": ev_type,
        "subject": subject,
        "eventTime": "2026-06-13T00:00:00Z",
        "data": data or {"orderId": "123"},
        "dataVersion": "1.0",
    }


def test_publish_empty_raises(eg):
    eg.create_topic("t")
    with pytest.raises(BadRequest):
        eg.publish("t", [])


def test_publish_missing_event_type_raises(eg):
    eg.create_topic("t")
    with pytest.raises(BadRequest):
        eg.publish("t", [{"subject": "/foo"}])


def test_publish_to_missing_topic_raises(eg):
    with pytest.raises(NotFound):
        eg.publish("ghost", [_order_event()])


def test_publish_no_subscriptions_stored_for_inspection(eg):
    eg.create_topic("t")
    eg.publish("t", [_order_event()])
    events = eg.list_events("t")
    assert len(events) == 1
    assert events[0]["event_type"] == "Order.Created"


def test_publish_fans_out_to_all_subscriptions(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "s1")
    eg.create_subscription("t", "s2")
    eg.publish("t", [_order_event()])
    # Events reach both subscriptions
    assert len(eg.list_events("t", subscription="s1")) == 1
    assert len(eg.list_events("t", subscription="s2")) == 1


def test_event_type_filter(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "orders-only", event_types=["Order.Created"])
    eg.create_subscription("t", "all-events")

    eg.publish("t", [_order_event(ev_type="Order.Created")])
    eg.publish("t", [_order_event(ev_type="Order.Deleted")])

    # orders-only: only Order.Created
    assert len(eg.list_events("t", subscription="orders-only")) == 1
    assert eg.list_events("t", subscription="orders-only")[0]["event_type"] == "Order.Created"

    # all-events: both
    assert len(eg.list_events("t", subscription="all-events")) == 2


def test_subject_begins_with_filter(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "orders", subject_begins_with="/orders/")
    eg.create_subscription("t", "all")

    eg.publish("t", [_order_event(subject="/orders/123")])
    eg.publish("t", [_order_event(subject="/invoices/456")])

    assert len(eg.list_events("t", subscription="orders")) == 1
    assert len(eg.list_events("t", subscription="all")) == 2


def test_subject_ends_with_filter(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "json-only", subject_ends_with=".json")

    eg.publish("t", [_order_event(subject="/orders/123.json")])
    eg.publish("t", [_order_event(subject="/orders/456.xml")])

    assert len(eg.list_events("t", subscription="json-only")) == 1


def test_property_filter(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "us-east", property_filters={"region": "us-east"})

    eg.publish("t", [{"id": "1", "eventType": "Test", "subject": "/test",
                      "eventTime": "2026-01-01T00:00:00Z",
                      "data": {"region": "us-east"}}])
    eg.publish("t", [{"id": "2", "eventType": "Test", "subject": "/test",
                      "eventTime": "2026-01-01T00:00:00Z",
                      "data": {"region": "eu-west"}}])

    assert len(eg.list_events("t", subscription="us-east")) == 1


def test_combined_filters(eg):
    eg.create_topic("t")
    eg.create_subscription(
        "t", "narrow",
        event_types=["Order.Created"],
        subject_begins_with="/orders/",
        property_filters={"status": "confirmed"},
    )

    # Matches all criteria
    eg.publish("t", [{"id": "a", "eventType": "Order.Created",
                      "subject": "/orders/1",
                      "eventTime": "2026-01-01T00:00:00Z",
                      "data": {"status": "confirmed"}}])
    # Wrong event type
    eg.publish("t", [{"id": "b", "eventType": "Order.Deleted",
                      "subject": "/orders/2",
                      "eventTime": "2026-01-01T00:00:00Z",
                      "data": {"status": "confirmed"}}])
    # Wrong subject
    eg.publish("t", [{"id": "c", "eventType": "Order.Created",
                      "subject": "/invoices/3",
                      "eventTime": "2026-01-01T00:00:00Z",
                      "data": {"status": "confirmed"}}])

    assert len(eg.list_events("t", subscription="narrow")) == 1


# ---------------------------------------------------------------------------
# CloudEvents schema
# ---------------------------------------------------------------------------

def test_cloudevents_schema(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "s")
    cloud_event = {
        "specversion": "1.0",
        "type": "com.example.order",
        "source": "https://example.com/orders",
        "id": "ce-1",
        "subject": "/orders/1",
        "time": "2026-01-01T00:00:00Z",
        "data": {"orderId": "1"},
    }
    eg.publish("t", [cloud_event])
    events = eg.list_events("t")
    assert events[0]["event_type"] == "com.example.order"
    assert events[0]["source"] == "https://example.com/orders"


# ---------------------------------------------------------------------------
# list_events filters
# ---------------------------------------------------------------------------

def test_list_events_by_type(eg):
    eg.create_topic("t")
    eg.publish("t", [_order_event(ev_type="Order.Created")])
    eg.publish("t", [_order_event(ev_type="Order.Deleted")])
    created = eg.list_events("t", event_type="Order.Created")
    assert all(e["event_type"] == "Order.Created" for e in created)


def test_list_events_limit(eg):
    eg.create_topic("t")
    for i in range(10):
        eg.publish("t", [{"id": str(i), "eventType": "Test",
                          "subject": f"/items/{i}",
                          "eventTime": "2026-01-01T00:00:00Z",
                          "data": {}}])
    events = eg.list_events("t", limit=3)
    assert len(events) <= 3


def test_delete_topic_removes_events_and_subscriptions(eg):
    eg.create_topic("t")
    eg.create_subscription("t", "s")
    eg.publish("t", [_order_event()])
    eg.delete_topic("t")
    # Topic gone; querying should raise
    with pytest.raises(NotFound):
        eg.list_events("t")
