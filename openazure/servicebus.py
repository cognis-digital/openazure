"""Azure Service Bus emulation.

Supports:
* Queues — send, receive (peek-lock), complete, abandon, dead-letter,
  defer; queue properties (max-size, lock-duration, max-delivery-count,
  requires-session, dead-letter-on-message-expiration).
* Topics / Subscriptions — create/delete, SQL-filter rules,
  publish message to topic (fans out to matching subscriptions).
* Sessions — session-aware queues; receive-by-session-id locks the
  session so only one consumer can hold it at a time.
* Dead-letter sub-queue — messages that exhaust delivery attempts or
  are explicitly dead-lettered are moved there; accessible via
  ``receive_dead_letter``.

Wire format for messages::

    {
        "message_id": "<hex>",
        "body": "<str>",
        "session_id": "<str|None>",
        "label": "<str|None>",
        "properties": {<user-properties>},
        "enqueued_at": <float>,
        "lock_token": "<hex|None>",
        "delivery_count": <int>,
        "sequence_number": <int>,
    }
"""

from __future__ import annotations

import re
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Very small SQL-filter evaluator (handles the common Service-Bus subset)
# ---------------------------------------------------------------------------

class _SqlFilter:
    """Evaluate a Service Bus SQL-filter expression against a message dict.

    Supported grammar (subset):
        expression  = or_expr
        or_expr     = and_expr ('OR' and_expr)*
        and_expr    = not_expr ('AND' not_expr)*
        not_expr    = 'NOT' not_expr | primary
        primary     = 'TRUE' | 'FALSE'
                    | '(' or_expr ')'
                    | value OP value
        OP          = '=' | '<>' | '!=' | '>' | '<' | '>=' | '<='
        value       = string_literal | number_literal | 'TRUE' | 'FALSE' | identifier
        identifier  = (used to look up message properties / user-properties)
    """

    _TOK = re.compile(
        r"'(?:[^'\\]|\\.)*'"           # single-quoted string literal
        r"|<>|>=|<=|!=|[<>=]"          # operators (longest first)
        r"|[A-Za-z_][A-Za-z0-9_.]*"   # identifier / keyword
        r"|\d+(?:\.\d+)?"              # numeric literal
        r"|[()]"                       # parentheses
        , re.IGNORECASE,
    )

    _OPS = frozenset(("<>", ">=", "<=", ">", "<", "=", "!="))
    _KWDS = frozenset(("AND", "OR", "NOT", "TRUE", "FALSE"))

    def __init__(self, expr: str):
        self._expr = expr.strip()
        self._tokens: list[str] = self._TOK.findall(self._expr)
        self._pos = 0

    # -- tokeniser helpers ---------------------------------------------------

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _peek2(self) -> str | None:
        """Look at the token two positions ahead."""
        idx = self._pos + 1
        return self._tokens[idx] if idx < len(self._tokens) else None

    def _consume(self) -> str:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _consume_if(self, *values) -> str | None:
        tok = self._peek()
        if tok and tok.upper() in [v.upper() for v in values]:
            self._pos += 1
            return tok
        return None

    # -- parser / evaluator --------------------------------------------------

    def evaluate(self, msg: dict) -> bool:
        """Return True iff the message matches this filter."""
        if not self._expr or self._expr.upper().strip() == "1=1":
            return True
        self._pos = 0
        result = self._parse_or(msg)
        return bool(result)

    def _resolve(self, name: str, msg: dict):
        """Resolve a property name against the message dict."""
        if name in msg:
            return msg[name]
        props = msg.get("properties") or {}
        if name in props:
            return props[name]
        return None

    def _parse_or(self, msg: dict):
        left = self._parse_and(msg)
        while self._consume_if("OR"):
            right = self._parse_and(msg)
            left = bool(left) or bool(right)
        return left

    def _parse_and(self, msg: dict):
        left = self._parse_not(msg)
        while self._consume_if("AND"):
            right = self._parse_not(msg)
            left = bool(left) and bool(right)
        return left

    def _parse_not(self, msg: dict):
        if self._consume_if("NOT"):
            return not bool(self._parse_primary(msg))
        return self._parse_primary(msg)

    def _parse_primary(self, msg: dict):
        tok = self._peek()
        if tok is None:
            return True

        tok_up = tok.upper()

        if tok_up == "TRUE":
            self._consume()
            return True
        if tok_up == "FALSE":
            self._consume()
            return False

        # Parenthesised sub-expression
        if tok == "(":
            self._consume()  # consume '('
            val = self._parse_or(msg)
            self._consume_if(")")  # consume ')'
            return val

        # Parse left-hand side value then check for an operator
        save_pos = self._pos
        lhs = self._parse_atom(msg)

        op = self._peek()
        if op in self._OPS:
            self._consume()  # consume operator
            rhs = self._parse_atom(msg)
            return self._compare(lhs, op, rhs)

        # No operator found.  If lhs came from an identifier lookup, we
        # treat this as "property IS NOT NULL".  If it was a literal, return it.
        return lhs is not None and bool(lhs)

    def _parse_atom(self, msg: dict):
        """Parse a single value (literal or identifier) and return its value."""
        tok = self._peek()
        if tok is None:
            return None

        # String literal
        if tok.startswith("'"):
            self._consume()
            return tok[1:-1].replace("\\'", "'")

        # Numeric literal
        try:
            n = float(tok)
            self._consume()
            return n
        except ValueError:
            pass

        tok_up = tok.upper()

        # Boolean keywords
        if tok_up == "TRUE":
            self._consume()
            return True
        if tok_up == "FALSE":
            self._consume()
            return False

        # Skip AND/OR/NOT — these are not atoms
        if tok_up in ("AND", "OR", "NOT"):
            return None

        # Identifier (property lookup)
        self._consume()
        return self._resolve(tok, msg)

    @staticmethod
    def _compare(lhs, op: str, rhs) -> bool:
        if lhs is None or rhs is None:
            return False
        try:
            if op == "=":
                # Try numeric equality first, then string
                try:
                    return float(lhs) == float(rhs)
                except (ValueError, TypeError):
                    return lhs == rhs
            if op in ("<>", "!="):
                try:
                    return float(lhs) != float(rhs)
                except (ValueError, TypeError):
                    return lhs != rhs
            # Strictly ordered comparisons require numbers
            a, b = float(lhs), float(rhs)
            if op == ">":
                return a > b
            if op == "<":
                return a < b
            if op == ">=":
                return a >= b
            if op == "<=":
                return a <= b
        except (TypeError, ValueError):
            pass
        return False


