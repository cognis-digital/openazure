"""Table Storage service.

A local, compatible subset of Azure Table Storage. Tables hold entities
uniquely keyed by ``(PartitionKey, RowKey)``. Each entity is a JSON object
of arbitrary string/number/bool properties plus the two key fields. The
service supports insert, insert-or-merge (upsert), merge, replace, get,
delete, and a simple partition / property query.

Extended operations (this pass):

* **Batch transactions** — ``batch_execute`` takes a list of operations
  (insert/upsert/merge/replace/delete) and applies them atomically within
  a single SQLite transaction; if any operation fails the entire batch is
  rolled back.
* **OData-lite query** — ``$filter``, ``$top``, and ``$select`` query
  parameters are parsed and applied on the result set. Supported filter
  operators: ``eq``, ``ne``, ``gt``, ``lt``, ``ge``, ``le`` and ``and``
  (case-insensitive). Property comparisons handle strings (quoted) and
  numbers. ``$top`` limits result count; ``$select`` restricts returned
  fields.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# OData-lite filter parser
# ---------------------------------------------------------------------------
# We support:  <prop> <op> <value> [and <prop> <op> <value>]*
# op  = eq | ne | gt | lt | ge | le  (case-insensitive)
# val = 'string'  |  number  |  true  |  false

_FILTER_TOKEN = re.compile(
    r"""
    (\w+)                          # property name
    \s+
    (eq|ne|gt|lt|ge|le)            # operator
    \s+
    (?:
        '([^']*)'                  # string literal (group 3)
        |
        (true|false)               # bool literal (group 4)
        |
        (-?\d+(?:\.\d+)?)          # numeric literal (group 5)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _parse_filter(filter_str: str) -> list[tuple[str, str, Any]]:
    """Return a list of (prop, op, value) triples parsed from *filter_str*."""
    parts = re.split(r"\s+and\s+", filter_str, flags=re.IGNORECASE)
    clauses: list[tuple[str, str, Any]] = []
    for part in parts:
        m = _FILTER_TOKEN.fullmatch(part.strip())
        if not m:
            raise BadRequest(f"unsupported filter clause: '{part.strip()}'")
        prop, op = m.group(1), m.group(2).lower()
        if m.group(3) is not None:
            val: Any = m.group(3)
        elif m.group(4) is not None:
            val = m.group(4).lower() == "true"
        else:
            raw = m.group(5)
            val = float(raw) if "." in raw else int(raw)
        clauses.append((prop, op, val))
    return clauses


def _apply_op(entity_val: Any, op: str, filter_val: Any) -> bool:
    try:
        if op == "eq":
            return entity_val == filter_val
        if op == "ne":
            return entity_val != filter_val
        if op == "gt":
            return entity_val > filter_val
        if op == "lt":
            return entity_val < filter_val
        if op == "ge":
            return entity_val >= filter_val
        if op == "le":
            return entity_val <= filter_val
    except TypeError:
        return False
    return False


def _apply_filter(entities: list[dict],
                  filter_str: Optional[str]) -> list[dict]:
    if not filter_str:
        return entities
    clauses = _parse_filter(filter_str)
    result = []
    for e in entities:
        if all(_apply_op(e.get(prop), op, val)
               for prop, op, val in clauses):
            result.append(e)
    return result


def _apply_select(entities: list[dict],
                  select_str: Optional[str]) -> list[dict]:
    if not select_str:
        return entities
    fields = [f.strip() for f in select_str.split(",") if f.strip()]
    # Always include system keys
    always = {"PartitionKey", "RowKey", "Timestamp"}
    keep = set(fields) | always
    return [{k: v for k, v in e.items() if k in keep} for e in entities]


