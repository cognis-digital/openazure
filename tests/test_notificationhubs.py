"""Tests for the Notification Hubs emulation."""

import pytest

from openazure.store import Store
from openazure.notificationhubs import NotificationHubsService, _eval_tag_expr
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def nh(store):
    return NotificationHubsService(store)


HUB = "my-hub"


# ---------------------------------------------------------------------------
# Hub management
# ---------------------------------------------------------------------------

def test_create_list_delete_hub(nh):
    nh.create_hub(HUB)
    nh.create_hub("hub2")
    hubs = nh.list_hubs()
    assert HUB in hubs and "hub2" in hubs
    nh.delete_hub("hub2")
    assert "hub2" not in nh.list_hubs()


def test_duplicate_hub_raises(nh):
    nh.create_hub(HUB)
    with pytest.raises(Conflict):
        nh.create_hub(HUB)


def test_delete_missing_hub_raises(nh):
    with pytest.raises(NotFound):
        nh.delete_hub("ghost")


def test_delete_hub_cleans_registrations(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "handle-1")
    nh.delete_hub(HUB)
    nh.create_hub(HUB)  # recreate; should be empty
    regs = nh.list_registrations(HUB)
    assert len(regs) == 0


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------

def test_create_get_registration(nh):
    nh.create_hub(HUB)
    r = nh.create_registration(HUB, "device-token-abc",
                                platform="apns",
                                tags=["news", "sports"])
    assert r["handle"] == "device-token-abc"
    assert r["platform"] == "apns"
    assert "news" in r["tags"]

    got = nh.get_registration(HUB, r["id"])
    assert got["id"] == r["id"]


def test_update_registration(nh):
    nh.create_hub(HUB)
    r = nh.create_registration(HUB, "old-handle")
    nh.update_registration(HUB, r["id"], handle="new-handle", tags=["a"])
    got = nh.get_registration(HUB, r["id"])
    assert got["handle"] == "new-handle"
    assert "a" in got["tags"]


def test_delete_registration(nh):
    nh.create_hub(HUB)
    r = nh.create_registration(HUB, "tok")
    nh.delete_registration(HUB, r["id"])
    with pytest.raises(NotFound):
        nh.get_registration(HUB, r["id"])


def test_list_registrations(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "h1", tags=["alpha"])
    nh.create_registration(HUB, "h2", tags=["beta"])
    nh.create_registration(HUB, "h3", tags=["alpha"])
    regs = nh.list_registrations(HUB)
    assert len(regs) == 3


def test_list_registrations_with_tag_filter(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "h1", tags=["alpha", "sports"])
    nh.create_registration(HUB, "h2", tags=["beta"])
    nh.create_registration(HUB, "h3", tags=["alpha"])
    regs = nh.list_registrations(HUB, tag_filter="alpha")
    assert len(regs) == 2
    assert all("alpha" in r["tags"] for r in regs)


def test_list_registrations_top(nh):
    nh.create_hub(HUB)
    for i in range(10):
        nh.create_registration(HUB, f"handle-{i}")
    regs = nh.list_registrations(HUB, top=3)
    assert len(regs) == 3


def test_get_missing_registration(nh):
    nh.create_hub(HUB)
    with pytest.raises(NotFound):
        nh.get_registration(HUB, "nonexistent")


# ---------------------------------------------------------------------------
# Installations
# ---------------------------------------------------------------------------

def test_upsert_get_installation(nh):
    nh.create_hub(HUB)
    r = nh.upsert_installation(HUB, "inst-1",
                               handle="fcm-token",
                               platform="fcm",
                               tags=["premium"])
    assert r["installation_id"] == "inst-1"
    assert r["handle"] == "fcm-token"
    assert "premium" in r["tags"]

    got = nh.get_installation(HUB, "inst-1")
    assert got["handle"] == "fcm-token"


def test_upsert_updates_existing_installation(nh):
    nh.create_hub(HUB)
    nh.upsert_installation(HUB, "inst-2", handle="old", platform="fcm")
    nh.upsert_installation(HUB, "inst-2", handle="new", platform="apns")
    got = nh.get_installation(HUB, "inst-2")
    assert got["handle"] == "new"
    assert got["platform"] == "apns"


def test_delete_installation(nh):
    nh.create_hub(HUB)
    nh.upsert_installation(HUB, "inst-3", handle="h")
    nh.delete_installation(HUB, "inst-3")
    with pytest.raises(NotFound):
        nh.get_installation(HUB, "inst-3")


def test_get_missing_installation(nh):
    nh.create_hub(HUB)
    with pytest.raises(NotFound):
        nh.get_installation(HUB, "nobody")


