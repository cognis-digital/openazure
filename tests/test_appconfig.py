"""Tests for the App Configuration emulation."""

import pytest

from openazure.store import Store
from openazure.appconfig import AppConfigService
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def ac(store):
    return AppConfigService(store)


AC_STORE = "my-appconfig"


# ---------------------------------------------------------------------------
# Key-values
# ---------------------------------------------------------------------------

def test_set_get_keyvalue(ac):
    r = ac.set_keyvalue(AC_STORE, "app:port", "8080")
    assert r["key"] == "app:port"
    assert r["value"] == "8080"
    got = ac.get_keyvalue(AC_STORE, "app:port")
    assert got["value"] == "8080"


def test_set_keyvalue_with_label(ac):
    ac.set_keyvalue(AC_STORE, "timeout", "30", label="prod")
    ac.set_keyvalue(AC_STORE, "timeout", "5", label="dev")
    assert ac.get_keyvalue(AC_STORE, "timeout", "prod")["value"] == "30"
    assert ac.get_keyvalue(AC_STORE, "timeout", "dev")["value"] == "5"


def test_update_keyvalue(ac):
    ac.set_keyvalue(AC_STORE, "x", "1")
    ac.set_keyvalue(AC_STORE, "x", "2")
    assert ac.get_keyvalue(AC_STORE, "x")["value"] == "2"


def test_delete_keyvalue(ac):
    ac.set_keyvalue(AC_STORE, "del", "bye")
    ac.delete_keyvalue(AC_STORE, "del")
    with pytest.raises(NotFound):
        ac.get_keyvalue(AC_STORE, "del")


def test_delete_missing_raises(ac):
    with pytest.raises(NotFound):
        ac.delete_keyvalue(AC_STORE, "ghost")


def test_list_keyvalues(ac):
    ac.set_keyvalue(AC_STORE, "a", "1")
    ac.set_keyvalue(AC_STORE, "b", "2")
    ac.set_keyvalue(AC_STORE, "c", "3")
    items = ac.list_keyvalues(AC_STORE)
    keys = [i["key"] for i in items]
    assert set(keys) == {"a", "b", "c"}


def test_list_keyvalues_with_key_filter(ac):
    ac.set_keyvalue(AC_STORE, "app:host", "localhost")
    ac.set_keyvalue(AC_STORE, "app:port", "8080")
    ac.set_keyvalue(AC_STORE, "db:host", "db")
    items = ac.list_keyvalues(AC_STORE, key_filter="app:*")
    keys = [i["key"] for i in items]
    assert all(k.startswith("app:") for k in keys)
    assert "db:host" not in keys


def test_list_keyvalues_with_top(ac):
    for i in range(10):
        ac.set_keyvalue(AC_STORE, f"key{i}", str(i))
    items = ac.list_keyvalues(AC_STORE, top=3)
    assert len(items) == 3


def test_empty_key_raises(ac):
    with pytest.raises(BadRequest):
        ac.set_keyvalue(AC_STORE, "", "val")


def test_etag_is_set(ac):
    r = ac.set_keyvalue(AC_STORE, "etag-test", "v")
    assert r["etag"]


def test_get_missing_raises(ac):
    with pytest.raises(NotFound):
        ac.get_keyvalue(AC_STORE, "nope")


# ---------------------------------------------------------------------------
# Lock / Unlock
# ---------------------------------------------------------------------------

def test_lock_prevents_update(ac):
    ac.set_keyvalue(AC_STORE, "locked", "v1")
    ac.lock_keyvalue(AC_STORE, "locked")
    with pytest.raises(BadRequest):
        ac.set_keyvalue(AC_STORE, "locked", "v2")


def test_unlock_allows_update(ac):
    ac.set_keyvalue(AC_STORE, "locked2", "v1")
    ac.lock_keyvalue(AC_STORE, "locked2")
    ac.unlock_keyvalue(AC_STORE, "locked2")
    ac.set_keyvalue(AC_STORE, "locked2", "v2")
    assert ac.get_keyvalue(AC_STORE, "locked2")["value"] == "v2"


def test_lock_prevents_delete(ac):
    ac.set_keyvalue(AC_STORE, "lockdel", "v")
    ac.lock_keyvalue(AC_STORE, "lockdel")
    with pytest.raises(BadRequest):
        ac.delete_keyvalue(AC_STORE, "lockdel")


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------

def test_revisions_tracked_on_each_set(ac):
    ac.set_keyvalue(AC_STORE, "rev", "v1")
    ac.set_keyvalue(AC_STORE, "rev", "v2")
    ac.set_keyvalue(AC_STORE, "rev", "v3")
    revs = ac.list_revisions(AC_STORE, "rev")
    assert len(revs) == 3


