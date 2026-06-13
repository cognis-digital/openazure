"""Azure Files service (File Shares).

A local, compatible subset of Azure Files. Shares contain a tree of
*directories* and *files*. Files carry byte payloads, metadata, and
content-type, mirroring Blob Storage semantics but within a hierarchical
namespace.

Supported operations
--------------------
* Shares: create, delete, list, get properties.
* Directories: create, delete, list (returns child directories + files).
* Files: create (allocate), upload range (full-file put), get, delete,
  copy (server-side), set metadata, get properties.

Path model
~~~~~~~~~~
All paths within a share use forward slashes. The root directory is the
empty string ``""``. A file at ``logs/app.log`` lives under directory
``logs``. Parent directories must exist before creating child directories
or files (matching real Azure Files behavior).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Optional

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FileShareService:
    """Local Azure Files emulation."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS fileshares (
                name TEXT PRIMARY KEY,
                quota_gb INTEGER NOT NULL DEFAULT 5120,
                created TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS share_directories (
                share TEXT NOT NULL,
                path TEXT NOT NULL,
                created TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (share, path)
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS share_files (
                share TEXT NOT NULL,
                path TEXT NOT NULL,
                content BLOB NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                etag TEXT NOT NULL,
                md5 TEXT NOT NULL,
                last_modified TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (share, path)
            )
            """
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _require_share(self, share: str) -> None:
        if not self.store.query(
            "SELECT name FROM fileshares WHERE name=?", (share,)
        ):
            raise NotFound(f"share '{share}' not found")

    @staticmethod
    def _parent(path: str) -> str:
        """Return the parent directory path (empty string = root)."""
        if "/" in path:
            return path.rsplit("/", 1)[0]
        return ""

    def _require_parent_dir(self, share: str, path: str) -> None:
        """Ensure the parent directory exists (root always exists)."""
        parent = self._parent(path)
        if parent == "":
            return  # root always exists
        if not self.store.query(
            "SELECT path FROM share_directories WHERE share=? AND path=?",
            (share, parent),
        ):
            raise NotFound(
                f"parent directory '{parent}' does not exist in share '{share}'"
            )

    # ------------------------------------------------------------------
    # Shares
    # ------------------------------------------------------------------
    def create_share(self, name: str, quota_gb: int = 5120,
                     metadata: Optional[dict] = None) -> dict:
        if self.store.query(
            "SELECT name FROM fileshares WHERE name=?", (name,)
        ):
            raise Conflict(f"share '{name}' already exists")
        ts = _now_iso()
        self.store.execute(
            "INSERT INTO fileshares (name, quota_gb, created, metadata) "
            "VALUES (?, ?, ?, ?)",
            (name, quota_gb, ts, json.dumps(metadata or {})),
        )
        return {"name": name, "quota_gb": quota_gb, "created": ts,
                "metadata": metadata or {}}

    def delete_share(self, name: str) -> None:
        self._require_share(name)
        self.store.execute(
            "DELETE FROM share_files WHERE share=?", (name,)
        )
        self.store.execute(
            "DELETE FROM share_directories WHERE share=?", (name,)
        )
        self.store.execute("DELETE FROM fileshares WHERE name=?", (name,))

    def list_shares(self) -> list[dict]:
        rows = self.store.query(
            "SELECT name, quota_gb, created, metadata FROM fileshares "
            "ORDER BY name"
        )
        result = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"] or "{}")
            except Exception:
                meta = {}
            result.append({
                "name": r["name"],
                "quota_gb": r["quota_gb"],
                "created": r["created"],
                "metadata": meta,
            })
        return result

    def get_share_properties(self, name: str) -> dict:
        self._require_share(name)
        rows = self.store.query(
            "SELECT name, quota_gb, created, metadata FROM fileshares "
            "WHERE name=?",
            (name,),
        )
        r = rows[0]
        try:
            meta = json.loads(r["metadata"] or "{}")
        except Exception:
            meta = {}
        return {
            "name": r["name"],
            "quota_gb": r["quota_gb"],
            "created": r["created"],
            "metadata": meta,
        }

    # ------------------------------------------------------------------
    # Directories
    # ------------------------------------------------------------------
    def create_directory(self, share: str, path: str,
                         metadata: Optional[dict] = None) -> dict:
        self._require_share(share)
        path = path.strip("/")
        self._require_parent_dir(share, path)
        if self.store.query(
            "SELECT path FROM share_directories WHERE share=? AND path=?",
            (share, path),
        ):
            raise Conflict(
                f"directory '{path}' already exists in share '{share}'"
            )
        ts = _now_iso()
        self.store.execute(
            "INSERT INTO share_directories (share, path, created, metadata) "
            "VALUES (?, ?, ?, ?)",
            (share, path, ts, json.dumps(metadata or {})),
        )
        return {"share": share, "path": path, "created": ts,
                "metadata": metadata or {}}

    def delete_directory(self, share: str, path: str) -> None:
        self._require_share(share)
        path = path.strip("/")
        if not self.store.query(
            "SELECT path FROM share_directories WHERE share=? AND path=?",
            (share, path),
        ):
            raise NotFound(
                f"directory '{path}' not found in share '{share}'"
            )
        # Refuse deletion if non-empty
        prefix = path + "/"
        children_dirs = self.store.query(
            "SELECT path FROM share_directories WHERE share=? AND path LIKE ?",
            (share, prefix + "%"),
        )
        children_files = self.store.query(
            "SELECT path FROM share_files WHERE share=? AND path LIKE ?",
            (share, prefix + "%"),
        )
        if children_dirs or children_files:
            raise BadRequest(
                f"directory '{path}' is not empty; delete contents first"
            )
        self.store.execute(
            "DELETE FROM share_directories WHERE share=? AND path=?",
            (share, path),
        )

    def list_directory(self, share: str,
                       path: str = "") -> dict:
        """Return immediate children (directories + files) of *path*."""
        self._require_share(share)
        path = path.strip("/")
        # Verify the directory exists (root is implicitly valid)
        if path and not self.store.query(
            "SELECT path FROM share_directories WHERE share=? AND path=?",
            (share, path),
        ):
            raise NotFound(
                f"directory '{path}' not found in share '{share}'"
            )
        prefix = (path + "/") if path else ""

        # immediate child directories
        dir_rows = self.store.query(
            "SELECT path FROM share_directories WHERE share=? ORDER BY path",
            (share,),
        )
        dirs = []
        for r in dir_rows:
            p = r["path"]
            if not p.startswith(prefix):
                continue
            remainder = p[len(prefix):]
            if "/" not in remainder and remainder:
                dirs.append({"name": remainder, "path": p})

        # immediate child files
        file_rows = self.store.query(
            "SELECT path, content_type, etag, md5, last_modified, "
            "length(content) AS size, metadata FROM share_files "
            "WHERE share=? ORDER BY path",
            (share,),
        )
        files = []
        for r in file_rows:
            p = r["path"]
            if not p.startswith(prefix):
                continue
            remainder = p[len(prefix):]
            if "/" not in remainder:
                try:
                    meta = json.loads(r["metadata"] or "{}")
                except Exception:
                    meta = {}
                files.append({
                    "name": remainder,
                    "path": p,
                    "content_type": r["content_type"],
                    "etag": r["etag"],
                    "content_md5": r["md5"],
                    "last_modified": r["last_modified"],
                    "size": r["size"],
                    "metadata": meta,
                })

        return {"path": path, "directories": dirs, "files": files}

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    def upload_file(self, share: str, path: str, data: bytes,
                    content_type: str = "application/octet-stream",
                    metadata: Optional[dict] = None) -> dict:
        """Create or replace a file."""
        self._require_share(share)
        path = path.strip("/")
        self._require_parent_dir(share, path)
        if isinstance(data, str):
            data = data.encode("utf-8")
        etag = '"%s"' % uuid.uuid4().hex
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
        ts = _now_iso()
        meta_json = json.dumps(metadata or {})
        self.store.execute(
            """
            INSERT INTO share_files
                (share, path, content, content_type, etag, md5,
                 last_modified, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(share, path) DO UPDATE SET
                content=excluded.content,
                content_type=excluded.content_type,
                etag=excluded.etag,
                md5=excluded.md5,
                last_modified=excluded.last_modified,
                metadata=excluded.metadata
            """,
            (share, path, data, content_type, etag, md5, ts, meta_json),
        )
        return {
            "share": share,
            "path": path,
            "etag": etag,
            "content_md5": md5,
            "last_modified": ts,
            "size": len(data),
            "metadata": metadata or {},
        }

    def get_file(self, share: str, path: str) -> dict:
        self._require_share(share)
        path = path.strip("/")
        rows = self.store.query(
            "SELECT * FROM share_files WHERE share=? AND path=?",
            (share, path),
        )
        if not rows:
            raise NotFound(f"file '{path}' not found in share '{share}'")
        r = rows[0]
        content = r["content"]
        if isinstance(content, str):
            content = content.encode("utf-8")
        try:
            meta = json.loads(r["metadata"] or "{}")
        except Exception:
            meta = {}
        return {
            "share": share,
            "path": path,
            "content": content,
            "content_type": r["content_type"],
            "etag": r["etag"],
            "content_md5": r["md5"],
            "last_modified": r["last_modified"],
            "size": len(content),
            "metadata": meta,
        }

    def get_file_properties(self, share: str, path: str) -> dict:
        f = self.get_file(share, path)
        f.pop("content")
        return f

    def delete_file(self, share: str, path: str) -> None:
        self._require_share(share)
        path = path.strip("/")
        if not self.store.query(
            "SELECT path FROM share_files WHERE share=? AND path=?",
            (share, path),
        ):
            raise NotFound(f"file '{path}' not found in share '{share}'")
        self.store.execute(
            "DELETE FROM share_files WHERE share=? AND path=?",
            (share, path),
        )

    def set_file_metadata(self, share: str, path: str,
                          metadata: dict) -> None:
        self._require_share(share)
        path = path.strip("/")
        if not self.store.query(
            "SELECT path FROM share_files WHERE share=? AND path=?",
            (share, path),
        ):
            raise NotFound(f"file '{path}' not found in share '{share}'")
        self.store.execute(
            "UPDATE share_files SET metadata=? WHERE share=? AND path=?",
            (json.dumps(metadata), share, path),
        )

    def copy_file(self, src_share: str, src_path: str,
                  dst_share: str, dst_path: str) -> dict:
        """Server-side copy."""
        src = self.get_file(src_share, src_path)
        return self.upload_file(
            dst_share, dst_path,
            src["content"],
            content_type=src["content_type"],
            metadata=src.get("metadata", {}),
        )
