"""Table Storage service.

A local, compatible subset of Azure Table Storage. Tables hold entities
uniquely keyed by ``(PartitionKey, RowKey)``. Each entity is a JSON object
of arbitrary string/number/bool properties plus the two key fields. The
service supports insert, insert-or-merge (upsert), merge, replace, get,
delete, and a simple partition / property query.
"""

from __future__ import annotations

import json
import time

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        if not self.store.query("SELECT name FROM tables WHERE name=?", (name,)):
            raise NotFound(f"table '{name}' not found")
        self.store.execute("DELETE FROM entities WHERE table_name=?", (name,))
        self.store.execute("DELETE FROM tables WHERE name=?", (name,))

    def list_tables(self) -> list[str]:
        rows = self.store.query("SELECT name FROM tables ORDER BY name")
        return [r["name"] for r in rows]

    def _require_table(self, name: str) -> None:
        if not self.store.query("SELECT name FROM tables WHERE name=?", (name,)):
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
            "SELECT 1 FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        ):
            raise Conflict(f"entity ({pk},{rk}) already exists")
        ts = _now_iso()
        self.store.execute(
            "INSERT INTO entities (table_name, partition_key, row_key, properties, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (table, pk, rk, json.dumps(props), ts),
        )
        return self.get_entity(table, pk, rk)

    def upsert_entity(self, table: str, entity: dict, merge: bool = True) -> dict:
        """Insert-or-(merge|replace). merge=True merges props, False replaces."""
        self._require_table(table)
        pk, rk, props = self._split_keys(entity)
        existing = self.store.query(
            "SELECT * FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        )
        ts = _now_iso()
        if existing and merge:
            cur = json.loads(existing[0]["properties"])
            cur.update(props)
            props = cur
        self.store.execute(
            "INSERT INTO entities (table_name, partition_key, row_key, properties, timestamp) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(table_name, partition_key, row_key) DO UPDATE SET "
            "properties=excluded.properties, timestamp=excluded.timestamp",
            (table, pk, rk, json.dumps(props), ts),
        )
        return self.get_entity(table, pk, rk)

    def merge_entity(self, table: str, entity: dict) -> dict:
        pk, rk, _ = self._split_keys(entity)
        if not self.store.query(
            "SELECT 1 FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        ):
            raise NotFound(f"entity ({pk},{rk}) not found")
        return self.upsert_entity(table, entity, merge=True)

    def replace_entity(self, table: str, entity: dict) -> dict:
        pk, rk, _ = self._split_keys(entity)
        if not self.store.query(
            "SELECT 1 FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, pk, rk),
        ):
            raise NotFound(f"entity ({pk},{rk}) not found")
        return self.upsert_entity(table, entity, merge=False)

    def get_entity(self, table: str, partition_key: str, row_key: str) -> dict:
        self._require_table(table)
        rows = self.store.query(
            "SELECT * FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, str(partition_key), str(row_key)),
        )
        if not rows:
            raise NotFound(f"entity ({partition_key},{row_key}) not found")
        return self._row_to_entity(rows[0])

    def delete_entity(self, table: str, partition_key: str, row_key: str) -> None:
        self._require_table(table)
        rows = self.store.query(
            "SELECT 1 FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, str(partition_key), str(row_key)),
        )
        if not rows:
            raise NotFound(f"entity ({partition_key},{row_key}) not found")
        self.store.execute(
            "DELETE FROM entities WHERE table_name=? AND partition_key=? AND row_key=?",
            (table, str(partition_key), str(row_key)),
        )

    def query_entities(self, table: str, partition_key: str | None = None,
                       filters: dict | None = None) -> list[dict]:
        """Query entities, optionally scoped to a partition and/or matching
        an exact-match property filter dict."""
        self._require_table(table)
        if partition_key is not None:
            rows = self.store.query(
                "SELECT * FROM entities WHERE table_name=? AND partition_key=? "
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
        if filters:
            entities = [
                e for e in entities
                if all(e.get(k) == v for k, v in filters.items())
            ]
        return entities
