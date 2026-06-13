"""Tests for Cosmos DB service.

Covers: databases, containers, items (CRUD), partition keys, SQL-subset
queries (SELECT * / SELECT fields / WHERE / ORDER BY / OFFSET LIMIT).
"""

import pytest

from openazure.store import Store
from openazure.cosmos import CosmosService
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def cosmos(store):
    return CosmosService(store)


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

def test_create_and_list_databases(cosmos):
    cosmos.create_database("db1")
    cosmos.create_database("db2")
    names = [d["id"] for d in cosmos.list_databases()]
    assert "db1" in names and "db2" in names


def test_duplicate_database_raises(cosmos):
    cosmos.create_database("db")
    with pytest.raises(Conflict):
        cosmos.create_database("db")


def test_get_database(cosmos):
    cosmos.create_database("mydb")
    db = cosmos.get_database("mydb")
    assert db["id"] == "mydb"
    assert "created" in db


def test_get_missing_database_raises(cosmos):
    with pytest.raises(NotFound):
        cosmos.get_database("ghost")


def test_delete_database(cosmos):
    cosmos.create_database("db")
    cosmos.delete_database("db")
    with pytest.raises(NotFound):
        cosmos.get_database("db")


def test_delete_database_cascades(cosmos):
    cosmos.create_database("db")
    cosmos.create_container("db", "c1")
    cosmos.create_item("db", "c1", {"id": "i1", "val": 1})
    cosmos.delete_database("db")
    cosmos.create_database("db")
    assert cosmos.list_containers("db") == []


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def test_create_and_list_containers(cosmos):
    cosmos.create_database("db")
    cosmos.create_container("db", "users", "/userId")
    cosmos.create_container("db", "orders", "/orderId")
    names = [c["id"] for c in cosmos.list_containers("db")]
    assert "users" in names and "orders" in names


def test_container_partition_key_stored(cosmos):
    cosmos.create_database("db")
    cosmos.create_container("db", "c", "/category")
    c = cosmos.get_container("db", "c")
    assert c["partitionKey"]["paths"] == ["/category"]


def test_duplicate_container_raises(cosmos):
    cosmos.create_database("db")
    cosmos.create_container("db", "c")
    with pytest.raises(Conflict):
        cosmos.create_container("db", "c")


def test_delete_container(cosmos):
    cosmos.create_database("db")
    cosmos.create_container("db", "c")
    cosmos.delete_container("db", "c")
    with pytest.raises(NotFound):
        cosmos.get_container("db", "c")


def test_container_in_missing_db_raises(cosmos):
    with pytest.raises(NotFound):
        cosmos.create_container("ghost", "c")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _setup(cosmos):
    cosmos.create_database("db")
    cosmos.create_container("db", "c", "/category")


def test_create_and_get_item(cosmos):
    _setup(cosmos)
    item = cosmos.create_item("db", "c", {"id": "i1", "category": "A", "val": 42})
    assert item["id"] == "i1"
    assert item["val"] == 42
    fetched = cosmos.get_item("db", "c", "i1")
    assert fetched["val"] == 42


def test_create_item_auto_id(cosmos):
    _setup(cosmos)
    item = cosmos.create_item("db", "c", {"category": "B", "name": "x"})
    assert "id" in item
    assert len(item["id"]) > 0


def test_duplicate_item_raises(cosmos):
    _setup(cosmos)
    cosmos.create_item("db", "c", {"id": "i1", "category": "A"})
    with pytest.raises(Conflict):
        cosmos.create_item("db", "c", {"id": "i1", "category": "A"})


def test_upsert_item_creates_then_updates(cosmos):
    _setup(cosmos)
    cosmos.upsert_item("db", "c", {"id": "i1", "category": "A", "v": 1})
    cosmos.upsert_item("db", "c", {"id": "i1", "category": "A", "v": 2})
    assert cosmos.get_item("db", "c", "i1")["v"] == 2


def test_replace_item(cosmos):
    _setup(cosmos)
    cosmos.create_item("db", "c", {"id": "i1", "category": "A", "old": True})
    cosmos.replace_item("db", "c", "i1",
                        {"id": "i1", "category": "A", "new": True})
    got = cosmos.get_item("db", "c", "i1")
    assert got.get("new") is True
    assert "old" not in got


