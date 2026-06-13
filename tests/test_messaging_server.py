"""End-to-end HTTP server tests for Service Bus, Event Hubs, and Event Grid,
plus Queue update_message and the enhanced Functions triggers."""

import json
import threading
import urllib.request
import urllib.error

import pytest

from openazure.server import make_server


@pytest.fixture
def live_server():
    httpd, app = make_server(host="127.0.0.1", port=0, in_memory=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield base, app
    httpd.shutdown()
    app.close()


def _req(method, url, data=None, content_type="application/json"):
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            payload = json.loads(raw) if "json" in ct else raw
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), {}
        except Exception:
            return e.code, raw, {}


# ---------------------------------------------------------------------------
# Index includes new services
# ---------------------------------------------------------------------------

def test_index_includes_new_services(live_server):
    base, _ = live_server
    s, body, _ = _req("GET", base + "/")
    assert s == 200
    svcs = set(body["services"])
    assert {"servicebus", "eventhubs", "eventgrid"}.issubset(svcs)


# ---------------------------------------------------------------------------
# Queue update_message via PATCH
# ---------------------------------------------------------------------------

def test_queue_update_message_via_patch(live_server):
    base, _ = live_server
    _req("PUT", base + "/queue/q")
    _req("POST", base + "/queue/q/messages", {"body": "original"})
    s, body, _ = _req("GET", base + "/queue/q/messages?num=1&vt=60")
    assert s == 200
    msg = body["messages"][0]
    mid = msg["id"]
    receipt = msg["pop_receipt"]
    # PATCH: update body and extend visibility
    s2, body2, _ = _req(
        "PATCH",
        f"{base}/queue/q/messages/{mid}?pop={receipt}&vt=120",
        {"body": "updated"},
    )
    assert s2 == 200
    assert "pop_receipt" in body2


# ---------------------------------------------------------------------------
# Service Bus HTTP round-trips
# ---------------------------------------------------------------------------

