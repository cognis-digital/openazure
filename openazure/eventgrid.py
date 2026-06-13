"""Azure Event Grid emulation.

Supports:
* Custom Topics — create, delete, list; configured with a schema
  (EventGridSchema by default; CloudEvents schema is recorded but not
  enforced — we accept both).
* Event Subscriptions — per-topic subscriptions with an endpoint URL
  (stored but not called in this local emulation), event-type filters,
  subject prefix/suffix filters, and property filters.
* Publish events — validate against CloudEvents / EventGrid envelope;
  fan out to matching subscriptions (filtering by event type, subject
  prefix/suffix, and custom property equality).
* Event store — published events are stored in SQLite for replay /
  inspection; ``list_events`` returns them for a given topic, with
  optional type and subscription filters.

This implementation intentionally does NOT make outbound HTTP calls to
subscription endpoints — it fans events to an in-process store that
tests and tools can inspect via ``list_events``.
"""

from __future__ import annotations

import json
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class EventGridService:
    """Local emulation of Azure Event Grid."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eg_topics (
                name TEXT PRIMARY KEY,
                schema TEXT NOT NULL DEFAULT 'EventGridSchema',
                created TEXT NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eg_subscriptions (
                topic TEXT NOT NULL,
                name TEXT NOT NULL,
                endpoint_url TEXT,
                event_types TEXT NOT NULL DEFAULT '[]',
                subject_begins_with TEXT,
                subject_ends_with TEXT,
                property_filters TEXT NOT NULL DEFAULT '{}',
                created TEXT NOT NULL,
                PRIMARY KEY (topic, name)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS eg_events (
                rowkey TEXT PRIMARY KEY,
                id TEXT NOT NULL,
                topic TEXT NOT NULL,
                subscription TEXT,
                event_type TEXT NOT NULL,
                subject TEXT NOT NULL,
                event_time TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}',
                data_version TEXT,
                source TEXT,
                raw_event TEXT NOT NULL
            )
        """)

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------
    def create_topic(self, name: str, *,
                     schema: str = "EventGridSchema") -> dict:
        if self.store.query("SELECT name FROM eg_topics WHERE name=?", (name,)):
            raise Conflict(f"Event Grid topic '{name}' already exists")
        self.store.execute(
            "INSERT INTO eg_topics VALUES (?,?,?)",
            (name, schema, _now_iso()),
        )
        return {"name": name, "schema": schema}

    def delete_topic(self, name: str) -> None:
        if not self.store.query("SELECT name FROM eg_topics WHERE name=?", (name,)):
            raise NotFound(f"Event Grid topic '{name}' not found")
        self.store.execute(
            "DELETE FROM eg_events WHERE topic=?", (name,)
        )
        self.store.execute(
            "DELETE FROM eg_subscriptions WHERE topic=?", (name,)
        )
        self.store.execute("DELETE FROM eg_topics WHERE name=?", (name,))

    def list_topics(self) -> list[str]:
        rows = self.store.query("SELECT name FROM eg_topics ORDER BY name")
        return [r["name"] for r in rows]

    def get_topic(self, name: str) -> dict:
        rows = self.store.query("SELECT * FROM eg_topics WHERE name=?", (name,))
        if not rows:
            raise NotFound(f"Event Grid topic '{name}' not found")
        r = rows[0]
        return {"name": r["name"], "schema": r["schema"], "created": r["created"]}

    def _req_topic(self, name: str) -> dict:
        return self.get_topic(name)

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    def create_subscription(self, topic: str, name: str, *,
                             endpoint_url: str | None = None,
                             event_types: list[str] | None = None,
                             subject_begins_with: str | None = None,
                             subject_ends_with: str | None = None,
                             property_filters: dict | None = None) -> dict:
        self._req_topic(topic)
        if self.store.query(
            "SELECT name FROM eg_subscriptions WHERE topic=? AND name=?",
            (topic, name),
        ):
            raise Conflict(
                f"Subscription '{name}' on topic '{topic}' already exists"
            )
        et_json = json.dumps(event_types or [])
        pf_json = json.dumps(property_filters or {})
        self.store.execute(
            "INSERT INTO eg_subscriptions VALUES (?,?,?,?,?,?,?,?)",
            (topic, name, endpoint_url, et_json,
             subject_begins_with, subject_ends_with, pf_json, _now_iso()),
        )
        return self._sub_dict(topic, name)

    def delete_subscription(self, topic: str, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM eg_subscriptions WHERE topic=? AND name=?",
            (topic, name),
        ):
            raise NotFound(
                f"Subscription '{name}' on topic '{topic}' not found"
            )
        self.store.execute(
            "DELETE FROM eg_subscriptions WHERE topic=? AND name=?",
            (topic, name),
        )

    def list_subscriptions(self, topic: str) -> list[dict]:
        self._req_topic(topic)
        rows = self.store.query(
            "SELECT name FROM eg_subscriptions WHERE topic=? ORDER BY name",
            (topic,),
        )
        return [self._sub_dict(topic, r["name"]) for r in rows]

    def get_subscription(self, topic: str, name: str) -> dict:
        if not self.store.query(
            "SELECT name FROM eg_subscriptions WHERE topic=? AND name=?",
            (topic, name),
        ):
            raise NotFound(
                f"Subscription '{name}' on topic '{topic}' not found"
            )
        return self._sub_dict(topic, name)

    def _sub_dict(self, topic: str, name: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM eg_subscriptions WHERE topic=? AND name=?",
            (topic, name),
        )
        if not rows:
            raise NotFound(f"Subscription '{name}' not found")
        r = rows[0]
        return {
            "topic": r["topic"],
            "name": r["name"],
            "endpoint_url": r["endpoint_url"],
            "event_types": json.loads(r["event_types"] or "[]"),
            "subject_begins_with": r["subject_begins_with"],
            "subject_ends_with": r["subject_ends_with"],
            "property_filters": json.loads(r["property_filters"] or "{}"),
            "created": r["created"],
        }

    # ------------------------------------------------------------------
    # Publishing events
    # ------------------------------------------------------------------
    def publish(self, topic: str, events: list[dict]) -> dict:
        """Publish a list of events to a topic.

        Each event may be in EventGridSchema or CloudEvents 1.0 format.
        Returns the number of events successfully stored.

        EventGridSchema required fields: id, eventType, subject, data, eventTime
        CloudEvents required fields: id, type, source, subject
        """
        self._req_topic(topic)
        if not events:
            raise BadRequest("events list must not be empty")
        subs = self._load_subscriptions(topic)
        stored = 0
        for ev in events:
            norm = self._normalise_event(ev, topic)
            # Fan out to matching subscriptions
            matched_subs: list[str] = []
            for sub in subs:
                if self._matches(norm, sub):
                    matched_subs.append(sub["name"])
                    self._store_event(norm, topic, sub["name"])
            if not matched_subs:
                # Still store with subscription=None (for inspection)
                self._store_event(norm, topic, None)
            stored += 1
        return {"stored": stored, "topic": topic}

    def _normalise_event(self, ev: dict, topic: str) -> dict:
        """Normalise both EventGrid and CloudEvents schema to a common dict."""
        # CloudEvents schema uses 'type' and 'source'
        if "type" in ev and "source" in ev:
            return {
                "id": ev.get("id") or uuid.uuid4().hex,
                "event_type": ev["type"],
                "subject": ev.get("subject", ""),
                "event_time": ev.get("time", _now_iso()),
                "data": ev.get("data", {}),
                "data_version": ev.get("datacontenttype", ""),
                "source": ev.get("source", ""),
                "raw_event": json.dumps(ev),
            }
        # EventGrid schema
        if "eventType" not in ev:
            raise BadRequest("Event must have 'eventType' or 'type' field")
        if "subject" not in ev:
            raise BadRequest("Event must have 'subject'")
        return {
            "id": ev.get("id") or uuid.uuid4().hex,
            "event_type": ev["eventType"],
            "subject": ev.get("subject", ""),
            "event_time": ev.get("eventTime", _now_iso()),
            "data": ev.get("data", {}),
            "data_version": ev.get("dataVersion", ""),
            "source": ev.get("topic", topic),
            "raw_event": json.dumps(ev),
        }

    def _store_event(self, norm: dict, topic: str, subscription: str | None):
        import uuid as _uuid
        rowkey = _uuid.uuid4().hex
        self.store.execute(
            "INSERT INTO eg_events "
            "(rowkey,id,topic,subscription,event_type,subject,event_time,"
            "data,data_version,source,raw_event) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                rowkey, norm["id"], topic, subscription,
                norm["event_type"], norm["subject"], norm["event_time"],
                json.dumps(norm["data"]), norm.get("data_version", ""),
                norm.get("source", ""), norm["raw_event"],
            ),
        )

    def _load_subscriptions(self, topic: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM eg_subscriptions WHERE topic=?", (topic,)
        )
        result = []
        for r in rows:
            result.append({
                "name": r["name"],
                "event_types": json.loads(r["event_types"] or "[]"),
                "subject_begins_with": r["subject_begins_with"],
                "subject_ends_with": r["subject_ends_with"],
                "property_filters": json.loads(r["property_filters"] or "{}"),
            })
        return result

    def _matches(self, norm: dict, sub: dict) -> bool:
        """Return True iff the normalised event matches the subscription filters."""
        # Event-type filter
        et_filter = sub["event_types"]
        if et_filter and norm["event_type"] not in et_filter:
            return False
        # Subject prefix filter
        begins = sub["subject_begins_with"]
        if begins and not norm["subject"].startswith(begins):
            return False
        # Subject suffix filter
        ends = sub["subject_ends_with"]
        if ends and not norm["subject"].endswith(ends):
            return False
        # Property filters (equality on data fields)
        pf = sub["property_filters"]
        if pf:
            data = norm["data"] if isinstance(norm["data"], dict) else {}
            for k, v in pf.items():
                if data.get(k) != v:
                    return False
        return True

    # ------------------------------------------------------------------
    # Querying stored events
    # ------------------------------------------------------------------
    def list_events(self, topic: str, *,
                    subscription: str | None = None,
                    event_type: str | None = None,
                    limit: int = 100) -> list[dict]:
        """Return stored events for a topic, with optional filters."""
        self._req_topic(topic)
        if subscription is not None:
            rows = self.store.query(
                "SELECT * FROM eg_events WHERE topic=? AND subscription=? "
                "ORDER BY event_time LIMIT ?",
                (topic, subscription, limit),
            )
        elif event_type is not None:
            rows = self.store.query(
                "SELECT * FROM eg_events WHERE topic=? AND event_type=? "
                "ORDER BY event_time LIMIT ?",
                (topic, event_type, limit),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM eg_events WHERE topic=? "
                "ORDER BY event_time LIMIT ?",
                (topic, limit),
            )
        return [
            {
                "id": r["id"],
                "topic": r["topic"],
                "subscription": r["subscription"],
                "event_type": r["event_type"],
                "subject": r["subject"],
                "event_time": r["event_time"],
                "data": json.loads(r["data"] or "{}"),
                "data_version": r["data_version"],
                "source": r["source"],
            }
            for r in rows
        ]
