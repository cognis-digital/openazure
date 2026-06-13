import pytest

from openazure.errors import NotFound, Conflict, BadRequest


def test_create_list_tables(table):
    table.create_table("People")
    table.create_table("Orders")
    assert table.list_tables() == ["Orders", "People"]


def test_duplicate_table_conflicts(table):
    table.create_table("T")
    with pytest.raises(Conflict):
        table.create_table("T")


def test_insert_and_get_entity(table):
    table.create_table("People")
    table.insert_entity("People", {
        "PartitionKey": "us", "RowKey": "alice", "age": 30, "active": True,
    })
    e = table.get_entity("People", "us", "alice")
    assert e["age"] == 30
    assert e["active"] is True
    assert e["PartitionKey"] == "us"
    assert e["RowKey"] == "alice"
    assert "Timestamp" in e


def test_insert_requires_keys(table):
    table.create_table("T")
    with pytest.raises(BadRequest):
        table.insert_entity("T", {"PartitionKey": "p"})  # missing RowKey


def test_insert_duplicate_conflicts(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "r"})
    with pytest.raises(Conflict):
        table.insert_entity("T", {"PartitionKey": "p", "RowKey": "r"})


def test_upsert_creates_then_merges(table):
    table.create_table("T")
    table.upsert_entity("T", {"PartitionKey": "p", "RowKey": "r", "a": 1})
    table.upsert_entity("T", {"PartitionKey": "p", "RowKey": "r", "b": 2})
    e = table.get_entity("T", "p", "r")
    assert e["a"] == 1 and e["b"] == 2  # merged


def test_replace_overwrites(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "r", "a": 1, "b": 2})
    table.replace_entity("T", {"PartitionKey": "p", "RowKey": "r", "a": 9})
    e = table.get_entity("T", "p", "r")
    assert e["a"] == 9
    assert "b" not in e  # replaced, not merged


def test_merge_requires_existing(table):
    table.create_table("T")
    with pytest.raises(NotFound):
        table.merge_entity("T", {"PartitionKey": "p", "RowKey": "missing", "x": 1})


def test_query_by_partition(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "us", "RowKey": "1", "n": "a"})
    table.insert_entity("T", {"PartitionKey": "us", "RowKey": "2", "n": "b"})
    table.insert_entity("T", {"PartitionKey": "eu", "RowKey": "3", "n": "c"})
    us = table.query_entities("T", partition_key="us")
    assert {e["RowKey"] for e in us} == {"1", "2"}
    assert len(table.query_entities("T")) == 3


def test_query_with_property_filter(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "1", "status": "open"})
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "2", "status": "closed"})
    res = table.query_entities("T", partition_key="p", filters={"status": "open"})
    assert len(res) == 1 and res[0]["RowKey"] == "1"


def test_delete_entity(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "r"})
    table.delete_entity("T", "p", "r")
    with pytest.raises(NotFound):
        table.get_entity("T", "p", "r")


def test_delete_table_cascades(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "r"})
    table.delete_table("T")
    table.create_table("T")
    assert table.query_entities("T") == []


def test_numeric_keys_coerced_to_str(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": 1, "RowKey": 2, "v": 5})
    assert table.get_entity("T", 1, 2)["v"] == 5
    assert table.get_entity("T", "1", "2")["v"] == 5