def test_servicebus_queue_roundtrip(live_server):
    base, _ = live_server
    # Create queue
    s, body, _ = _req("PUT", base + "/servicebus/queues/orders",
                       {"max_delivery_count": 5})
    assert s == 201
    assert body["name"] == "orders"

    # List queues
    s, body, _ = _req("GET", base + "/servicebus/queues")
    assert s == 200
    assert "orders" in body["queues"]

    # Queue properties
    s, body, _ = _req("GET", base + "/servicebus/queues/orders")
    assert s == 200
    assert body["max_delivery_count"] == 5

    # Send message
    s, body, _ = _req("POST", base + "/servicebus/queues/orders/messages",
                       {"body": "order-001", "label": "purchase"})
    assert s == 201
    assert "message_id" in body

    # Receive
    s, body, _ = _req("GET", base + "/servicebus/queues/orders/messages?num=1&lock=30")
    assert s == 200
    msgs = body["messages"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "order-001"
    lock = msgs[0]["lock_token"]

    # Complete
    s, body, _ = _req("DELETE",
                       f"{base}/servicebus/queues/orders/messages?lock={lock}")
    assert s == 200
    assert body.get("completed") is True

    # Verify empty
    s, body, _ = _req("GET", base + "/servicebus/queues/orders/messages?num=1")
    assert body["messages"] == []


def test_servicebus_dead_letter(live_server):
    base, _ = live_server
    _req("PUT", base + "/servicebus/queues/dlq")
    _req("POST", base + "/servicebus/queues/dlq/messages", {"body": "toxic"})
    s, body, _ = _req("GET", base + "/servicebus/queues/dlq/messages?num=1&lock=60")
    lock = body["messages"][0]["lock_token"]
    # Dead-letter it
    s, _, _ = _req("POST",
                   f"{base}/servicebus/queues/dlq/messages?comp=deadletter&lock={lock}",
                   {"reason": "Unprocessable"})
    assert s == 200
    # Retrieve from DL
    s, body, _ = _req("GET", base + "/servicebus/queues/dlq/deadletter?num=1")
    assert s == 200
    assert len(body["messages"]) == 1
    assert body["messages"][0]["dead_letter_reason"] == "Unprocessable"


def test_servicebus_abandon(live_server):
    base, _ = live_server
    _req("PUT", base + "/servicebus/queues/aq")
    _req("POST", base + "/servicebus/queues/aq/messages", {"body": "retry"})
    s, body, _ = _req("GET", base + "/servicebus/queues/aq/messages?num=1&lock=60")
    lock = body["messages"][0]["lock_token"]
    s, _, _ = _req("POST",
                   f"{base}/servicebus/queues/aq/messages?comp=abandon&lock={lock}",
                   {})
    assert s == 200
    # Message is re-available
    s, body, _ = _req("GET", base + "/servicebus/queues/aq/messages?num=1&lock=60")
    assert len(body["messages"]) == 1


def test_servicebus_topic_subscription_roundtrip(live_server):
    base, _ = live_server
    # Create topic
    s, body, _ = _req("PUT", base + "/servicebus/topics/events")
    assert s == 201

    # List topics
    s, body, _ = _req("GET", base + "/servicebus/topics")
    assert "events" in body["topics"]

    # Create subscriptions
    s, _, _ = _req("PUT", base + "/servicebus/topics/events/subscriptions/sub1")
    assert s == 201
    _req("PUT", base + "/servicebus/topics/events/subscriptions/sub2")

    # List subscriptions
    s, body, _ = _req("GET", base + "/servicebus/topics/events/subscriptions")
    assert "sub1" in body["subscriptions"]

    # Publish
    s, body, _ = _req("POST", base + "/servicebus/topics/events/messages",
                       {"body": "event-data"})
    assert s == 201
    assert "sub1" in body["delivered_to"]
    assert "sub2" in body["delivered_to"]

    # Receive from sub1
    s, body, _ = _req("GET",
                       base + "/servicebus/topics/events/subscriptions/sub1/messages?num=1")
    assert s == 200
    assert body["messages"][0]["body"] == "event-data"


def test_servicebus_rule_roundtrip(live_server):
    base, _ = live_server
    _req("PUT", base + "/servicebus/topics/t")
    _req("PUT", base + "/servicebus/topics/t/subscriptions/s")
    # Add SQL rule
    s, body, _ = _req("PUT",
                       base + "/servicebus/topics/t/subscriptions/s/rules/MyRule",
                       {"filter_sql": "amount > 100"})
    assert s == 201
    # List rules
    s, body, _ = _req("GET",
                       base + "/servicebus/topics/t/subscriptions/s/rules")
    assert s == 200
    names = [r["name"] for r in body["rules"]]
    assert "MyRule" in names
    # Delete rule
    s, _, _ = _req("DELETE",
                   base + "/servicebus/topics/t/subscriptions/s/rules/MyRule")
    assert s == 200


def test_servicebus_delete_topic(live_server):
    base, _ = live_server
    _req("PUT", base + "/servicebus/topics/t2")
    s, _, _ = _req("DELETE", base + "/servicebus/topics/t2")
    assert s == 200
    s, body, _ = _req("GET", base + "/servicebus/topics")
    assert "t2" not in body["topics"]


# ---------------------------------------------------------------------------
# Event Hubs HTTP round-trips
# ---------------------------------------------------------------------------

def test_eventhubs_hub_roundtrip(live_server):
    base, _ = live_server
    # Create hub
    s, body, _ = _req("PUT", base + "/eventhubs/telemetry",
                       {"partition_count": 4})
    assert s == 201
    assert body["partition_count"] == 4

    # List
    s, body, _ = _req("GET", base + "/eventhubs")
    assert "telemetry" in body["hubs"]

    # Properties
    s, body, _ = _req("GET", base + "/eventhubs/telemetry")
    assert body["name"] == "telemetry"

    # Delete
    s, _, _ = _req("DELETE", base + "/eventhubs/telemetry")
    assert s == 200


def test_eventhubs_send_receive_roundtrip(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventhubs/logs", {"partition_count": 2})

    # Send events
    s, body, _ = _req("POST", base + "/eventhubs/logs/events",
                       {"body": "entry-1", "partition_key": "host-a"})
    assert s == 201
    assert "event_id" in body
    partition = body["partition"]

    # Send second event same partition key (same partition)
    _req("POST", base + "/eventhubs/logs/events",
         {"body": "entry-2", "partition_key": "host-a"})

    # Receive
    s, body, _ = _req("GET",
                       f"{base}/eventhubs/logs/partitions/{partition}/events?cg=$Default&num=10")
    assert s == 200
    bodies = [e["body"] for e in body["events"]]
    assert "entry-1" in bodies
    assert "entry-2" in bodies


def test_eventhubs_send_batch(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventhubs/batch-hub", {"partition_count": 2})
    events = [{"body": f"msg-{i}"} for i in range(5)]
    s, body, _ = _req("POST", base + "/eventhubs/batch-hub/events?partition=0",
                       events)
    assert s == 201
    assert len(body["results"]) == 5


def test_eventhubs_partitions(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventhubs/ph", {"partition_count": 3})
    s, body, _ = _req("GET", base + "/eventhubs/ph/partitions")
    assert s == 200
    assert len(body["partitions"]) == 3
    s, body, _ = _req("GET", base + "/eventhubs/ph/partitions/1")
    assert s == 200
    assert body["partition"] == 1


def test_eventhubs_consumer_groups(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventhubs/cg-hub")
    s, _, _ = _req("PUT", base + "/eventhubs/cg-hub/consumergroups/analytics")
    assert s == 201
    s, body, _ = _req("GET", base + "/eventhubs/cg-hub/consumergroups")
    assert "analytics" in body["consumer_groups"]
    s, _, _ = _req("DELETE", base + "/eventhubs/cg-hub/consumergroups/analytics")
    assert s == 200


def test_eventhubs_checkpoint(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventhubs/chk-hub", {"partition_count": 1})
    # Get initial
    s, body, _ = _req("GET", base + "/eventhubs/chk-hub/partitions/0/checkpoint?cg=$Default")
    assert body["sequence_number"] == -1
    # Update
    s, body, _ = _req("PUT", base + "/eventhubs/chk-hub/partitions/0/checkpoint",
                       {"consumer_group": "$Default",
                        "sequence_number": 10, "offset": 500})
    assert s == 200
    assert body["sequence_number"] == 10


# ---------------------------------------------------------------------------
# Event Grid HTTP round-trips
# ---------------------------------------------------------------------------

def test_eventgrid_topic_roundtrip(live_server):
    base, _ = live_server
    s, body, _ = _req("PUT", base + "/eventgrid/topics/orders")
    assert s == 201
    assert body["name"] == "orders"

    s, body, _ = _req("GET", base + "/eventgrid/topics")
    assert "orders" in body["topics"]

    s, body, _ = _req("GET", base + "/eventgrid/topics/orders")
    assert body["name"] == "orders"

    s, _, _ = _req("DELETE", base + "/eventgrid/topics/orders")
    assert s == 200


def test_eventgrid_subscription_roundtrip(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventgrid/topics/t")
    s, body, _ = _req("PUT",
                       base + "/eventgrid/topics/t/subscriptions/my-sub",
                       {"event_types": ["Order.Created"],
                        "subject_begins_with": "/orders/"})
    assert s == 201
    assert body["name"] == "my-sub"

    s, body, _ = _req("GET", base + "/eventgrid/topics/t/subscriptions")
    assert any(s["name"] == "my-sub" for s in body["subscriptions"])

    s, body, _ = _req("GET", base + "/eventgrid/topics/t/subscriptions/my-sub")
    assert body["event_types"] == ["Order.Created"]

    s, _, _ = _req("DELETE", base + "/eventgrid/topics/t/subscriptions/my-sub")
    assert s == 200


def test_eventgrid_publish_and_list(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventgrid/topics/eg-t")
    _req("PUT", base + "/eventgrid/topics/eg-t/subscriptions/s1")

    events = [
        {
            "id": "e1",
            "eventType": "Order.Created",
            "subject": "/orders/1",
            "eventTime": "2026-01-01T00:00:00Z",
            "data": {"orderId": "1"},
            "dataVersion": "1.0",
        }
    ]
    s, body, _ = _req("POST", base + "/eventgrid/topics/eg-t/events", events)
    assert s == 200
    assert body["stored"] == 1

    # List all events for topic
    s, body, _ = _req("GET", base + "/eventgrid/topics/eg-t/events")
    assert s == 200
    assert len(body["events"]) >= 1

    # Filter by subscription
    s, body, _ = _req("GET", base + "/eventgrid/topics/eg-t/events?sub=s1")
    assert s == 200
    assert all(e["event_type"] == "Order.Created" for e in body["events"])


def test_eventgrid_publish_with_filter(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventgrid/topics/filtered-t")
    # Subscription that only wants Order.Created
    _req("PUT", base + "/eventgrid/topics/filtered-t/subscriptions/orders-only",
         {"event_types": ["Order.Created"]})
    _req("PUT", base + "/eventgrid/topics/filtered-t/subscriptions/all-events")

    events_to_send = [
        {"id": "1", "eventType": "Order.Created", "subject": "/orders/1",
         "eventTime": "2026-01-01T00:00:00Z", "data": {}},
        {"id": "2", "eventType": "Order.Deleted", "subject": "/orders/2",
         "eventTime": "2026-01-01T00:00:00Z", "data": {}},
    ]
    s, body, _ = _req("POST",
                       base + "/eventgrid/topics/filtered-t/events",
                       events_to_send)
    assert s == 200

    # orders-only should only have Order.Created
    s, body, _ = _req("GET",
                       base + "/eventgrid/topics/filtered-t/events?sub=orders-only")
    assert len(body["events"]) == 1

    # all-events should have both
    s, body, _ = _req("GET",
                       base + "/eventgrid/topics/filtered-t/events?sub=all-events")
    assert len(body["events"]) == 2


def test_eventgrid_cloudevents(live_server):
    base, _ = live_server
    _req("PUT", base + "/eventgrid/topics/ce-topic")
    cloud_event = {
        "specversion": "1.0",
        "type": "com.example.sensor",
        "source": "https://sensors.example.com",
        "id": "ce-1",
        "subject": "/sensors/temp",
        "time": "2026-01-01T00:00:00Z",
        "data": {"value": 22.5},
    }
    s, body, _ = _req("POST", base + "/eventgrid/topics/ce-topic/events",
                       [cloud_event])
    assert s == 200
    s, body, _ = _req("GET", base + "/eventgrid/topics/ce-topic/events")
    assert body["events"][0]["event_type"] == "com.example.sensor"


# ---------------------------------------------------------------------------
# 404 for unknown service paths
# ---------------------------------------------------------------------------

def test_unknown_servicebus_path(live_server):
    base, _ = live_server
    s, body, _ = _req("GET", base + "/servicebus/unknown/path")
    assert s == 400


def test_missing_eventhub(live_server):
    base, _ = live_server
    s, body, _ = _req("GET", base + "/eventhubs/ghost")
    assert s == 404


def test_missing_eventgrid_topic(live_server):
    base, _ = live_server
    s, body, _ = _req("GET", base + "/eventgrid/topics/ghost")
    assert s == 404
