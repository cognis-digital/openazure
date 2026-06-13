"""Blob Storage service.

A local, compatible subset of Azure Blob Storage: containers hold named
blobs. Blobs are arbitrary byte payloads with a content-type, an ETag, a
last-modified timestamp, and an MD5 content hash (mirroring the
``Content-MD5`` header semantics Azure exposes).

Data is stored in sqlite via :class:`openazure.store.Store`. Blob payloads
are kept as BLOB columns which keeps the implementation dependency-free and
works identically in-memory and on-disk.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from typing import Optional

from .errors import NotFound, Conflict
from .store import Store


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class BlobService:
    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS blob_containers (
                name TEXT PRIMARY KEY,
                created TEXT NOT NULL
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS blobs (
                container TEXT NOT NULL,
                name TEXT NOT NULL,
                content BLOB NOT NULL,
                content_type TEXT NOT NULL,
                etag TEXT NOT NULL,
                md5 TEXT NOT NULL,
                last_modified TEXT NOT NULL,
                PRIMARY KEY (container, name)
            )
            """
        )

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------
    def create_container(self, name: str) -> dict:
        existing = self.store.query(
            "SELECT name FROM blob_containers WHERE name=?", (name,)
        )
        if existing:
            raise Conflict(f"container '{name}' already exists")
        self.store.execute(
            "INSERT INTO blob_containers (name, created) VALUES (?, ?)",
            (name, _now_iso()),
        )
        return {"name": name}

    def delete_container(self, name: str) -> None:
        rows = self.store.query(
            "SELECT name FROM blob_containers WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"container '{name}' not found")
        self.store.execute("DELETE FROM blobs WHERE container=?", (name,))
        self.store.execute("DELETE FROM blob_containers WHERE name=?", (name,))

    def list_containers(self) -> list[str]:
        rows = self.store.query(
            "SELECT name FROM blob_containers ORDER BY name"
        )
        return [r["name"] for r in rows]

    def _require_container(self, name: str) -> None:
        rows = self.store.query(
            "SELECT name FROM blob_containers WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"container '{name}' not found")

    # ------------------------------------------------------------------
    # Blobs
    # ------------------------------------------------------------------
    def put_blob(self, container: str, name: str, data: bytes,
                 content_type: str = "application/octet-stream") -> dict:
        self._require_container(container)
        if isinstance(data, str):
            data = data.encode("utf-8")
        etag = '"%s"' % uuid.uuid4().hex
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
        last_modified = _now_iso()
        self.store.execute(
            """
            INSERT INTO blobs (container, name, content, content_type, etag, md5, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(container, name) DO UPDATE SET
                content=excluded.content,
                content_type=excluded.content_type,
                etag=excluded.etag,
                md5=excluded.md5,
                last_modified=excluded.last_modified
            """,
            (container, name, data, content_type, etag, md5, last_modified),
        )
        return {
            "container": container,
            "name": name,
            "etag": etag,
            "content_md5": md5,
            "last_modified": last_modified,
            "size": len(data),
        }

    def get_blob(self, container: str, name: str) -> dict:
        self._require_container(container)
        rows = self.store.query(
            "SELECT * FROM blobs WHERE container=? AND name=?",
            (container, name),
        )
        if not rows:
            raise NotFound(f"blob '{name}' not found in container '{container}'")
        r = rows[0]
        content = r["content"]
        if isinstance(content, str):
            content = content.encode("utf-8")
        return {
            "container": container,
            "name": name,
            "content": content,
            "content_type": r["content_type"],
            "etag": r["etag"],
            "content_md5": r["md5"],
            "last_modified": r["last_modified"],
            "size": len(content),
        }

    def get_blob_properties(self, container: str, name: str) -> dict:
        blob = self.get_blob(container, name)
        blob.pop("content")
        return blob

    def delete_blob(self, container: str, name: str) -> None:
        self._require_container(container)
        rows = self.store.query(
            "SELECT name FROM blobs WHERE container=? AND name=?",
            (container, name),
        )
        if not rows:
            raise NotFound(f"blob '{name}' not found in container '{container}'")
        self.store.execute(
            "DELETE FROM blobs WHERE container=? AND name=?", (container, name)
        )

    def list_blobs(self, container: str, prefix: Optional[str] = None) -> list[dict]:
        self._require_container(container)
        if prefix:
            rows = self.store.query(
                "SELECT name, content_type, etag, md5, last_modified, "
                "length(content) AS size FROM blobs "
                "WHERE container=? AND name LIKE ? ORDER BY name",
                (container, prefix + "%"),
            )
        else:
            rows = self.store.query(
                "SELECT name, content_type, etag, md5, last_modified, "
                "length(content) AS size FROM blobs "
                "WHERE container=? ORDER BY name",
                (container,),
            )
        return [
            {
                "name": r["name"],
                "content_type": r["content_type"],
                "etag": r["etag"],
                "content_md5": r["md5"],
                "last_modified": r["last_modified"],
                "size": r["size"],
            }
            for r in rows
        ]