def test_installation_with_templates(nh):
    nh.create_hub(HUB)
    templates = {
        "myTemplate": {
            "body": '{"data":{"message":"$(message)"}}',
            "tags": ["english"],
        }
    }
    r = nh.upsert_installation(HUB, "t-inst", handle="tok",
                               templates=templates)
    assert "myTemplate" in r["templates"]


# ---------------------------------------------------------------------------
# Send notifications
# ---------------------------------------------------------------------------

def test_send_broadcast_notification(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "device-1")
    nh.create_registration(HUB, "device-2")
    result = nh.send_notification(HUB, {"title": "Hello", "body": "World"})
    assert result["recipient_count"] == 2
    assert result["notification_id"]


def test_send_with_tag_expression(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "d1", tags=["sports", "news"])
    nh.create_registration(HUB, "d2", tags=["sports"])
    nh.create_registration(HUB, "d3", tags=["news"])
    nh.create_registration(HUB, "d4", tags=["tech"])

    # Only devices with "sports" AND "news"
    result = nh.send_notification(HUB, "payload", tag_expression="sports && news")
    assert result["recipient_count"] == 1

    # Devices with "sports" OR "news"
    result2 = nh.send_notification(HUB, "payload", tag_expression="sports || news")
    assert result2["recipient_count"] == 3


def test_send_with_not_tag_expression(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "d1", tags=["premium"])
    nh.create_registration(HUB, "d2", tags=["free"])
    result = nh.send_notification(HUB, "msg", tag_expression="!free")
    assert result["recipient_count"] == 1


def test_send_platform_filter(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "ios-token", platform="apns")
    nh.create_registration(HUB, "android-token", platform="fcm")
    result = nh.send_notification(HUB, "alert", platform="apns")
    assert result["recipient_count"] == 1
    rec = result
    # Only apns device
    sent = nh.get_sent_notification(HUB, rec["notification_id"])
    assert all(r["platform"] == "apns" for r in sent["recipients"])


def test_send_captures_installation_recipients(nh):
    nh.create_hub(HUB)
    nh.upsert_installation(HUB, "i1", handle="fcm-tok", tags=["vip"])
    result = nh.send_notification(HUB, "test")
    assert result["recipient_count"] >= 1


def test_list_sent_notifications(nh):
    nh.create_hub(HUB)
    nh.send_notification(HUB, "msg1")
    nh.send_notification(HUB, "msg2")
    notifications = nh.list_sent_notifications(HUB)
    assert len(notifications) >= 2


def test_get_sent_notification_detail(nh):
    nh.create_hub(HUB)
    nh.create_registration(HUB, "tok")
    result = nh.send_notification(HUB, {"alert": "hey"})
    detail = nh.get_sent_notification(HUB, result["notification_id"])
    assert detail["notification_id"] == result["notification_id"]
    assert detail["recipient_count"] == 1


def test_send_to_missing_hub_raises(nh):
    with pytest.raises(NotFound):
        nh.send_notification("ghost-hub", "msg")


# ---------------------------------------------------------------------------
# Tag expression evaluator
# ---------------------------------------------------------------------------

def test_tag_expr_single_match(nh):
    assert _eval_tag_expr("sports", {"sports", "news"})
    assert not _eval_tag_expr("sports", {"news"})


def test_tag_expr_and(nh):
    assert _eval_tag_expr("a && b", {"a", "b", "c"})
    assert not _eval_tag_expr("a && b", {"a"})


def test_tag_expr_or(nh):
    assert _eval_tag_expr("a || b", {"a"})
    assert _eval_tag_expr("a || b", {"b"})
    assert not _eval_tag_expr("a || b", {"c"})


def test_tag_expr_not(nh):
    assert _eval_tag_expr("!paid", {"free"})
    assert not _eval_tag_expr("!paid", {"paid"})


def test_tag_expr_complex(nh):
    # (sports || news) && !muted
    assert _eval_tag_expr("(sports || news) && !muted", {"sports"})
    assert not _eval_tag_expr("(sports || news) && !muted", {"sports", "muted"})
    assert not _eval_tag_expr("(sports || news) && !muted", {"tech"})


def test_tag_expr_nested_parens(nh):
    expr = "(a || b) && (c || d)"
    assert _eval_tag_expr(expr, {"a", "c"})
    assert not _eval_tag_expr(expr, {"a"})
    assert _eval_tag_expr(expr, {"b", "d"})


def test_tag_expr_three_way_or(nh):
    assert _eval_tag_expr("x || y || z", {"z"})
    assert not _eval_tag_expr("x || y || z", {"w"})
