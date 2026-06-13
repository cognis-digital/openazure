"""Queue Storage service.

A local, compatible subset of Azure Queue Storage. Messages are enqueued
into named queues and dequeued with a *visibility timeout*: a dequeued
message becomes invisible to other consumers for ``visibility_timeout``
seconds, during which the consumer holds a ``pop_receipt`` it must present
to delete the message. If the message is not deleted before the timeout
expires it becomes visible again (at-least-once delivery), and its
``dequeue_count`` is incremented.
"""

from __future__ import annotations

import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class QueueService:
    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS queues (
                name TEXT PRIMARY KEY,
                created TEXT NOT NULL
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                queue TEXT NOT NULL,
                body TEXT NOT NULL,
                inserted REAL NOT NULL,
                visible_after REAL NOT NULL,
                dequeue_count INTEGER NOT NULL DEFAULT 0,
                pop_receipt TEXT
            )
            """
        )

    # ------------------------------------------------------------------
    # Queues
    # ------------------------------------------------------------------
    def create_queue(self, name: str) -> dict:
        if self.store.query("SELECT name FROM queues WHERE name=?", (name,)):
            raise Conflict(f"queue '{name}' already exists")
        self.store.execute(
            "INSERT INTO queues (name, created) VALUES (?, ?)",
            (name, _now_iso()),
        )
        return {"name": name}

    def delete_queue(self, name: str) -> None:
        if not self.store.query("SELECT name FROM queues WHERE name=?", (name,)):
            raise NotFound(f"queue '{name}' not found")
        self.store.execute("DELETE FROM messages WHERE queue=?", (name,))
        self.store.execute("DELETE FROM queues WHERE name=?", (name,))

    def list_queues(self) -> list[str]:
        rows = self.store.query("SELECT name FROM queues ORDER BY name")
        return [r["name"] for r in rows]

    def _require_queue(self, name: str) -> None:
        if not self.store.query("SELECT name FROM queues WHERE name=?", (name,)):
            raise NotFound(f"queue '{name}' not found")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def enqueue(self, queue: str, body: str, visibility_delay: float = 0.0) -> dict:
        self._require_queue(queue)
        if not isinstance(body, str):
            body = str(body)
        mid = uuid.uuid4().hex
        now = _now()
        visible_after = now + max(0.0, float(visibility_delay))
        self.store.execute(
            "INSERT INTO messages (id, queue, body, inserted, visible_after, dequeue_count) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (mid, queue, body, now, visible_after),
        )
        return {"id": mid, "queue": queue, "body": body}

    def dequeue(self, queue: str, max_messages: int = 1,
                visibility_timeout: float = 30.0) -> list[dict]:
        """Receive up to ``max_messages`` currently-visible messages and
        make them invisible for ``visibility_timeout`` seconds."""
        self._require_queue(queue)
        if max_messages < 1:
            raise BadRequest("max_messages must be >= 1")
        out: list[dict] = []
        with self.store.lock:
            now = _now()
            rows = self.store.conn.execute(
                "SELECT * FROM messages WHERE queue=? AND visible_after<=? "
                "ORDER BY inserted LIMIT ?",
                (queue, now, max_messages),
            ).fetchall()
            for r in rows:
                receipt = uuid.uuid4().hex
                self.store.conn.execute(
                    "UPDATE messages SET visible_after=?, pop_receipt=?, "
                    "dequeue_count=dequeue_count+1 WHERE id=?",
                    (now + float(visibility_timeout), receipt, r["id"]),
                )
                out.append({
                    "id": r["id"],
                    "queue": queue,
                    "body": r["body"],
                    "pop_receipt": receipt,
                    "dequeue_count": r["dequeue_count"] + 1,
                })
            self.store.conn.commit()
        return out

    def peek(self, queue: str, max_messages: int = 1) -> list[dict]:
        """Return visible messages without changing their visibility."""
        self._require_queue(queue)
        now = _now()
        rows = self.store.query(
            "SELECT * FROM messages WHERE queue=? AND visible_after<=? "
            "ORDER BY inserted LIMIT ?",
            (queue, now, max_messages),
        )
        return [
            {"id": r["id"], "queue": queue, "body": r["body"],
             "dequeue_count": r["dequeue_count"]}
            for r in rows
        ]

    def delete_message(self, queue: str, message_id: str, pop_receipt: str) -> None:
        self._require_queue(queue)
        rows = self.store.query(
            "SELECT pop_receipt FROM messages WHERE id=? AND queue=?",
            (message_id, queue),
        )
        if not rows:
            raise NotFound(f"message '{message_id}' not found")
        if rows[0]["pop_receipt"] != pop_receipt:
            raise BadRequest("pop_receipt mismatch (message visibility may have expired)")
        self.store.execute("DELETE FROM messages WHERE id=?", (message_id,))

    def clear(self, queue: str) -> None:
        self._require_queue(queue)
        self.store.execute("DELETE FROM messages WHERE queue=?", (queue,))

    def count(self, queue: str) -> int:
        """Approximate count of all messages (visible + invisible)."""
        self._require_queue(queue)
        rows = self.store.query(
            "SELECT COUNT(*) AS c FROM messages WHERE queue=?", (queue,)
        )
        return rows[0]["c"]
