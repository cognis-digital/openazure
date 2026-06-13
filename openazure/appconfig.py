"""Azure App Configuration emulation.

Supports:
* Key-values — set, get, list (with key/label prefix filters), delete.
* Labels — any string label (including ``null``/empty for no label); a
  key+label pair is unique within a store.
* Revisions — every Set call pushes a new revision row; the active value is
  the latest; revision history can be listed.
* Feature flags — a feature flag is a key-value with the ``feature-flag``
  content type and a JSON body; helper methods create/update/read/toggle them.
* Snapshots — point-in-time named snapshots of the key-value store.

The ``label`` field defaults to ``""`` (no label).
"""

from __future__ import annotations

import json
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


_FEATURE_FLAG_CT = "application/vnd.microsoft.appconfig.ff+json;charset=utf-8"
_KV_CT = "application/json"


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _etag() -> str:
    return uuid.uuid4().hex


class AppConfigService:
    """Local emulation of Azure App Configuration."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS ac_keyvalues (
                store    TEXT NOT NULL,
                key      TEXT NOT NULL,
                label    TEXT NOT NULL DEFAULT '',
                value    TEXT,
                content_type TEXT,
                etag     TEXT NOT NULL,
                locked   INTEGER NOT NULL DEFAULT 0,
                last_modified REAL NOT NULL,
                tags     TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (store, key, label)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS ac_revisions (
                id       TEXT PRIMARY KEY,
                store    TEXT NOT NULL,
                key      TEXT NOT NULL,
                label    TEXT NOT NULL DEFAULT '',
                value    TEXT,
                content_type TEXT,
                etag     TEXT NOT NULL,
                modified REAL NOT NULL,
                tags     TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS ac_snapshots (
                store   TEXT NOT NULL,
                name    TEXT NOT NULL,
                created REAL NOT NULL,
                items   TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (store, name)
            )
        """)

    # ------------------------------------------------------------------
    # Key-values
    # ------------------------------------------------------------------

    def set_keyvalue(self, store: str, key: str, value: str | None = None, *,
                     label: str = "",
                     content_type: str | None = None,
                     tags: dict | None = None,
                     etag_match: str | None = None) -> dict:
        """Create or update a key-value."""
        if not key:
            raise BadRequest("Key must not be empty")
        now = _now()
        et = _etag()
        existing = self.store.query(
            "SELECT etag, locked FROM ac_keyvalues WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )
        if existing:
            if existing[0]["locked"]:
                raise BadRequest(f"Key '{key}' (label='{label}') is locked (read-only)")
            if etag_match and etag_match != existing[0]["etag"]:
                raise BadRequest("ETag mismatch — optimistic concurrency failure")
            self.store.execute(
                """UPDATE ac_keyvalues
                   SET value=?, content_type=?, etag=?, last_modified=?, tags=?
                   WHERE store=? AND key=? AND label=?""",
                (value, content_type or _KV_CT, et, now,
                 json.dumps(tags or {}), store, key, label),
            )
        else:
            self.store.execute(
                "INSERT INTO ac_keyvalues VALUES (?,?,?,?,?,?,0,?,?)",
                (store, key, label, value, content_type or _KV_CT,
                 et, now, json.dumps(tags or {})),
            )
        # Record revision
        rev_id = uuid.uuid4().hex
        self.store.execute(
            "INSERT INTO ac_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            (rev_id, store, key, label, value,
             content_type or _KV_CT, et, now, json.dumps(tags or {})),
        )
        return self._kv_dict(store, key, label)

    def get_keyvalue(self, store: str, key: str,
                     label: str = "") -> dict:
        rows = self.store.query(
            "SELECT * FROM ac_keyvalues WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )
        if not rows:
            raise NotFound(
                f"Key '{key}' (label='{label}') not found in store '{store}'"
            )
        return self._kv_dict_row(rows[0])

    def delete_keyvalue(self, store: str, key: str,
                        label: str = "") -> None:
        existing = self.store.query(
            "SELECT locked FROM ac_keyvalues WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )
        if not existing:
            raise NotFound(
                f"Key '{key}' (label='{label}') not found in store '{store}'"
            )
        if existing[0]["locked"]:
            raise BadRequest(f"Key '{key}' (label='{label}') is locked")
        self.store.execute(
            "DELETE FROM ac_keyvalues WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )

    def list_keyvalues(self, store: str, *,
                       key_filter: str | None = None,
                       label_filter: str | None = None,
                       top: int | None = None) -> list[dict]:
        """List key-values, optionally filtered by key prefix and/or label."""
        rows = self.store.query(
            "SELECT * FROM ac_keyvalues WHERE store=? ORDER BY key, label",
            (store,),
        )
        results = []
        for r in rows:
            if key_filter and not r["key"].startswith(key_filter.rstrip("*")):
                continue
            if label_filter is not None:
                if label_filter == "\0":  # null label sentinel
                    if r["label"] != "":
                        continue
                elif not r["label"].startswith(label_filter.rstrip("*")):
                    continue
            results.append(self._kv_dict_row(r))
            if top and len(results) >= top:
                break
        return results

    def lock_keyvalue(self, store: str, key: str,
                      label: str = "") -> dict:
        """Lock (make read-only) a key-value."""
        self._require_kv(store, key, label)
        self.store.execute(
            "UPDATE ac_keyvalues SET locked=1 WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )
        return self._kv_dict(store, key, label)

    def unlock_keyvalue(self, store: str, key: str,
                        label: str = "") -> dict:
        """Unlock a key-value."""
        self._require_kv(store, key, label)
        self.store.execute(
            "UPDATE ac_keyvalues SET locked=0 WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )
        return self._kv_dict(store, key, label)

    def list_revisions(self, store: str, key: str,
                       label: str = "") -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM ac_revisions WHERE store=? AND key=? AND label=? "
            "ORDER BY modified DESC",
            (store, key, label),
        )
        return [self._rev_dict(r) for r in rows]

    def _require_kv(self, store: str, key: str, label: str):
        if not self.store.query(
            "SELECT key FROM ac_keyvalues WHERE store=? AND key=? AND label=?",
            (store, key, label),
        ):
            raise NotFound(
                f"Key '{key}' (label='{label}') not found in store '{store}'"
            )

    def _kv_dict(self, store: str, key: str, label: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM ac_keyvalues WHERE store=? AND key=? AND label=?",
            (store, key, label),
        )
        return self._kv_dict_row(rows[0])

    @staticmethod
    def _kv_dict_row(r) -> dict:
        return {
            "key": r["key"],
            "label": r["label"] or None,
            "value": r["value"],
            "content_type": r["content_type"],
            "etag": r["etag"],
            "locked": bool(r["locked"]),
            "last_modified": r["last_modified"],
            "tags": json.loads(r["tags"] or "{}"),
        }

    @staticmethod
    def _rev_dict(r) -> dict:
        return {
            "id": r["id"],
            "key": r["key"],
            "label": r["label"] or None,
            "value": r["value"],
            "etag": r["etag"],
            "modified": r["modified"],
            "tags": json.loads(r["tags"] or "{}"),
        }

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    def set_feature_flag(self, store: str, feature_name: str, *,
                         enabled: bool = False,
                         description: str = "",
                         conditions: dict | None = None,
                         label: str = "") -> dict:
        """Create or update a feature flag."""
        key = f".appconfig.featureflag/{feature_name}"
        body = {
            "id": feature_name,
            "description": description,
            "enabled": enabled,
            "conditions": conditions or {"client_filters": []},
        }
        return self.set_keyvalue(
            store, key, json.dumps(body),
            label=label, content_type=_FEATURE_FLAG_CT,
        )

    def get_feature_flag(self, store: str, feature_name: str,
                         label: str = "") -> dict:
        key = f".appconfig.featureflag/{feature_name}"
        kv = self.get_keyvalue(store, key, label)
        flag_body = json.loads(kv["value"] or "{}")
        return {"key": kv["key"], "label": kv["label"],
                "etag": kv["etag"],
                "feature": flag_body}

    def toggle_feature_flag(self, store: str, feature_name: str,
                            enabled: bool, label: str = "") -> dict:
        key = f".appconfig.featureflag/{feature_name}"
        kv = self.get_keyvalue(store, key, label)
        body = json.loads(kv["value"] or "{}")
        body["enabled"] = enabled
        return self.set_keyvalue(
            store, key, json.dumps(body),
            label=label, content_type=_FEATURE_FLAG_CT,
        )

    def list_feature_flags(self, store: str, label: str | None = None) -> list[dict]:
        prefix = ".appconfig.featureflag/"
        kvs = self.list_keyvalues(store, key_filter=prefix,
                                  label_filter=label)
        result = []
        for kv in kvs:
            try:
                flag_body = json.loads(kv["value"] or "{}")
            except (json.JSONDecodeError, TypeError):
                flag_body = {}
            result.append({"key": kv["key"], "label": kv["label"],
                            "etag": kv["etag"],
                            "feature": flag_body})
        return result

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def create_snapshot(self, store: str, name: str, *,
                        key_filter: str | None = None,
                        label_filter: str | None = None) -> dict:
        """Capture the current state of matching key-values as a named snapshot."""
        if self.store.query(
            "SELECT name FROM ac_snapshots WHERE store=? AND name=?",
            (store, name),
        ):
            raise Conflict(f"Snapshot '{name}' already exists in store '{store}'")
        items = self.list_keyvalues(
            store, key_filter=key_filter, label_filter=label_filter
        )
        now = _now()
        self.store.execute(
            "INSERT INTO ac_snapshots VALUES (?,?,?,?)",
            (store, name, now, json.dumps(items)),
        )
        return {"store": store, "name": name, "created": now,
                "item_count": len(items)}

    def get_snapshot(self, store: str, name: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM ac_snapshots WHERE store=? AND name=?",
            (store, name),
        )
        if not rows:
            raise NotFound(f"Snapshot '{name}' not found in store '{store}'")
        r = rows[0]
        items = json.loads(r["items"])
        return {"store": store, "name": name, "created": r["created"],
                "item_count": len(items), "items": items}

    def list_snapshots(self, store: str) -> list[dict]:
        rows = self.store.query(
            "SELECT name, created, items FROM ac_snapshots WHERE store=? ORDER BY name",
            (store,),
        )
        return [{"name": r["name"], "created": r["created"],
                 "item_count": len(json.loads(r["items"]))}
                for r in rows]

    def delete_snapshot(self, store: str, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM ac_snapshots WHERE store=? AND name=?",
            (store, name),
        ):
            raise NotFound(f"Snapshot '{name}' not found in store '{store}'")
        self.store.execute(
            "DELETE FROM ac_snapshots WHERE store=? AND name=?",
            (store, name),
        )
