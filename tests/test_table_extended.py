"""Tests for extended Table Storage operations (this pass).

Covers: batch transactions, OData-lite $filter/$top/$select queries.
"""

import pytest

from openazure.errors import NotFound, Conflict, BadRequest


# ---------------------------------------------------------------------------
# OData-lite $filter
# ---------------------------------------------------------------------------

def _seed(table, t_name):
    table.create_table(t_name)
    table.insert_entity(t_name, {"PartitionKey": "p", "RowKey": "1",
                                  "score": 10, "status": "open", "active": True})
    table.insert_entity(t_name, {"PartitionKey": "p", "RowKey": "2",
                                  "score": 20, "status": "closed", "active": False})
    table.insert_entity(t_name, {"PartitionKey": "p", "RowKey": "3",
                                  "score": 30, "status": "open", "active": True})
    table.insert_entity(t_name, {"PartitionKey": "q", "RowKey": "4",
                                  "score": 40, "status": "open", "active": True})


def test_odata_filter_eq_string(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="status eq 'open'")
    assert len(res) == 3
    assert all(e["status"] == "open" for e in res)


def test_odata_filter_eq_number(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="score eq 20")
    assert len(res) == 1 and res[0]["RowKey"] == "2"


def test_odata_filter_gt(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="score gt 20")
    assert {e["RowKey"] for e in res} == {"3", "4"}


def test_odata_filter_lt(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="score lt 20")
    assert len(res) == 1 and res[0]["RowKey"] == "1"


def test_odata_filter_ge(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="score ge 20")
    assert len(res) == 3


def test_odata_filter_le(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="score le 20")
    assert len(res) == 2


def test_odata_filter_ne(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="status ne 'open'")
    assert len(res) == 1 and res[0]["RowKey"] == "2"


def test_odata_filter_bool_eq(table):
    _seed(table, "T")
    res = table.query_entities("T", odata_filter="active eq false")
    assert len(res) == 1 and res[0]["RowKey"] == "2"


def test_odata_filter_and(table):
    _seed(table, "T")
    res = table.query_entities(
        "T", odata_filter="status eq 'open' and score gt 20"
    )
    assert {e["RowKey"] for e in res} == {"3", "4"}


def test_odata_filter_with_partition_scope(table):
    _seed(table, "T")
    res = table.query_entities(
        "T", partition_key="p", odata_filter="score gt 15"
    )
    assert {e["RowKey"] for e in res} == {"2", "3"}


def test_odata_filter_invalid_clause_raises(table):
    table.create_table("T")
    with pytest.raises(BadRequest):
        table.query_entities("T", odata_filter="score BETWEEN 1 AND 10")


# ---------------------------------------------------------------------------
# $top
# ---------------------------------------------------------------------------

def test_top_limits_results(table):
    _seed(table, "T")
    res = table.query_entities("T", top=2)
    assert len(res) == 2


def test_top_zero_returns_empty(table):
    _seed(table, "T")
    res = table.query_entities("T", top=0)
    assert res == []


def test_top_larger_than_result_set(table):
    _seed(table, "T")
    res = table.query_entities("T", top=100)
    assert len(res) == 4


# ---------------------------------------------------------------------------
# $select
# ---------------------------------------------------------------------------

def test_select_restricts_fields(table):
    _seed(table, "T")
    res = table.query_entities("T", select="score,status")
    for e in res:
        assert "score" in e
        assert "status" in e
        assert "active" not in e
        # system keys always present
        assert "PartitionKey" in e
        assert "RowKey" in e


def test_select_single_field(table):
    _seed(table, "T")
    res = table.query_entities("T", select="score")
    assert all("score" in e for e in res)
    assert all("status" not in e for e in res)


# ---------------------------------------------------------------------------
# Batch transactions
# ---------------------------------------------------------------------------

def test_batch_insert_multiple(table):
    table.create_table("T")
    ops = [
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "1", "v": 1}},
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "2", "v": 2}},
    ]
    results = table.batch_execute("T", ops)
    assert len(results) == 2
    assert table.get_entity("T", "p", "1")["v"] == 1
    assert table.get_entity("T", "p", "2")["v"] == 2


def test_batch_mixed_ops(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "existing", "v": 0})
    ops = [
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "new", "v": 99}},
        {"op": "upsert", "entity": {"PartitionKey": "p", "RowKey": "existing", "v": 42}},
        {"op": "delete", "PartitionKey": "p", "RowKey": "existing"},
    ]
    table.batch_execute("T", ops)
    assert table.get_entity("T", "p", "new")["v"] == 99
    with pytest.raises(NotFound):
        table.get_entity("T", "p", "existing")


def test_batch_rollback_on_failure(table):
    """If one op fails, no changes are committed."""
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "1", "v": 1})
    ops = [
        # This would succeed on its own
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "new", "v": 99}},
        # This will fail (duplicate)
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "1", "v": 2}},
    ]
    with pytest.raises(Conflict):
        table.batch_execute("T", ops)
    # new entity must NOT have been committed
    with pytest.raises(NotFound):
        table.get_entity("T", "p", "new")


def test_batch_unknown_op_raises(table):
    table.create_table("T")
    with pytest.raises(BadRequest):
        table.batch_execute("T", [
            {"op": "explode", "entity": {"PartitionKey": "p", "RowKey": "r"}}
        ])


def test_batch_upsert_and_replace(table):
    table.create_table("T")
    table.insert_entity("T", {"PartitionKey": "p", "RowKey": "r", "a": 1, "b": 2})
    ops = [
        {"op": "upsert", "entity": {"PartitionKey": "p", "RowKey": "r", "b": 99}},
        {"op": "replace", "entity": {"PartitionKey": "p", "RowKey": "r", "c": 7}},
    ]
    table.batch_execute("T", ops)
    e = table.get_entity("T", "p", "r")
    assert e["c"] == 7
    assert "a" not in e  # replaced, not merged