def test_replace_missing_item_raises(cosmos):
    _setup(cosmos)
    with pytest.raises(NotFound):
        cosmos.replace_item("db", "c", "ghost", {"id": "ghost", "category": "A"})


def test_delete_item(cosmos):
    _setup(cosmos)
    cosmos.create_item("db", "c", {"id": "i1", "category": "A"})
    cosmos.delete_item("db", "c", "i1")
    with pytest.raises(NotFound):
        cosmos.get_item("db", "c", "i1")


def test_delete_missing_item_raises(cosmos):
    _setup(cosmos)
    with pytest.raises(NotFound):
        cosmos.delete_item("db", "c", "ghost")


def test_list_items(cosmos):
    _setup(cosmos)
    cosmos.create_item("db", "c", {"id": "a", "category": "X"})
    cosmos.create_item("db", "c", {"id": "b", "category": "Y"})
    items = cosmos.list_items("db", "c")
    ids = {it["id"] for it in items}
    assert "a" in ids and "b" in ids


def test_list_items_by_partition_key(cosmos):
    _setup(cosmos)
    cosmos.create_item("db", "c", {"id": "1", "category": "A"})
    cosmos.create_item("db", "c", {"id": "2", "category": "B"})
    cosmos.create_item("db", "c", {"id": "3", "category": "A"})
    items = cosmos.list_items("db", "c", partition_key="A")
    assert {it["id"] for it in items} == {"1", "3"}


def test_item_has_etag_and_ts(cosmos):
    _setup(cosmos)
    item = cosmos.create_item("db", "c", {"id": "i1", "category": "A"})
    assert "_etag" in item and "_ts" in item


# ---------------------------------------------------------------------------
# SQL-subset queries
# ---------------------------------------------------------------------------

def _seed_items(cosmos):
    _setup(cosmos)
    cosmos.create_item("db", "c", {"id": "1", "category": "fruit", "price": 1.0, "name": "apple"})
    cosmos.create_item("db", "c", {"id": "2", "category": "fruit", "price": 3.0, "name": "mango"})
    cosmos.create_item("db", "c", {"id": "3", "category": "veggie", "price": 2.0, "name": "carrot"})
    cosmos.create_item("db", "c", {"id": "4", "category": "veggie", "price": 5.0, "name": "kale"})


def test_query_select_star(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items("db", "c", "SELECT * FROM c")
    assert len(res) == 4


def test_query_where_eq(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items("db", "c", "SELECT * FROM c WHERE c.category = 'fruit'")
    assert len(res) == 2
    assert all(r["category"] == "fruit" for r in res)


def test_query_where_gt(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items("db", "c", "SELECT * FROM c WHERE c.price > 2.0")
    assert {r["id"] for r in res} == {"2", "4"}


def test_query_where_and(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items(
        "db", "c",
        "SELECT * FROM c WHERE c.category = 'veggie' AND c.price < 4.0"
    )
    assert len(res) == 1 and res[0]["id"] == "3"


def test_query_select_fields(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items("db", "c", "SELECT c.name, c.price FROM c")
    for r in res:
        assert "name" in r and "price" in r
        assert "category" not in r


def test_query_order_by_asc(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items("db", "c", "SELECT * FROM c ORDER BY c.price ASC")
    prices = [r["price"] for r in res]
    assert prices == sorted(prices)


def test_query_order_by_desc(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items("db", "c", "SELECT * FROM c ORDER BY c.price DESC")
    prices = [r["price"] for r in res]
    assert prices == sorted(prices, reverse=True)


def test_query_offset_limit(cosmos):
    _seed_items(cosmos)
    # ORDER BY to get deterministic ordering
    res = cosmos.query_items(
        "db", "c",
        "SELECT * FROM c ORDER BY c.id OFFSET 1 LIMIT 2"
    )
    assert len(res) == 2
    assert res[0]["id"] == "2"


def test_query_invalid_syntax_raises(cosmos):
    _setup(cosmos)
    with pytest.raises(BadRequest):
        cosmos.query_items("db", "c", "FETCH ALL FROM c")


def test_query_where_double_quoted_string(cosmos):
    _seed_items(cosmos)
    res = cosmos.query_items(
        "db", "c", 'SELECT * FROM c WHERE c.category = "fruit"'
    )
    assert len(res) == 2