def test_revisions_ordered_newest_first(ac):
    ac.set_keyvalue(AC_STORE, "ord", "old")
    ac.set_keyvalue(AC_STORE, "ord", "new")
    revs = ac.list_revisions(AC_STORE, "ord")
    # newest first
    assert revs[0]["value"] == "new"


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

def test_set_get_feature_flag(ac):
    r = ac.set_feature_flag(AC_STORE, "dark-mode", enabled=True,
                            description="Toggle dark mode")
    assert r["key"].startswith(".appconfig.featureflag/")

    ff = ac.get_feature_flag(AC_STORE, "dark-mode")
    assert ff["feature"]["enabled"] is True
    assert ff["feature"]["id"] == "dark-mode"


def test_toggle_feature_flag(ac):
    ac.set_feature_flag(AC_STORE, "feature-x", enabled=False)
    ac.toggle_feature_flag(AC_STORE, "feature-x", enabled=True)
    ff = ac.get_feature_flag(AC_STORE, "feature-x")
    assert ff["feature"]["enabled"] is True


def test_list_feature_flags(ac):
    ac.set_feature_flag(AC_STORE, "f1")
    ac.set_feature_flag(AC_STORE, "f2")
    ac.set_keyvalue(AC_STORE, "regular-key", "value")  # should not appear
    flags = ac.list_feature_flags(AC_STORE)
    flag_ids = [f["feature"].get("id") for f in flags]
    assert "f1" in flag_ids
    assert "f2" in flag_ids
    assert "regular-key" not in flag_ids


def test_feature_flag_with_label(ac):
    ac.set_feature_flag(AC_STORE, "rollout", enabled=True, label="beta")
    ac.set_feature_flag(AC_STORE, "rollout", enabled=False, label="prod")
    beta = ac.get_feature_flag(AC_STORE, "rollout", label="beta")
    prod = ac.get_feature_flag(AC_STORE, "rollout", label="prod")
    assert beta["feature"]["enabled"] is True
    assert prod["feature"]["enabled"] is False


def test_feature_flag_with_conditions(ac):
    conditions = {"client_filters": [{"name": "PercentageFilter",
                                       "parameters": {"value": 50}}]}
    ac.set_feature_flag(AC_STORE, "partial", conditions=conditions)
    ff = ac.get_feature_flag(AC_STORE, "partial")
    assert ff["feature"]["conditions"]["client_filters"][0]["name"] == "PercentageFilter"


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def test_create_get_snapshot(ac):
    ac.set_keyvalue(AC_STORE, "app:host", "localhost")
    ac.set_keyvalue(AC_STORE, "app:port", "8080")
    snap = ac.create_snapshot(AC_STORE, "snap1")
    assert snap["item_count"] >= 2

    got = ac.get_snapshot(AC_STORE, "snap1")
    assert got["name"] == "snap1"
    assert got["item_count"] >= 2
    assert len(got["items"]) >= 2


def test_snapshot_captures_state_at_point_in_time(ac):
    ac.set_keyvalue(AC_STORE, "s", "before")
    snap = ac.create_snapshot(AC_STORE, "point-in-time")
    ac.set_keyvalue(AC_STORE, "s", "after")
    # Snapshot should still have old value
    got = ac.get_snapshot(AC_STORE, "point-in-time")
    snap_values = [item["value"] for item in got["items"] if item["key"] == "s"]
    assert "before" in snap_values


def test_create_duplicate_snapshot_raises(ac):
    ac.set_keyvalue(AC_STORE, "x", "1")
    ac.create_snapshot(AC_STORE, "dup")
    with pytest.raises(Conflict):
        ac.create_snapshot(AC_STORE, "dup")


def test_list_snapshots(ac):
    ac.set_keyvalue(AC_STORE, "y", "2")
    ac.create_snapshot(AC_STORE, "s1")
    ac.create_snapshot(AC_STORE, "s2")
    snaps = ac.list_snapshots(AC_STORE)
    names = [s["name"] for s in snaps]
    assert "s1" in names and "s2" in names


def test_delete_snapshot(ac):
    ac.set_keyvalue(AC_STORE, "z", "3")
    ac.create_snapshot(AC_STORE, "del-snap")
    ac.delete_snapshot(AC_STORE, "del-snap")
    with pytest.raises(NotFound):
        ac.get_snapshot(AC_STORE, "del-snap")


def test_snapshot_with_key_filter(ac):
    ac.set_keyvalue(AC_STORE, "app:a", "1")
    ac.set_keyvalue(AC_STORE, "db:b", "2")
    snap = ac.create_snapshot(AC_STORE, "filtered", key_filter="app:*")
    got = ac.get_snapshot(AC_STORE, "filtered")
    keys = [item["key"] for item in got["items"]]
    assert all(k.startswith("app:") for k in keys)
    assert "db:b" not in keys
