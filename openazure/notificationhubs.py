"""Azure Notification Hubs emulation.

Supports:
* Hub management — create, delete, list hubs.
* Device registrations — register a device (tag set, channel handle, platform),
  update, delete, list (with tag filter).
* Installation API (simplified) — create/update and delete an installation.
* Send notifications — broadcast or tag-expression targeted;
  notifications are captured in a ``nh_sent_notifications`` table for
  test inspection rather than actually delivered.
* Tag expression evaluation — supports ``&&``, ``||``, ``!``, and
  parentheses for tag targeting.
"""

from __future__ import annotations

import json
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
# Tag expression evaluator
# ---------------------------------------------------------------------------

def _eval_tag_expr(expr: str, device_tags: set[str]) -> bool:
    """
    Evaluate a simple tag expression against a set of device tags.

    Supported operators: ``&&``, ``||``, ``!``, ``()``.
    Single tag names must match ``[A-Za-z0-9_:-]+``.
    """
    expr = expr.strip()

    # Parenthesised sub-expression (greedy inner-most first)
    while "(" in expr:
        inner = re.search(r"\(([^()]+)\)", expr)
        if not inner:
            raise BadRequest(f"Unmatched parentheses in tag expression: {expr}")
        sub_result = _eval_tag_expr(inner.group(1), device_tags)
        expr = expr[: inner.start()] + ("1" if sub_result else "0") + expr[inner.end():]

    # Handle pre-evaluated literals from recursive calls
    expr = expr.strip()

    # Split on || (lowest precedence)
    or_parts = [p.strip() for p in _split_on(expr, "||")]
    if len(or_parts) > 1:
        return any(_eval_tag_expr(p, device_tags) for p in or_parts)

    # Split on && (higher precedence)
    and_parts = [p.strip() for p in _split_on(expr, "&&")]
    if len(and_parts) > 1:
        return all(_eval_tag_expr(p, device_tags) for p in and_parts)

    # NOT
    if expr.startswith("!"):
        return not _eval_tag_expr(expr[1:].strip(), device_tags)

    # Literal 1/0 from parenthesis reduction
    if expr == "1":
        return True
    if expr == "0":
        return False

    # Single tag
    tag = expr.strip()
    if not re.fullmatch(r"[\w:@\.\-]+", tag):
        raise BadRequest(f"Invalid tag in expression: '{tag}'")
    return tag in device_tags


def _split_on(expr: str, op: str) -> list[str]:
    """Split ``expr`` on ``op`` at the top level (no nested parentheses)."""
    parts = []
    depth = 0
    current = []
    i = 0
    op_len = len(op)
    while i < len(expr):
        if expr[i] == "(":
            depth += 1
            current.append(expr[i])
            i += 1
        elif expr[i] == ")":
            depth -= 1
            current.append(expr[i])
            i += 1
        elif depth == 0 and expr[i: i + op_len] == op:
            parts.append("".join(current))
            current = []
            i += op_len
        else:
            current.append(expr[i])
            i += 1
    parts.append("".join(current))
    return parts