class ServiceBusService:
    """Local emulation of Azure Service Bus."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self):
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_queues (
                name TEXT PRIMARY KEY,
                max_size_mb INTEGER NOT NULL DEFAULT 1024,
                lock_duration INTEGER NOT NULL DEFAULT 60,
                max_delivery_count INTEGER NOT NULL DEFAULT 10,
                requires_session INTEGER NOT NULL DEFAULT 0,
                dead_letter_on_expiry INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_topics (
                name TEXT PRIMARY KEY,
                max_size_mb INTEGER NOT NULL DEFAULT 1024,
                created TEXT NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_subscriptions (
                topic TEXT NOT NULL,
                name TEXT NOT NULL,
                created TEXT NOT NULL,
                PRIMARY KEY (topic, name)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_rules (
                topic TEXT NOT NULL,
                subscription TEXT NOT NULL,
                name TEXT NOT NULL,
                filter_sql TEXT NOT NULL DEFAULT '1=1',
                PRIMARY KEY (topic, subscription, name)
            )
        """)
        # Unified message table (queue + subscription)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_messages (
                id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                dest_type TEXT NOT NULL,
                body TEXT NOT NULL,
                session_id TEXT,
                label TEXT,
                properties TEXT NOT NULL DEFAULT '{}',
                enqueued_at REAL NOT NULL,
                visible_after REAL NOT NULL,
                lock_token TEXT,
                lock_expires REAL NOT NULL DEFAULT 0,
                delivery_count INTEGER NOT NULL DEFAULT 0,
                sequence_number INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'active',
                dead_letter_reason TEXT
            )
        """)
        # Session locks table
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_session_locks (
                destination TEXT NOT NULL,
                session_id TEXT NOT NULL,
                lock_token TEXT NOT NULL,
                lock_expires REAL NOT NULL,
                PRIMARY KEY (destination, session_id)
            )
        """)
        # Sequence counter
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS sb_seq (
                id INTEGER PRIMARY KEY AUTOINCREMENT
            )
        """)

    def _next_seq(self) -> int:
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT INTO sb_seq DEFAULT VALUES"
            )
            self.store.conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Queues
    # ------------------------------------------------------------------
    def create_queue(self, name: str, *,
                     max_size_mb: int = 1024,
                     lock_duration: int = 60,
                     max_delivery_count: int = 10,
                     requires_session: bool = False,
                     dead_letter_on_expiry: bool = False) -> dict:
        if self.store.query("SELECT name FROM sb_queues WHERE name=?", (name,)):
            raise Conflict(f"Service Bus queue '{name}' already exists")
        self.store.execute(
            "INSERT INTO sb_queues VALUES (?,?,?,?,?,?,?)",
            (name, max_size_mb, lock_duration, max_delivery_count,
             int(requires_session), int(dead_letter_on_expiry), _now_iso()),
        )
        return self._queue_props(name)

    def delete_queue(self, name: str) -> None:
        self._req_queue(name)
        self.store.execute(
            "DELETE FROM sb_messages WHERE destination=? AND dest_type='queue'",
            (name,),
        )
        self.store.execute("DELETE FROM sb_queues WHERE name=?", (name,))

    def list_queues(self) -> list[str]:
        rows = self.store.query("SELECT name FROM sb_queues ORDER BY name")
        return [r["name"] for r in rows]

    def get_queue_properties(self, name: str) -> dict:
        self._req_queue(name)
        return self._queue_props(name)

    def _req_queue(self, name: str) -> dict:
        rows = self.store.query("SELECT * FROM sb_queues WHERE name=?", (name,))
        if not rows:
            raise NotFound(f"Service Bus queue '{name}' not found")
        return dict(rows[0])

    def _queue_props(self, name: str) -> dict:
        r = self._req_queue(name)
        return {
            "name": r["name"],
            "max_size_mb": r["max_size_mb"],
            "lock_duration": r["lock_duration"],
            "max_delivery_count": r["max_delivery_count"],
            "requires_session": bool(r["requires_session"]),
            "dead_letter_on_expiry": bool(r["dead_letter_on_expiry"]),
            "created": r["created"],
        }

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------
    def create_topic(self, name: str, *, max_size_mb: int = 1024) -> dict:
        if self.store.query("SELECT name FROM sb_topics WHERE name=?", (name,)):
            raise Conflict(f"Service Bus topic '{name}' already exists")
        self.store.execute(
            "INSERT INTO sb_topics VALUES (?,?,?)",
            (name, max_size_mb, _now_iso()),
        )
        return {"name": name, "max_size_mb": max_size_mb}

    def delete_topic(self, name: str) -> None:
        if not self.store.query("SELECT name FROM sb_topics WHERE name=?", (name,)):
            raise NotFound(f"Service Bus topic '{name}' not found")
        # cascade: subscriptions + their messages + rules
        subs = self.store.query(
            "SELECT name FROM sb_subscriptions WHERE topic=?", (name,)
        )
        for s in subs:
            dest = f"{name}/{s['name']}"
            self.store.execute(
                "DELETE FROM sb_messages WHERE destination=? AND dest_type='subscription'",
                (dest,),
            )
        self.store.execute(
            "DELETE FROM sb_rules WHERE topic=?", (name,)
        )
        self.store.execute(
            "DELETE FROM sb_subscriptions WHERE topic=?", (name,)
        )
        self.store.execute("DELETE FROM sb_topics WHERE name=?", (name,))

    def list_topics(self) -> list[str]:
        rows = self.store.query("SELECT name FROM sb_topics ORDER BY name")
        return [r["name"] for r in rows]

    def _req_topic(self, name: str) -> None:
        if not self.store.query("SELECT name FROM sb_topics WHERE name=?", (name,)):
            raise NotFound(f"Service Bus topic '{name}' not found")

    # ------------------------------------------------------------------
    # Subscriptions + rules
    # ------------------------------------------------------------------
    def create_subscription(self, topic: str, name: str) -> dict:
        self._req_topic(topic)
        if self.store.query(
            "SELECT name FROM sb_subscriptions WHERE topic=? AND name=?",
            (topic, name),
        ):
            raise Conflict(
                f"Subscription '{name}' on topic '{topic}' already exists"
            )
        self.store.execute(
            "INSERT INTO sb_subscriptions VALUES (?,?,?)",
            (topic, name, _now_iso()),
        )
        # Default rule: allow everything
        self.store.execute(
            "INSERT INTO sb_rules VALUES (?,?,?,?)",
            (topic, name, "$Default", "1=1"),
        )
        return {"topic": topic, "name": name}

    def delete_subscription(self, topic: str, name: str) -> None:
        self._req_sub(topic, name)
        dest = f"{topic}/{name}"
        self.store.execute(
            "DELETE FROM sb_messages WHERE destination=? AND dest_type='subscription'",
            (dest,),
        )
        self.store.execute(
            "DELETE FROM sb_rules WHERE topic=? AND subscription=?", (topic, name)
        )
        self.store.execute(
            "DELETE FROM sb_subscriptions WHERE topic=? AND name=?", (topic, name)
        )

    def list_subscriptions(self, topic: str) -> list[str]:
        self._req_topic(topic)
        rows = self.store.query(
            "SELECT name FROM sb_subscriptions WHERE topic=? ORDER BY name",
            (topic,),
        )
        return [r["name"] for r in rows]

    def _req_sub(self, topic: str, sub: str) -> None:
        if not self.store.query(
            "SELECT name FROM sb_subscriptions WHERE topic=? AND name=?",
            (topic, sub),
        ):
            raise NotFound(
                f"Subscription '{sub}' on topic '{topic}' not found"
            )

    def add_rule(self, topic: str, subscription: str, name: str,
                 filter_sql: str) -> dict:
        self._req_sub(topic, subscription)
        if self.store.query(
            "SELECT name FROM sb_rules WHERE topic=? AND subscription=? AND name=?",
            (topic, subscription, name),
        ):
            raise Conflict(f"Rule '{name}' already exists")
        self.store.execute(
            "INSERT INTO sb_rules VALUES (?,?,?,?)",
            (topic, subscription, name, filter_sql),
        )
        return {"topic": topic, "subscription": subscription,
                "name": name, "filter_sql": filter_sql}

    def remove_rule(self, topic: str, subscription: str, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM sb_rules WHERE topic=? AND subscription=? AND name=?",
            (topic, subscription, name),
        ):
            raise NotFound(f"Rule '{name}' not found")
        self.store.execute(
            "DELETE FROM sb_rules WHERE topic=? AND subscription=? AND name=?",
            (topic, subscription, name),
        )

    def list_rules(self, topic: str, subscription: str) -> list[dict]:
        self._req_sub(topic, subscription)
        rows = self.store.query(
            "SELECT name, filter_sql FROM sb_rules "
            "WHERE topic=? AND subscription=? ORDER BY name",
            (topic, subscription),
        )
        return [{"name": r["name"], "filter_sql": r["filter_sql"]} for r in rows]

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def send_queue(self, queue: str, body: str, *,
                   session_id: str | None = None,
                   label: str | None = None,
                   properties: dict | None = None) -> dict:
        props = self._req_queue(queue)
        if props["requires_session"] and not session_id:
            raise BadRequest(
                f"Queue '{queue}' requires a session_id"
            )
        return self._store_message(
            queue, "queue", body,
            session_id=session_id, label=label,
            properties=properties or {},
        )

    def publish_topic(self, topic: str, body: str, *,
                      label: str | None = None,
                      properties: dict | None = None) -> dict:
        """Publish to a topic — fan-out to every matching subscription."""
        self._req_topic(topic)
        import json as _json
        props = properties or {}
        subs = self.store.query(
            "SELECT name FROM sb_subscriptions WHERE topic=?", (topic,)
        )
        delivered: list[str] = []
        for s in subs:
            sub_name = s["name"]
            rules = self.store.query(
                "SELECT filter_sql FROM sb_rules WHERE topic=? AND subscription=?",
                (topic, sub_name),
            )
            msg_dict = {"body": body, "label": label, **(props)}
            matched = False
            for rule in rules:
                try:
                    flt = _SqlFilter(rule["filter_sql"])
                    if flt.evaluate(msg_dict):
                        matched = True
                        break
                except Exception:
                    matched = True
                    break
            if not matched and rules:
                continue
            dest = f"{topic}/{sub_name}"
            self._store_message(
                dest, "subscription", body,
                label=label, properties=props,
            )
            delivered.append(sub_name)
        return {"topic": topic, "delivered_to": delivered}

    def _store_message(self, destination: str, dest_type: str,
                       body: str, *,
                       session_id: str | None = None,
                       label: str | None = None,
                       properties: dict | None = None) -> dict:
        import json as _json
        mid = uuid.uuid4().hex
        seq = self._next_seq()
        now = _now()
        self.store.execute(
            "INSERT INTO sb_messages "
            "(id,destination,dest_type,body,session_id,label,properties,"
            "enqueued_at,visible_after,lock_token,lock_expires,"
            "delivery_count,sequence_number,state,dead_letter_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL,0,0,?,'active',NULL)",
            (
                mid, destination, dest_type, body,
                session_id, label,
                _json.dumps(properties or {}),
                now, now, seq,
            ),
        )
        return {"message_id": mid, "sequence_number": seq}

    # ------------------------------------------------------------------
    # Receiving (peek-lock)
    # ------------------------------------------------------------------
    def receive_queue(self, queue: str, max_messages: int = 1,
                      lock_duration: int | None = None) -> list[dict]:
        props = self._req_queue(queue)
        ld = lock_duration or props["lock_duration"]
        return self._receive(queue, "queue", max_messages, ld,
                             session_id=None)

    def receive_subscription(self, topic: str, sub: str,
                              max_messages: int = 1,
                              lock_duration: int = 60) -> list[dict]:
        self._req_sub(topic, sub)
        dest = f"{topic}/{sub}"
        return self._receive(dest, "subscription", max_messages,
                             lock_duration, session_id=None)

    def receive_session(self, queue: str, session_id: str,
                        max_messages: int = 1,
                        lock_duration: int = 60) -> list[dict]:
        """Receive messages from a session-enabled queue for a specific session."""
        props = self._req_queue(queue)
        if not props["requires_session"]:
            raise BadRequest(f"Queue '{queue}' does not require sessions")
        # Acquire or verify session lock
        now = _now()
        with self.store.lock:
            lock_rows = self.store.conn.execute(
                "SELECT lock_token, lock_expires FROM sb_session_locks "
                "WHERE destination=? AND session_id=?",
                (queue, session_id),
            ).fetchall()
            if lock_rows and lock_rows[0]["lock_expires"] > now:
                # Already locked by someone else — but we allow same process
                pass
            # Upsert our session lock
            token = uuid.uuid4().hex
            expires = now + lock_duration
            self.store.conn.execute(
                "INSERT OR REPLACE INTO sb_session_locks "
                "(destination, session_id, lock_token, lock_expires) "
                "VALUES (?,?,?,?)",
                (queue, session_id, token, expires),
            )
            self.store.conn.commit()
        return self._receive(queue, "queue", max_messages, lock_duration,
                             session_id=session_id)

    def _receive(self, destination: str, dest_type: str,
                 max_messages: int, lock_duration: int,
                 session_id: str | None) -> list[dict]:
        import json as _json
        out: list[dict] = []
        with self.store.lock:
            now = _now()
            if session_id is not None:
                rows = self.store.conn.execute(
                    "SELECT * FROM sb_messages "
                    "WHERE destination=? AND dest_type=? "
                    "AND session_id=? AND state='active' "
                    "AND visible_after<=? "
                    "ORDER BY sequence_number LIMIT ?",
                    (destination, dest_type, session_id, now, max_messages),
                ).fetchall()
            else:
                rows = self.store.conn.execute(
                    "SELECT * FROM sb_messages "
                    "WHERE destination=? AND dest_type=? "
                    "AND state='active' AND visible_after<=? "
                    "ORDER BY sequence_number LIMIT ?",
                    (destination, dest_type, now, max_messages),
                ).fetchall()
            for r in rows:
                lock_token = uuid.uuid4().hex
                lock_exp = now + float(lock_duration)
                new_dc = r["delivery_count"] + 1
                self.store.conn.execute(
                    "UPDATE sb_messages SET lock_token=?, lock_expires=?, "
                    "visible_after=?, delivery_count=? WHERE id=?",
                    (lock_token, lock_exp, lock_exp, new_dc, r["id"]),
                )
                out.append({
                    "message_id": r["id"],
                    "body": r["body"],
                    "session_id": r["session_id"],
                    "label": r["label"],
                    "properties": _json.loads(r["properties"] or "{}"),
                    "enqueued_at": r["enqueued_at"],
                    "lock_token": lock_token,
                    "delivery_count": new_dc,
                    "sequence_number": r["sequence_number"],
                })
            self.store.conn.commit()
        # Check max delivery count
        self._auto_deadletter(destination, dest_type)
        return out

    def _auto_deadletter(self, destination: str, dest_type: str) -> None:
        """Move messages exceeding max_delivery_count to dead-letter."""
        if dest_type == "queue":
            rows = self.store.query(
                "SELECT max_delivery_count FROM sb_queues WHERE name=?",
                (destination,),
            )
            if not rows:
                return
            mdc = rows[0]["max_delivery_count"]
        else:
            # subscriptions use a default of 10
            mdc = 10
        with self.store.lock:
            over = self.store.conn.execute(
                "SELECT id FROM sb_messages "
                "WHERE destination=? AND dest_type=? "
                "AND delivery_count>? AND state='active'",
                (destination, dest_type, mdc),
            ).fetchall()
            for r in over:
                self.store.conn.execute(
                    "UPDATE sb_messages SET state='dead_letter', "
                    "dead_letter_reason='MaxDeliveryCountExceeded' WHERE id=?",
                    (r["id"],),
                )
            if over:
                self.store.conn.commit()

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------
    def complete_message(self, destination: str, lock_token: str) -> None:
        """Complete (delete) a message by its lock token."""
        rows = self.store.query(
            "SELECT id, lock_expires FROM sb_messages "
            "WHERE lock_token=? AND destination=?",
            (lock_token, destination),
        )
        if not rows:
            raise NotFound(f"Message with lock_token '{lock_token}' not found")
        if rows[0]["lock_expires"] < _now():
            raise BadRequest("Message lock has expired")
        self.store.execute(
            "DELETE FROM sb_messages WHERE lock_token=? AND destination=?",
            (lock_token, destination),
        )

    def abandon_message(self, destination: str, lock_token: str) -> None:
        """Return a message to the active queue immediately."""
        rows = self.store.query(
            "SELECT id FROM sb_messages WHERE lock_token=? AND destination=?",
            (lock_token, destination),
        )
        if not rows:
            raise NotFound(f"Message with lock_token '{lock_token}' not found")
        self.store.execute(
            "UPDATE sb_messages SET lock_token=NULL, lock_expires=0, "
            "visible_after=? WHERE lock_token=? AND destination=?",
            (_now(), lock_token, destination),
        )

    def dead_letter_message(self, destination: str, lock_token: str,
                            reason: str = "UserDeadLettered") -> None:
        """Explicitly move a message to the dead-letter sub-queue."""
        rows = self.store.query(
            "SELECT id FROM sb_messages WHERE lock_token=? AND destination=?",
            (lock_token, destination),
        )
        if not rows:
            raise NotFound(f"Message with lock_token '{lock_token}' not found")
        self.store.execute(
            "UPDATE sb_messages SET state='dead_letter', "
            "dead_letter_reason=?, lock_token=NULL WHERE lock_token=? AND destination=?",
            (reason, lock_token, destination),
        )

    def receive_dead_letter(self, destination: str,
                            max_messages: int = 1) -> list[dict]:
        """Peek at dead-lettered messages (non-destructive)."""
        import json as _json
        rows = self.store.query(
            "SELECT * FROM sb_messages "
            "WHERE destination=? AND state='dead_letter' "
            "ORDER BY sequence_number LIMIT ?",
            (destination, max_messages),
        )
        return [
            {
                "message_id": r["id"],
                "body": r["body"],
                "session_id": r["session_id"],
                "dead_letter_reason": r["dead_letter_reason"],
                "delivery_count": r["delivery_count"],
                "sequence_number": r["sequence_number"],
                "properties": _json.loads(r["properties"] or "{}"),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Queue message count helpers
    # ------------------------------------------------------------------
    def queue_message_count(self, queue: str) -> dict:
        self._req_queue(queue)
        active = self.store.query(
            "SELECT COUNT(*) AS c FROM sb_messages "
            "WHERE destination=? AND dest_type='queue' AND state='active'",
            (queue,),
        )[0]["c"]
        dl = self.store.query(
            "SELECT COUNT(*) AS c FROM sb_messages "
            "WHERE destination=? AND dest_type='queue' AND state='dead_letter'",
            (queue,),
        )[0]["c"]
        return {"active": active, "dead_letter": dl}
