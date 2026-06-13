"""Cosmos DB service (SQL API subset).

A local, compatible subset of Azure Cosmos DB's SQL API. Items are stored
in *containers* inside *databases*, each item uniquely identified by an
``id`` field and a *partition key* property.

Supported operations
--------------------
* Databases: create, delete, list, get.
* Containers: create (with partition key path), delete, list, get.
* Items: create, upsert, replace, get, delete.
* Query: SQL-subset ``SELECT … FROM c [WHERE …] [ORDER BY …] [OFFSET … LIMIT …]``
  executed in-process (no external query parser needed).

SQL subset
~~~~~~~~~~
* ``SELECT *`` or ``SELECT c.prop1, c.prop2, …``
* ``FROM c`` (the collection alias; always ``c``)
* ``WHERE`` expressions with ``=``, ``!=``, ``<``, ``<=``, ``>``, ``>=``,
  ``AND`` (case-insensitive); string values in single or double quotes.
* ``ORDER BY c.prop [ASC|DESC]``
* ``OFFSET n LIMIT m``

Partition key
~~~~~~~~~~~~~
The partition key path is stored (e.g. ``/category``), and the value is
extracted from the item at create/upsert time. Items with a missing
partition key default to ``"__default__"``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# SQL-subset query engine
# ---------------------------------------------------------------------------

_WHERE_CLAUSE = re.compile(
    r"""
    ([\w\.]+)            # property path (c.foo or just foo)
    \s*
    (=|!=|<=|>=|<|>)
    \s*
    (?:
        '([^']*)'        # single-quoted string (group 3)
        |
        "([^"]*)"        # double-quoted string (group 4)
        |
        (true|false)     # bool literal (group 5)
        |
        (-?\d+(?:\.\d+)?)  # number (group 6)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_SELECT_ALIAS = re.compile(r"^c\.", re.IGNORECASE)


def _extract_prop(item: dict, path: str) -> Any:
    """Get a nested property; path like ``c.address.city`` or ``c.id``."""
    # strip leading "c." alias
    path = _SELECT_ALIAS.sub("", path)
    parts = path.split(".")
    val: Any = item
    for p in parts:
        if not isinstance(val, dict):
            return None
        val = val.get(p)
    return val


def _parse_where(where_str: str) -> list[tuple[str, str, Any]]:
    parts = re.split(r"\s+and\s+", where_str, flags=re.IGNORECASE)
    clauses = []
    for part in parts:
        m = _WHERE_CLAUSE.fullmatch(part.strip())
        if not m:
            raise BadRequest(f"unsupported WHERE clause: '{part.strip()}'")
        prop, op = m.group(1), m.group(2)
        if m.group(3) is not None:
            val: Any = m.group(3)
        elif m.group(4) is not None:
            val = m.group(4)
        elif m.group(5) is not None:
            val = m.group(5).lower() == "true"
        else:
            raw = m.group(6)
            val = float(raw) if "." in raw else int(raw)
        clauses.append((prop, op, val))
    return clauses


def _apply_op(lhs: Any, op: str, rhs: Any) -> bool:
    try:
        if op == "=":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs
        if op == "<":
            return lhs < rhs
        if op == "<=":
            return lhs <= rhs
        if op == ">":
            return lhs > rhs
        if op == ">=":
            return lhs >= rhs
    except TypeError:
        return False
    return False


