"""Azure Event Hubs emulation.

Supports:
* Namespaces (logical grouping; one namespace per Store, but the
  operations use namespace name for multi-tenancy).
* Event Hubs — create, delete, list; configurable partition count.
* Partitions — list, get partition properties (start/end sequence number,
  last enqueued time).
* Consumer groups — create, delete, list; ``$Default`` is created
  automatically.
* Send events — single event or batch; events routed to a partition
  (by partition key hash or round-robin).
* Receive events — by partition + consumer group, with offset / sequence
  tracking; returns events in order.

Events are stored in ``eh_events`` keyed by (hub, partition, sequence_number).
Consumer group checkpoints are tracked in ``eh_checkpoints``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _partition_for_key(key: str, partition_count: int) -> int:
    """Consistent partition assignment from a partition key."""
    h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
    return h % partition_count


class EventHubsService:
    """Local emulation of Azure Event Hubs."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eh_hubs (
                name TEXT PRIMARY KEY,
                partition_count INTEGER NOT NULL DEFAULT 4,
                message_retention INTEGER NOT NULL DEFAULT 1,
                created TEXT NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eh_consumer_groups (
                hub TEXT NOT NULL,
                name TEXT NOT NULL,
                created TEXT NOT NULL,
                PRIMARY KEY (hub, name)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eh_events (
                id TEXT PRIMARY KEY,
                hub TEXT NOT NULL,
                partition INTEGER NOT NULL,
                sequence_number INTEGER NOT NULL,
                offset INTEGER NOT NULL,
                enqueued_time REAL NOT NULL,
                body TEXT NOT NULL,
                properties TEXT NOT NULL DEFAULT '{}',
                partition_key TEXT
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eh_checkpoints (
                hub TEXT NOT NULL,
                consumer_group TEXT NOT NULL,
                partition INTEGER NOT NULL,
                sequence_number INTEGER NOT NULL DEFAULT -1,
                offset INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (hub, consumer_group, partition)
            )
        """)
        # Per-hub sequence counters (stored in eh_seq)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eh_seq (
                hub TEXT NOT NULL,
                partition INTEGER NOT NULL,
                sequence_number INTEGER NOT NULL DEFAULT 0,
                offset INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hub, partition)
            )
        """)

    # ------------------------------------------------------------------
    # Event Hubs
    # ------------------------------------------------------------------
    def create_hub(self, name: str, *,
                   partition_count: int = 4,
                   message_retention: int = 1) -> dict:
        if partition_count < 1 or partition_count > 32:
            raise BadRequest("partition_count must be between 1 and 32")
        if self.store.query("SELECT name FROM eh_hubs WHERE name=?", (name,)):
            raise Conflict(f"Event Hub '{name}' already exists")
        self.store.execute(
            "INSERT INTO eh_hubs VALUES (?,?,?,?)",
            (name, partition_count, message_retention, _now_iso()),
        )
        # Create $Default consumer group
        self.store.execute(
            "INSERT INTO eh_consumer_groups VALUES (?,?,?)",
            (name, "$Default", _now_iso()),
        )
        # Initialise sequence counters for each partition
        for p in range(partition_count):
            self.store.execute(
                "INSERT OR IGNORE INTO eh_seq VALUES (?,?,0,0)", (name, p)
            )
        return self._hub_info(name)

    def delete_hub(self, name: str) -> None:
        self._req_hub(name)
        self.store.execute("DELETE FROM eh_events WHERE hub=?", (name,))
        self.store.execute("DELETE FROM eh_consumer_groups WHERE hub=?", (name,))
        self.store.execute("DELETE FROM eh_checkpoints WHERE hub=?", (name,))
        self.store.execute("DELETE FROM eh_seq WHERE hub=?", (name,))
        self.store.execute("DELETE FROM eh_hubs WHERE name=?", (name,))

    def list_hubs(self) -> list[str]:
        rows = self.store.query("SELECT name FROM eh_hubs ORDER BY name")
        return [r["name"] for r in rows]

    def get_hub_properties(self, name: str) -> dict:
        self._req_hub(name)
        return self._hub_info(name)

    def _req_hub(self, name: str) -> dict:
        rows = self.store.query("SELECT * FROM eh_hubs WHERE name=?", (name,))
        if not rows:
            raise NotFound(f"Event Hub '{name}' not found")
        return dict(rows[0])

    def _hub_info(self, name: str) -> dict:
        r = self._req_hub(name)
        return {
            "name": r["name"],
            "partition_count": r["partition_count"],
            "message_retention": r["message_retention"],
            "created": r["created"],
        }

    # ------------------------------------------------------------------
    # Partitions
    # ------------------------------------------------------------------
    def list_partitions(self, hub: str) -> list[int]:
        r = self._req_hub(hub)
        return list(range(r["partition_count"]))

    def get_partition_properties(self, hub: str, partition: int) -> dict:
        r = self._req_hub(hub)
        if partition < 0 or partition >= r["partition_count"]:
            raise BadRequest(
                f"Partition {partition} out of range for hub '{hub}'"
            )
        seq_rows = self.store.query(
            "SELECT sequence_number, offset FROM eh_seq WHERE hub=? AND partition=?",
            (hub, partition),
        )
        seq_info = dict(seq_rows[0]) if seq_rows else {"sequence_number": 0, "offset": 0}
        first_rows = self.store.query(
            "SELECT MIN(sequence_number) AS first_seq, "
            "MIN(enqueued_time) AS first_time FROM eh_events "
            "WHERE hub=? AND partition=?",
            (hub, partition),
        )
        last_rows = self.store.query(
            "SELECT MAX(enqueued_time) AS last_time FROM eh_events "
            "WHERE hub=? AND partition=?",
            (hub, partition),
        )
        return {
            "hub": hub,
            "partition": partition,
            "begin_sequence_number": first_rows[0]["first_seq"] or 0,
            "last_sequence_number": seq_info["sequence_number"],
            "last_enqueued_time": last_rows[0]["last_time"],
            "is_empty": seq_info["sequence_number"] == 0,
        }

    # ------------------------------------------------------------------
    # Consumer groups
    # ------------------------------------------------------------------
    def create_consumer_group(self, hub: str, name: str) -> dict:
        self._req_hub(hub)
        if self.store.query(
            "SELECT name FROM eh_consumer_groups WHERE hub=? AND name=?",
            (hub, name),
        ):
            raise Conflict(
                f"Consumer group '{name}' on hub '{hub}' already exists"
            )
        self.store.execute(
            "INSERT INTO eh_consumer_groups VALUES (?,?,?)",
            (hub, name, _now_iso()),
        )
        return {"hub": hub, "name": name}

    def delete_consumer_group(self, hub: str, name: str) -> None:
        if name == "$Default":
            raise BadRequest("Cannot delete the $Default consumer group")
        if not self.store.query(
            "SELECT name FROM eh_consumer_groups WHERE hub=? AND name=?",
            (hub, name),
        ):
            raise NotFound(f"Consumer group '{name}' on hub '{hub}' not found")
        self.store.execute(
            "DELETE FROM eh_checkpoints WHERE hub=? AND consumer_group=?",
            (hub, name),
        )
        self.store.execute(
            "DELETE FROM eh_consumer_groups WHERE hub=? AND name=?",
            (hub, name),
        )

    def list_consumer_groups(self, hub: str) -> list[str]:
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT name FROM eh_consumer_groups WHERE hub=? ORDER BY name",
            (hub,),
        )
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------
    # Sending events
    # ------------------------------------------------------------------
    def send_event(self, hub: str, body: str, *,
                   partition_key: str | None = None,
                   partition: int | None = None,
                   properties: dict | None = None) -> dict:
        r = self._req_hub(hub)
        pc = r["partition_count"]
        if partition is not None:
            if partition < 0 or partition >= pc:
                raise BadRequest(
                    f"Partition {partition} out of range for hub '{hub}'"
                )
            target = partition
        elif partition_key is not None:
            target = _partition_for_key(partition_key, pc)
        else:
            # Round-robin: use current total event count mod pc
            rows = self.store.query(
                "SELECT SUM(sequence_number) AS s FROM eh_seq WHERE hub=?",
                (hub,),
            )
            total = rows[0]["s"] or 0
            target = int(total) % pc
        return self._store_event(hub, target, body, partition_key, properties or {})

    def send_batch(self, hub: str, events: list[dict], *,
                   partition_key: str | None = None,
                   partition: int | None = None) -> list[dict]:
        """Send multiple events; all go to the same partition."""
        results = []
        for ev in events:
            body = ev.get("body", "")
            pk = ev.get("partition_key", partition_key)
            props = ev.get("properties", {})
            results.append(
                self.send_event(hub, body,
                                partition_key=pk, partition=partition,
                                properties=props)
            )
        return results

    def _store_event(self, hub: str, partition: int, body: str,
                     partition_key: str | None,
                     properties: dict) -> dict:
        eid = uuid.uuid4().hex
        now = _now()
        with self.store.lock:
            seq_rows = self.store.conn.execute(
                "SELECT sequence_number, offset FROM eh_seq WHERE hub=? AND partition=?",
                (hub, partition),
            ).fetchall()
            if seq_rows:
                seq = seq_rows[0]["sequence_number"] + 1
                off = seq_rows[0]["offset"] + len(body.encode("utf-8"))
            else:
                seq, off = 1, len(body.encode("utf-8"))
            self.store.conn.execute(
                "INSERT INTO eh_events VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, hub, partition, seq, off, now,
                 body, json.dumps(properties), partition_key),
            )
            self.store.conn.execute(
                "INSERT OR REPLACE INTO eh_seq VALUES (?,?,?,?)",
                (hub, partition, seq, off),
            )
            self.store.conn.commit()
        return {
            "event_id": eid,
            "hub": hub,
            "partition": partition,
            "sequence_number": seq,
            "offset": off,
            "enqueued_time": now,
        }

    # ------------------------------------------------------------------
    # Receiving events
    # ------------------------------------------------------------------
    def receive_events(self, hub: str, partition: int,
                       consumer_group: str = "$Default",
                       max_events: int = 100,
                       starting_sequence: int | None = None) -> list[dict]:
        """Receive events from a partition, advancing the consumer group checkpoint."""
        r = self._req_hub(hub)
        if partition < 0 or partition >= r["partition_count"]:
            raise BadRequest(
                f"Partition {partition} out of range for hub '{hub}'"
            )
        if not self.store.query(
            "SELECT name FROM eh_consumer_groups WHERE hub=? AND name=?",
            (hub, consumer_group),
        ):
            raise NotFound(
                f"Consumer group '{consumer_group}' not found on hub '{hub}'"
            )
        # Determine starting sequence
        if starting_sequence is None:
            cp_rows = self.store.query(
                "SELECT sequence_number FROM eh_checkpoints "
                "WHERE hub=? AND consumer_group=? AND partition=?",
                (hub, consumer_group, partition),
            )
            start_seq = cp_rows[0]["sequence_number"] if cp_rows else 0
        else:
            start_seq = starting_sequence - 1  # inclusive from given

        rows = self.store.query(
            "SELECT * FROM eh_events "
            "WHERE hub=? AND partition=? AND sequence_number>? "
            "ORDER BY sequence_number LIMIT ?",
            (hub, partition, start_seq, max_events),
        )
        events = []
        max_seq = start_seq
        for r in rows:
            events.append({
                "event_id": r["id"],
                "hub": hub,
                "partition": partition,
                "sequence_number": r["sequence_number"],
                "offset": r["offset"],
                "enqueued_time": r["enqueued_time"],
                "body": r["body"],
                "properties": json.loads(r["properties"] or "{}"),
                "partition_key": r["partition_key"],
            })
            if r["sequence_number"] > max_seq:
                max_seq = r["sequence_number"]
        # Update checkpoint
        if events:
            self.store.execute(
                "INSERT OR REPLACE INTO eh_checkpoints VALUES (?,?,?,?,?)",
                (hub, consumer_group, partition, max_seq,
                 rows[-1]["offset"]),
            )
        return events

    def get_checkpoint(self, hub: str, consumer_group: str,
                       partition: int) -> dict:
        rows = self.store.query(
            "SELECT * FROM eh_checkpoints "
            "WHERE hub=? AND consumer_group=? AND partition=?",
            (hub, consumer_group, partition),
        )
        if not rows:
            return {"hub": hub, "consumer_group": consumer_group,
                    "partition": partition,
                    "sequence_number": -1, "offset": -1}
        r = rows[0]
        return {
            "hub": hub,
            "consumer_group": consumer_group,
            "partition": partition,
            "sequence_number": r["sequence_number"],
            "offset": r["offset"],
        }

    def update_checkpoint(self, hub: str, consumer_group: str,
                          partition: int, sequence_number: int,
                          offset: int) -> dict:
        self.store.execute(
            "INSERT OR REPLACE INTO eh_checkpoints VALUES (?,?,?,?,?)",
            (hub, consumer_group, partition, sequence_number, offset),
        )
        return self.get_checkpoint(hub, consumer_group, partition)