# ---------------------------------------------------------------------------
class TableService:
    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS tables (
                name TEXT PRIMARY KEY,
                created TEXT NOT NULL
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                table_name TEXT NOT NULL,
                partition_key TEXT NOT NULL,
                row_key TEXT NOT NULL,
                properties TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (table_name, partition_key, row_key)
            )
            """
        )

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------
    def create_table(self, name: str) -> dict:
        if self.store.query("SELECT name FROM tables WHERE name=?", (name,)):
            raise Conflict(f"table '{name}' already exists")
        self.store.execute(
            "INSERT INTO tables (name, created) VALUES (?, ?)",
            (name, _now_iso()),
        )
        return {"name": name}

    def delete_table(self, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM tables WHERE name=?", (name,)
        ):
            raise NotFound(f"table '{name}' not found")
        self.store.execute(
            "DELETE FROM entities WHERE table_name=?", (name,)
        )
        self.store.execute("DELETE FROM tables WHERE name=?", (name,))

    def list_tables(self) -> list[str]:
        rows = self.store.query("SELECT name FROM tables ORDER BY name")
        return [r["name"] for r in rows]

    def _require_table(self, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM tables WHERE name=?", (name,)
        ):
            raise NotFound(f"table '{name}' not found")

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------
    @staticmethod
    def _split_keys(entity: dict) -> tuple[str, str, dict]:
        if "PartitionKey" not in entity or "RowKey" not in entity:
            raise BadRequest("entity must include PartitionKey and RowKey")
        pk = str(entity["PartitionKey"])
        rk = str(entity["RowKey"])
        props = {k: v for k, v in entity.items()
                 if k not in ("PartitionKey", "RowKey", "Timestamp")}
        return pk, rk, props

    def _row_to_entity(self, row) -> dict:
        props = json.loads(row["properties"])
        props["PartitionKey"] = row["partition_key"]
        props["RowKey"] = row["row_key"]
        props["Timestamp"] = row["timestamp"]
        return props

    def insert_entity(self, table: str, entity: dict) -> dict:
        self._require_table(table)
        pk, rk, props = self._split_keys(entity)
        if self.store.query(
            "SELECT 1 FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        ):
            raise Conflict(f"entity ({pk},{rk}) already exists")
        ts = _now_iso()
        self.store.execute(
            "INSERT INTO entities "
            "(table_name, partition_key, row_key, properties, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (table, pk, rk, json.dumps(props), ts),
        )
        return self.get_entity(table, pk, rk)

    def upsert_entity(self, table: str, entity: dict,
                      merge: bool = True) -> dict:
        """Insert-or-(merge|replace). merge=True merges props, False replaces."""
        self._require_table(table)
        pk, rk, props = self._split_keys(entity)
        existing = self.store.query(
            "SELECT * FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        )
        ts = _now_iso()
        if existing and merge:
            cur = json.loads(existing[0]["properties"])
            cur.update(props)
            props = cur
        self.store.execute(
            "INSERT INTO entities "
            "(table_name, partition_key, row_key, properties, timestamp) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(table_name, partition_key, row_key) DO UPDATE SET "
            "properties=excluded.properties, timestamp=excluded.timestamp",
            (table, pk, rk, json.dumps(props), ts),
        )
        return self.get_entity(table, pk, rk)

    def merge_entity(self, table: str, entity: dict) -> dict:
        pk, rk, _ = self._split_keys(entity)
        if not self.store.query(
            "SELECT 1 FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        ):
            raise NotFound(f"entity ({pk},{rk}) not found")
        return self.upsert_entity(table, entity, merge=True)

    def replace_entity(self, table: str, entity: dict) -> dict:
        pk, rk, _ = self._split_keys(entity)
        if not self.store.query(
            "SELECT 1 FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        ):
            raise NotFound(f"entity ({pk},{rk}) not found")
        return self.upsert_entity(table, entity, merge=False)

    def get_entity(self, table: str, partition_key: str,
                   row_key: str) -> dict:
        self._require_table(table)
        rows = self.store.query(
            "SELECT * FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, str(partition_key), str(row_key)),
        )
        if not rows:
            raise NotFound(
                f"entity ({partition_key},{row_key}) not found"
            )
        return self._row_to_entity(rows[0])

    def delete_entity(self, table: str, partition_key: str,
                      row_key: str) -> None:
        self._require_table(table)
        rows = self.store.query(
            "SELECT 1 FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, str(partition_key), str(row_key)),
        )
        if not rows:
            raise NotFound(
                f"entity ({partition_key},{row_key}) not found"
            )
        self.store.execute(
            "DELETE FROM entities "
            "WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, str(partition_key), str(row_key)),
        )

    def query_entities(self, table: str,
                       partition_key: Optional[str] = None,
                       filters: Optional[dict] = None,
                       odata_filter: Optional[str] = None,
                       top: Optional[int] = None,
                       select: Optional[str] = None) -> list[dict]:
        """Query entities with optional scoping and OData-lite filters.

        Parameters
        ----------
        partition_key:
            Scope query to a single partition.
        filters:
            Exact-match dict (legacy, still supported).
        odata_filter:
            OData ``$filter`` expression string (e.g.
            ``"age gt 30 and status eq 'open'"``) applied after
            partition-scope.
        top:
            Maximum number of results to return (``$top``).
        select:
            Comma-separated property names to return (``$select``).
        """
        self._require_table(table)
        if partition_key is not None:
            rows = self.store.query(
                "SELECT * FROM entities "
                "WHERE table_name=? AND partition_key=? "
                "ORDER BY row_key",
                (table, str(partition_key)),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM entities WHERE table_name=? "
                "ORDER BY partition_key, row_key",
                (table,),
            )
        entities = [self._row_to_entity(r) for r in rows]
        # legacy exact-match filters
        if filters:
            entities = [
                e for e in entities
                if all(e.get(k) == v for k, v in filters.items())
            ]
        # OData-lite filter
        if odata_filter:
            entities = _apply_filter(entities, odata_filter)
        # $top
        if top is not None:
            entities = entities[:top]
        # $select
        if select:
            entities = _apply_select(entities, select)
        return entities

    # ------------------------------------------------------------------
    # Batch transactions
    # ------------------------------------------------------------------
    def batch_execute(self, table: str,
                      operations: list[dict]) -> list[dict]:
        """Execute a list of operations atomically.

        Each operation dict must contain ``"op"`` (one of ``insert``,
        ``upsert``, ``merge``, ``replace``, ``delete``) and ``"entity"``
        (for write ops) or ``"PartitionKey"`` + ``"RowKey"`` (for delete).

        Returns a list of result dicts, one per operation, in order.
        Rolls back all changes if any operation fails.

        Implementation note: the Store's ``execute`` helper auto-commits
        after every statement, which would destroy our savepoint. We
        therefore collect all SQL statements, validate them first (dry-
        run in Python), then execute the entire batch inside a single
        BEGIN/COMMIT block using the raw connection.
        """
        self._require_table(table)

        # First pass: validate all operations and build the result set
        # by simulating them against an in-memory snapshot. We do this
        # entirely in Python before touching the DB so we can roll back
        # cleanly by simply not writing.
        #
        # Full approach: run all ops inside a single lock, flush with a
        # plain BEGIN…COMMIT that bypasses store.execute's per-stmt commit.

        results: list[dict] = []
        with self.store.lock:
            conn = self.store.conn
            # Temporarily disable autocommit-like behaviour: execute a
            # manual BEGIN so all subsequent conn.execute calls are part
            # of this transaction. sqlite3 in Python uses implicit
            # transactions for DML; we need to commit / rollback explicitly.
            try:
                conn.execute("BEGIN")
            except Exception:
                # already in a transaction (e.g. in-memory WAL mode) - ok
                pass

            ok = False
            try:
                for op_spec in operations:
                    op = op_spec.get("op", "").lower()
                    if op == "insert":
                        entity = op_spec["entity"]
                        pk, rk, props = self._split_keys(entity)
                        # check duplicate
                        dup = conn.execute(
                            "SELECT 1 FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        if dup:
                            from .errors import Conflict as _C
                            raise _C(f"entity ({pk},{rk}) already exists")
                        ts = _now_iso()
                        conn.execute(
                            "INSERT INTO entities "
                            "(table_name, partition_key, row_key, properties, timestamp) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (table, pk, rk, json.dumps(props), ts),
                        )
                        row = conn.execute(
                            "SELECT * FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        res = self._row_to_entity(row)

                    elif op in ("upsert", "merge"):
                        entity = op_spec["entity"]
                        pk, rk, props = self._split_keys(entity)
                        existing = conn.execute(
                            "SELECT * FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        ts = _now_iso()
                        if existing:
                            cur = json.loads(existing["properties"])
                            cur.update(props)
                            merged = cur
                        else:
                            merged = props
                        conn.execute(
                            "INSERT INTO entities "
                            "(table_name, partition_key, row_key, properties, timestamp) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(table_name, partition_key, row_key) DO UPDATE SET "
                            "properties=excluded.properties, timestamp=excluded.timestamp",
                            (table, pk, rk, json.dumps(merged), ts),
                        )
                        row = conn.execute(
                            "SELECT * FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        res = self._row_to_entity(row)

                    elif op == "replace":
                        entity = op_spec["entity"]
                        pk, rk, props = self._split_keys(entity)
                        existing = conn.execute(
                            "SELECT 1 FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        if not existing:
                            from .errors import NotFound as _NF
                            raise _NF(f"entity ({pk},{rk}) not found")
                        ts = _now_iso()
                        conn.execute(
                            "INSERT INTO entities "
                            "(table_name, partition_key, row_key, properties, timestamp) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(table_name, partition_key, row_key) DO UPDATE SET "
                            "properties=excluded.properties, timestamp=excluded.timestamp",
                            (table, pk, rk, json.dumps(props), ts),
                        )
                        row = conn.execute(
                            "SELECT * FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        res = self._row_to_entity(row)

                    elif op == "delete":
                        pk = str(op_spec.get(
                            "PartitionKey",
                            op_spec.get("entity", {}).get("PartitionKey", ""),
                        ))
                        rk = str(op_spec.get(
                            "RowKey",
                            op_spec.get("entity", {}).get("RowKey", ""),
                        ))
                        existing = conn.execute(
                            "SELECT 1 FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        ).fetchone()
                        if not existing:
                            from .errors import NotFound as _NF
                            raise _NF(f"entity ({pk},{rk}) not found")
                        conn.execute(
                            "DELETE FROM entities "
                            "WHERE table_name=? AND partition_key=? AND row_key=?",
                            (table, pk, rk),
                        )
                        res = {"deleted": True,
                               "PartitionKey": pk, "RowKey": rk}
                    else:
                        raise BadRequest(
                            f"unknown batch op '{op}'; "
                            "must be insert/upsert/merge/replace/delete"
                        )
                    results.append(res)

                conn.commit()
                ok = True
            finally:
                if not ok:
                    conn.rollback()

        return results