_SQL_RE = re.compile(
    r"""
    SELECT\s+(?P<select>.+?)\s+
    FROM\s+c
    (?:\s+WHERE\s+(?P<where>.+?))?
    (?:\s+ORDER\s+BY\s+(?P<order>[\w\.]+)(?:\s+(?P<dir>ASC|DESC))?)?
    (?:\s+OFFSET\s+(?P<offset>\d+)\s+LIMIT\s+(?P<limit>\d+))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE | re.DOTALL,
)


def _run_query(items: list[dict], sql: str) -> list[dict]:
    m = _SQL_RE.match(sql.strip())
    if not m:
        raise BadRequest(
            f"unsupported query syntax; expected "
            "'SELECT ... FROM c [WHERE ...] [ORDER BY ...] [OFFSET n LIMIT m]'"
        )
    select_raw = m.group("select").strip()
    where_raw = m.group("where")
    order_raw = m.group("order")
    dir_raw = (m.group("dir") or "ASC").upper()
    offset = int(m.group("offset") or 0)
    limit = m.group("limit")

    # WHERE filter
    result = list(items)
    if where_raw:
        clauses = _parse_where(where_raw.strip())
        result = [
            it for it in result
            if all(_apply_op(_extract_prop(it, p), op, v)
                   for p, op, v in clauses)
        ]

    # ORDER BY
    if order_raw:
        def key_fn(it: dict) -> Any:
            v = _extract_prop(it, order_raw)
            return (v is None, v if v is not None else "")
        result.sort(key=key_fn, reverse=(dir_raw == "DESC"))

    # OFFSET / LIMIT
    result = result[offset:]
    if limit is not None:
        result = result[:int(limit)]

    # SELECT projection
    if select_raw.strip() != "*":
        fields = [f.strip() for f in select_raw.split(",")]
        projected = []
        for it in result:
            row: dict = {}
            for f in fields:
                clean = _SELECT_ALIAS.sub("", f)
                row[clean] = _extract_prop(it, f)
            projected.append(row)
        return projected

    return result


# ---------------------------------------------------------------------------
class CosmosService:
    """Local Cosmos DB SQL-API emulation."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS cosmos_databases (
                name TEXT PRIMARY KEY,
                created TEXT NOT NULL
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS cosmos_containers (
                db TEXT NOT NULL,
                name TEXT NOT NULL,
                partition_key_path TEXT NOT NULL,
                created TEXT NOT NULL,
                PRIMARY KEY (db, name)
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS cosmos_items (
                db TEXT NOT NULL,
                container TEXT NOT NULL,
                id TEXT NOT NULL,
                partition_key TEXT NOT NULL,
                body TEXT NOT NULL,
                last_modified TEXT NOT NULL,
                etag TEXT NOT NULL,
                PRIMARY KEY (db, container, id, partition_key)
            )
            """
        )

    # ------------------------------------------------------------------
    # Databases
    # ------------------------------------------------------------------
    def create_database(self, name: str) -> dict:
        if self.store.query(
            "SELECT name FROM cosmos_databases WHERE name=?", (name,)
        ):
            raise Conflict(f"Cosmos DB database '{name}' already exists")
        ts = _now_iso()
        self.store.execute(
            "INSERT INTO cosmos_databases (name, created) VALUES (?, ?)",
            (name, ts),
        )
        return {"id": name, "created": ts}

    def delete_database(self, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM cosmos_databases WHERE name=?", (name,)
        ):
            raise NotFound(f"Cosmos DB database '{name}' not found")
        self.store.execute(
            "DELETE FROM cosmos_items WHERE db=?", (name,)
        )
        self.store.execute(
            "DELETE FROM cosmos_containers WHERE db=?", (name,)
        )
        self.store.execute(
            "DELETE FROM cosmos_databases WHERE name=?", (name,)
        )

    def list_databases(self) -> list[dict]:
        rows = self.store.query(
            "SELECT name, created FROM cosmos_databases ORDER BY name"
        )
        return [{"id": r["name"], "created": r["created"]} for r in rows]

    def get_database(self, name: str) -> dict:
        rows = self.store.query(
            "SELECT name, created FROM cosmos_databases WHERE name=?",
            (name,),
        )
        if not rows:
            raise NotFound(f"Cosmos DB database '{name}' not found")
        r = rows[0]
        return {"id": r["name"], "created": r["created"]}

    def _require_database(self, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM cosmos_databases WHERE name=?", (name,)
        ):
            raise NotFound(f"Cosmos DB database '{name}' not found")

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------
    def create_container(self, db: str, name: str,
                         partition_key_path: str = "/id") -> dict:
        self._require_database(db)
        if self.store.query(
            "SELECT name FROM cosmos_containers WHERE db=? AND name=?",
            (db, name),
        ):
            raise Conflict(
                f"Cosmos DB container '{name}' already exists in '{db}'"
            )
        ts = _now_iso()
        self.store.execute(
            "INSERT INTO cosmos_containers "
            "(db, name, partition_key_path, created) VALUES (?, ?, ?, ?)",
            (db, name, partition_key_path, ts),
        )
        return {
            "id": name,
            "db": db,
            "partitionKey": {"paths": [partition_key_path]},
            "created": ts,
        }

    def delete_container(self, db: str, name: str) -> None:
        self._require_database(db)
        if not self.store.query(
            "SELECT name FROM cosmos_containers WHERE db=? AND name=?",
            (db, name),
        ):
            raise NotFound(
                f"Cosmos DB container '{name}' not found in '{db}'"
            )
        self.store.execute(
            "DELETE FROM cosmos_items WHERE db=? AND container=?",
            (db, name),
        )
        self.store.execute(
            "DELETE FROM cosmos_containers WHERE db=? AND name=?",
            (db, name),
        )

    def list_containers(self, db: str) -> list[dict]:
        self._require_database(db)
        rows = self.store.query(
            "SELECT name, partition_key_path, created FROM cosmos_containers "
            "WHERE db=? ORDER BY name",
            (db,),
        )
        return [
            {
                "id": r["name"],
                "db": db,
                "partitionKey": {"paths": [r["partition_key_path"]]},
                "created": r["created"],
            }
            for r in rows
        ]

    def get_container(self, db: str, name: str) -> dict:
        self._require_database(db)
        rows = self.store.query(
            "SELECT name, partition_key_path, created FROM cosmos_containers "
            "WHERE db=? AND name=?",
            (db, name),
        )
        if not rows:
            raise NotFound(
                f"Cosmos DB container '{name}' not found in '{db}'"
            )
        r = rows[0]
        return {
            "id": r["name"],
            "db": db,
            "partitionKey": {"paths": [r["partition_key_path"]]},
            "created": r["created"],
        }

    def _require_container(self, db: str, container: str) -> str:
        """Returns partition_key_path."""
        self._require_database(db)
        rows = self.store.query(
            "SELECT partition_key_path FROM cosmos_containers "
            "WHERE db=? AND name=?",
            (db, container),
        )
        if not rows:
            raise NotFound(
                f"Cosmos DB container '{container}' not found in '{db}'"
            )
        return rows[0]["partition_key_path"]

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------
    def _pk_value(self, item: dict, pk_path: str) -> str:
        """Extract partition key value from item using a path like '/category'."""
        key = pk_path.lstrip("/")
        return str(item.get(key, "__default__"))

    def create_item(self, db: str, container: str, item: dict) -> dict:
        pk_path = self._require_container(db, container)
        if "id" not in item:
            item = dict(item)
            item["id"] = uuid.uuid4().hex
        item_id = str(item["id"])
        pk_val = self._pk_value(item, pk_path)
        if self.store.query(
            "SELECT id FROM cosmos_items "
            "WHERE db=? AND container=? AND id=? AND partition_key=?",
            (db, container, item_id, pk_val),
        ):
            raise Conflict(
                f"Cosmos item '{item_id}' already exists in '{db}/{container}'"
            )
        ts = _now_iso()
        etag = uuid.uuid4().hex
        body = dict(item)
        body.setdefault("_ts", ts)
        body["_etag"] = etag
        self.store.execute(
            "INSERT INTO cosmos_items "
            "(db, container, id, partition_key, body, last_modified, etag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (db, container, item_id, pk_val, json.dumps(body), ts, etag),
        )
        return body

    def upsert_item(self, db: str, container: str, item: dict) -> dict:
        pk_path = self._require_container(db, container)
        if "id" not in item:
            item = dict(item)
            item["id"] = uuid.uuid4().hex
        item_id = str(item["id"])
        pk_val = self._pk_value(item, pk_path)
        ts = _now_iso()
        etag = uuid.uuid4().hex
        body = dict(item)
        body["_ts"] = ts
        body["_etag"] = etag
        self.store.execute(
            "INSERT INTO cosmos_items "
            "(db, container, id, partition_key, body, last_modified, etag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(db, container, id, partition_key) DO UPDATE SET "
            "body=excluded.body, last_modified=excluded.last_modified, "
            "etag=excluded.etag",
            (db, container, item_id, pk_val, json.dumps(body), ts, etag),
        )
        return body

    def replace_item(self, db: str, container: str,
                     item_id: str, item: dict,
                     partition_key: Optional[str] = None) -> dict:
        pk_path = self._require_container(db, container)
        pk_val = (partition_key
                  if partition_key is not None
                  else self._pk_value(item, pk_path))
        if not self.store.query(
            "SELECT id FROM cosmos_items "
            "WHERE db=? AND container=? AND id=? AND partition_key=?",
            (db, container, str(item_id), str(pk_val)),
        ):
            raise NotFound(
                f"Cosmos item '{item_id}' not found in '{db}/{container}'"
            )
        return self.upsert_item(db, container,
                                dict(item, id=item_id))

    def get_item(self, db: str, container: str, item_id: str,
                 partition_key: Optional[str] = None) -> dict:
        self._require_container(db, container)
        if partition_key is not None:
            rows = self.store.query(
                "SELECT body FROM cosmos_items "
                "WHERE db=? AND container=? AND id=? AND partition_key=?",
                (db, container, str(item_id), str(partition_key)),
            )
        else:
            rows = self.store.query(
                "SELECT body FROM cosmos_items "
                "WHERE db=? AND container=? AND id=?",
                (db, container, str(item_id)),
            )
        if not rows:
            raise NotFound(
                f"Cosmos item '{item_id}' not found in '{db}/{container}'"
            )
        return json.loads(rows[0]["body"])

    def delete_item(self, db: str, container: str, item_id: str,
                    partition_key: Optional[str] = None) -> None:
        self._require_container(db, container)
        if partition_key is not None:
            rows = self.store.query(
                "SELECT id FROM cosmos_items "
                "WHERE db=? AND container=? AND id=? AND partition_key=?",
                (db, container, str(item_id), str(partition_key)),
            )
            if not rows:
                raise NotFound(
                    f"Cosmos item '{item_id}' not found in '{db}/{container}'"
                )
            self.store.execute(
                "DELETE FROM cosmos_items "
                "WHERE db=? AND container=? AND id=? AND partition_key=?",
                (db, container, str(item_id), str(partition_key)),
            )
        else:
            rows = self.store.query(
                "SELECT id FROM cosmos_items "
                "WHERE db=? AND container=? AND id=?",
                (db, container, str(item_id)),
            )
            if not rows:
                raise NotFound(
                    f"Cosmos item '{item_id}' not found in '{db}/{container}'"
                )
            self.store.execute(
                "DELETE FROM cosmos_items "
                "WHERE db=? AND container=? AND id=?",
                (db, container, str(item_id)),
            )

    def list_items(self, db: str, container: str,
                   partition_key: Optional[str] = None) -> list[dict]:
        self._require_container(db, container)
        if partition_key is not None:
            rows = self.store.query(
                "SELECT body FROM cosmos_items "
                "WHERE db=? AND container=? AND partition_key=? "
                "ORDER BY last_modified",
                (db, container, str(partition_key)),
            )
        else:
            rows = self.store.query(
                "SELECT body FROM cosmos_items "
                "WHERE db=? AND container=? ORDER BY last_modified",
                (db, container),
            )
        return [json.loads(r["body"]) for r in rows]

    # ------------------------------------------------------------------
    # Query (SQL subset)
    # ------------------------------------------------------------------
    def query_items(self, db: str, container: str,
                    sql: str,
                    partition_key: Optional[str] = None) -> list[dict]:
        """Execute a SQL-subset query against the container's items."""
        items = self.list_items(db, container, partition_key)
        return _run_query(items, sql)