class NotificationHubsService:
    """Local emulation of Azure Notification Hubs."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS nh_hubs (
                name    TEXT PRIMARY KEY,
                created TEXT NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS nh_registrations (
                id          TEXT PRIMARY KEY,
                hub         TEXT NOT NULL,
                handle      TEXT NOT NULL,
                platform    TEXT NOT NULL DEFAULT 'gcm',
                tags        TEXT NOT NULL DEFAULT '[]',
                expires_at  REAL,
                created     REAL NOT NULL,
                updated     REAL NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS nh_installations (
                installation_id TEXT NOT NULL,
                hub             TEXT NOT NULL,
                handle          TEXT NOT NULL,
                platform        TEXT NOT NULL DEFAULT 'gcm',
                tags            TEXT NOT NULL DEFAULT '[]',
                templates       TEXT NOT NULL DEFAULT '{}',
                created         REAL NOT NULL,
                updated         REAL NOT NULL,
                PRIMARY KEY (hub, installation_id)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS nh_sent_notifications (
                notification_id TEXT PRIMARY KEY,
                hub             TEXT NOT NULL,
                payload         TEXT NOT NULL,
                tag_expression  TEXT,
                platform        TEXT,
                sent_at         REAL NOT NULL,
                recipient_count INTEGER NOT NULL DEFAULT 0,
                recipients      TEXT NOT NULL DEFAULT '[]'
            )
        """)

    # ------------------------------------------------------------------
    # Hub management
    # ------------------------------------------------------------------

    def create_hub(self, name: str) -> dict:
        if self.store.query(
            "SELECT name FROM nh_hubs WHERE name=?", (name,)
        ):
            raise Conflict(f"Notification hub '{name}' already exists")
        self.store.execute(
            "INSERT INTO nh_hubs VALUES (?,?)", (name, _now_iso())
        )
        return {"name": name, "created": _now_iso()}

    def delete_hub(self, name: str) -> None:
        self._req_hub(name)
        self.store.execute(
            "DELETE FROM nh_registrations WHERE hub=?", (name,)
        )
        self.store.execute(
            "DELETE FROM nh_installations WHERE hub=?", (name,)
        )
        self.store.execute(
            "DELETE FROM nh_sent_notifications WHERE hub=?", (name,)
        )
        self.store.execute("DELETE FROM nh_hubs WHERE name=?", (name,))

    def list_hubs(self) -> list[str]:
        rows = self.store.query(
            "SELECT name FROM nh_hubs ORDER BY name"
        )
        return [r["name"] for r in rows]

    def _req_hub(self, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM nh_hubs WHERE name=?", (name,)
        ):
            raise NotFound(f"Notification hub '{name}' not found")

    # ------------------------------------------------------------------
    # Registrations
    # ------------------------------------------------------------------

    def create_registration(self, hub: str, handle: str, *,
                            platform: str = "gcm",
                            tags: list[str] | None = None,
                            expires_at: float | None = None) -> dict:
        """Create a new registration for a device handle."""
        self._req_hub(hub)
        reg_id = uuid.uuid4().hex
        now = _now()
        self.store.execute(
            "INSERT INTO nh_registrations VALUES (?,?,?,?,?,?,?,?)",
            (reg_id, hub, handle, platform,
             json.dumps(tags or []), expires_at, now, now),
        )
        return self._reg_dict(reg_id)

    def update_registration(self, hub: str, registration_id: str, *,
                            handle: str | None = None,
                            tags: list[str] | None = None,
                            expires_at: float | None = None) -> dict:
        """Update an existing registration."""
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT * FROM nh_registrations WHERE id=? AND hub=?",
            (registration_id, hub),
        )
        if not rows:
            raise NotFound(
                f"Registration '{registration_id}' not found in hub '{hub}'"
            )
        r = rows[0]
        new_handle = handle or r["handle"]
        new_tags = json.dumps(tags) if tags is not None else r["tags"]
        new_exp = expires_at if expires_at is not None else r["expires_at"]
        self.store.execute(
            "UPDATE nh_registrations SET handle=?, tags=?, expires_at=?, updated=? "
            "WHERE id=?",
            (new_handle, new_tags, new_exp, _now(), registration_id),
        )
        return self._reg_dict(registration_id)

    def delete_registration(self, hub: str, registration_id: str) -> None:
        self._req_hub(hub)
        if not self.store.query(
            "SELECT id FROM nh_registrations WHERE id=? AND hub=?",
            (registration_id, hub),
        ):
            raise NotFound(
                f"Registration '{registration_id}' not found in hub '{hub}'"
            )
        self.store.execute(
            "DELETE FROM nh_registrations WHERE id=?", (registration_id,)
        )

    def get_registration(self, hub: str, registration_id: str) -> dict:
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT * FROM nh_registrations WHERE id=? AND hub=?",
            (registration_id, hub),
        )
        if not rows:
            raise NotFound(
                f"Registration '{registration_id}' not found in hub '{hub}'"
            )
        return self._reg_dict(registration_id)

    def list_registrations(self, hub: str, *,
                           tag_filter: str | None = None,
                           top: int = 100) -> list[dict]:
        """List registrations, optionally filtering by a single required tag."""
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT * FROM nh_registrations WHERE hub=? ORDER BY created",
            (hub,),
        )
        results = []
        for r in rows:
            tags = json.loads(r["tags"] or "[]")
            if tag_filter and tag_filter not in tags:
                continue
            results.append(self._reg_dict_row(r))
            if len(results) >= top:
                break
        return results

    def _reg_dict(self, reg_id: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM nh_registrations WHERE id=?", (reg_id,)
        )
        return self._reg_dict_row(rows[0])

    @staticmethod
    def _reg_dict_row(r) -> dict:
        return {
            "id": r["id"],
            "hub": r["hub"],
            "handle": r["handle"],
            "platform": r["platform"],
            "tags": json.loads(r["tags"] or "[]"),
            "expires_at": r["expires_at"],
            "created": r["created"],
            "updated": r["updated"],
        }

    # ------------------------------------------------------------------
    # Installations
    # ------------------------------------------------------------------

    def upsert_installation(self, hub: str, installation_id: str, *,
                            handle: str,
                            platform: str = "gcm",
                            tags: list[str] | None = None,
                            templates: dict | None = None) -> dict:
        self._req_hub(hub)
        now = _now()
        existing = self.store.query(
            "SELECT installation_id FROM nh_installations "
            "WHERE hub=? AND installation_id=?",
            (hub, installation_id),
        )
        if existing:
            self.store.execute(
                "UPDATE nh_installations "
                "SET handle=?, platform=?, tags=?, templates=?, updated=? "
                "WHERE hub=? AND installation_id=?",
                (handle, platform,
                 json.dumps(tags or []),
                 json.dumps(templates or {}),
                 now, hub, installation_id),
            )
        else:
            self.store.execute(
                "INSERT INTO nh_installations VALUES (?,?,?,?,?,?,?,?)",
                (installation_id, hub, handle, platform,
                 json.dumps(tags or []),
                 json.dumps(templates or {}),
                 now, now),
            )
        return self._install_dict(hub, installation_id)

    def delete_installation(self, hub: str, installation_id: str) -> None:
        self._req_hub(hub)
        if not self.store.query(
            "SELECT installation_id FROM nh_installations "
            "WHERE hub=? AND installation_id=?",
            (hub, installation_id),
        ):
            raise NotFound(
                f"Installation '{installation_id}' not found in hub '{hub}'"
            )
        self.store.execute(
            "DELETE FROM nh_installations WHERE hub=? AND installation_id=?",
            (hub, installation_id),
        )

    def get_installation(self, hub: str, installation_id: str) -> dict:
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT * FROM nh_installations WHERE hub=? AND installation_id=?",
            (hub, installation_id),
        )
        if not rows:
            raise NotFound(
                f"Installation '{installation_id}' not found in hub '{hub}'"
            )
        return self._install_dict_row(rows[0])

    def _install_dict(self, hub: str, installation_id: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM nh_installations WHERE hub=? AND installation_id=?",
            (hub, installation_id),
        )
        return self._install_dict_row(rows[0])

    @staticmethod
    def _install_dict_row(r) -> dict:
        return {
            "installation_id": r["installation_id"],
            "hub": r["hub"],
            "handle": r["handle"],
            "platform": r["platform"],
            "tags": json.loads(r["tags"] or "[]"),
            "templates": json.loads(r["templates"] or "{}"),
            "created": r["created"],
            "updated": r["updated"],
        }

    # ------------------------------------------------------------------
    # Send notifications
    # ------------------------------------------------------------------

    def send_notification(self, hub: str, payload: dict | str, *,
                          tag_expression: str | None = None,
                          platform: str | None = None) -> dict:
        """
        Send a notification (captured for inspection, not actually delivered).

        If ``tag_expression`` is provided it is evaluated against each
        registration's and installation's tag set; only matching devices
        are recorded as recipients.
        """
        self._req_hub(hub)

        if isinstance(payload, str):
            payload_str = payload
        else:
            payload_str = json.dumps(payload)

        # Collect all device handles eligible for this notification
        recipients = []

        # From registrations
        reg_rows = self.store.query(
            "SELECT id, handle, platform, tags FROM nh_registrations WHERE hub=?",
            (hub,),
        )
        for r in reg_rows:
            if platform and r["platform"] != platform:
                continue
            tags = set(json.loads(r["tags"] or "[]"))
            if tag_expression:
                try:
                    if not _eval_tag_expr(tag_expression, tags):
                        continue
                except BadRequest:
                    continue
            recipients.append({"type": "registration",
                                "id": r["id"],
                                "handle": r["handle"],
                                "platform": r["platform"]})

        # From installations
        inst_rows = self.store.query(
            "SELECT installation_id, handle, platform, tags "
            "FROM nh_installations WHERE hub=?",
            (hub,),
        )
        for r in inst_rows:
            if platform and r["platform"] != platform:
                continue
            tags = set(json.loads(r["tags"] or "[]"))
            if tag_expression:
                try:
                    if not _eval_tag_expr(tag_expression, tags):
                        continue
                except BadRequest:
                    continue
            recipients.append({"type": "installation",
                                "id": r["installation_id"],
                                "handle": r["handle"],
                                "platform": r["platform"]})

        notif_id = uuid.uuid4().hex
        now = _now()
        self.store.execute(
            "INSERT INTO nh_sent_notifications VALUES (?,?,?,?,?,?,?,?)",
            (notif_id, hub, payload_str, tag_expression, platform,
             now, len(recipients), json.dumps(recipients)),
        )
        return {
            "notification_id": notif_id,
            "hub": hub,
            "sent_at": now,
            "recipient_count": len(recipients),
        }

    def list_sent_notifications(self, hub: str, *,
                                limit: int = 100) -> list[dict]:
        """Retrieve captured sent notifications for a hub."""
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT * FROM nh_sent_notifications WHERE hub=? "
            "ORDER BY sent_at DESC LIMIT ?",
            (hub, limit),
        )
        return [
            {
                "notification_id": r["notification_id"],
                "hub": hub,
                "payload": r["payload"],
                "tag_expression": r["tag_expression"],
                "platform": r["platform"],
                "sent_at": r["sent_at"],
                "recipient_count": r["recipient_count"],
                "recipients": json.loads(r["recipients"] or "[]"),
            }
            for r in rows
        ]

    def get_sent_notification(self, hub: str,
                              notification_id: str) -> dict:
        self._req_hub(hub)
        rows = self.store.query(
            "SELECT * FROM nh_sent_notifications "
            "WHERE hub=? AND notification_id=?",
            (hub, notification_id),
        )
        if not rows:
            raise NotFound(
                f"Notification '{notification_id}' not found in hub '{hub}'"
            )
        r = rows[0]
        return {
            "notification_id": r["notification_id"],
            "hub": hub,
            "payload": r["payload"],
            "tag_expression": r["tag_expression"],
            "platform": r["platform"],
            "sent_at": r["sent_at"],
            "recipient_count": r["recipient_count"],
            "recipients": json.loads(r["recipients"] or "[]"),
        }
