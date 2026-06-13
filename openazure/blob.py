"""Blob Storage service.

A local, compatible subset of Azure Blob Storage: containers hold named
blobs. Blobs are arbitrary byte payloads with a content-type, an ETag, a
last-modified timestamp, and an MD5 content hash (mirroring the
``Content-MD5`` header semantics Azure exposes).

Extended operations (this pass):

* **Block blobs** — stage individual blocks (``stage_block``), then commit
  a block list in order (``commit_block_list``), matching Azure's two-phase
  put-block / put-block-list pattern.
* **Metadata** — per-blob key/value metadata dict; returned alongside blob
  properties.
* **Copy** — server-side copy of a blob to a new name (or container).
* **Access tiers** — Hot / Cool / Archive label stored per blob.
* **SAS token stub** — ``generate_sas`` returns a signed URL fragment (no
  real cryptographic enforcement; useful for testing URL-assembly code).
* **Container lease stub** — ``acquire_lease`` / ``release_lease`` give a
  simple exclusive-lease token per container.

Data is stored in sqlite via :class:`openazure.store.Store`. Blob payloads
are kept as BLOB columns which keeps the implementation dependency-free and
works identically in-memory and on-disk.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Optional

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_epoch() -> float:
    return time.time()


class BlobService:
    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS blob_containers (
                name TEXT PRIMARY KEY,
                created TEXT NOT NULL,
                lease_id TEXT,
                lease_acquired TEXT
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
                metadata TEXT NOT NULL DEFAULT '{}',
                tier TEXT NOT NULL DEFAULT 'Hot',
                PRIMARY KEY (container, name)
            )
            """
        )
        # staged blocks waiting for commit_block_list
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS blob_staged_blocks (
                container TEXT NOT NULL,
                blob_name TEXT NOT NULL,
                block_id TEXT NOT NULL,
                content BLOB NOT NULL,
                staged_at REAL NOT NULL,
                PRIMARY KEY (container, blob_name, block_id)
            )
            """
        )
        # add new columns to existing installs (idempotent ALTER TABLE)
        for col, defn in [
            ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
            ("tier", "TEXT NOT NULL DEFAULT 'Hot'"),
            ("lease_id", "TEXT"),
            ("lease_acquired", "TEXT"),
        ]:
            try:
                if col in ("lease_id", "lease_acquired"):
                    self.store.execute(
                        f"ALTER TABLE blob_containers ADD COLUMN {col} TEXT"
                    )
                else:
                    self.store.execute(
                        f"ALTER TABLE blobs ADD COLUMN {col} {defn}"
                    )
            except Exception:
                pass  # column already exists

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
        self.store.execute(
            "DELETE FROM blob_staged_blocks WHERE container=?", (name,)
        )
        self.store.execute(
            "DELETE FROM blob_containers WHERE name=?", (name,)
        )

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
    # Container leases (stub)
    # ------------------------------------------------------------------
    def acquire_lease(self, container: str) -> str:
        """Acquire an exclusive lease on the container. Returns a lease ID."""
        self._require_container(container)
        rows = self.store.query(
            "SELECT lease_id FROM blob_containers WHERE name=?", (container,)
        )
        if rows and rows[0]["lease_id"]:
            raise Conflict(f"container '{container}' already has an active lease")
        lease_id = uuid.uuid4().hex
        self.store.execute(
            "UPDATE blob_containers SET lease_id=?, lease_acquired=? WHERE name=?",
            (lease_id, _now_iso(), container),
        )
        return lease_id

    def release_lease(self, container: str, lease_id: str) -> None:
        """Release a previously acquired lease."""
        self._require_container(container)
        rows = self.store.query(
            "SELECT lease_id FROM blob_containers WHERE name=?", (container,)
        )
        if not rows or rows[0]["lease_id"] != lease_id:
            raise BadRequest("lease_id mismatch or no active lease")
        self.store.execute(
            "UPDATE blob_containers SET lease_id=NULL, lease_acquired=NULL WHERE name=?",
            (container,),
        )

    # ------------------------------------------------------------------
    # Blobs — basic
    # ------------------------------------------------------------------
    def put_blob(self, container: str, name: str, data: bytes,
                 content_type: str = "application/octet-stream",
                 metadata: Optional[dict] = None,
                 tier: str = "Hot") -> dict:
        self._require_container(container)
        if isinstance(data, str):
            data = data.encode("utf-8")
        etag = '"%s"' % uuid.uuid4().hex
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
        last_modified = _now_iso()
        meta_json = json.dumps(metadata or {})
        self.store.execute(
            """
            INSERT INTO blobs
                (container, name, content, content_type, etag, md5,
                 last_modified, metadata, tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(container, name) DO UPDATE SET
                content=excluded.content,
                content_type=excluded.content_type,
                etag=excluded.etag,
                md5=excluded.md5,
                last_modified=excluded.last_modified,
                metadata=excluded.metadata,
                tier=excluded.tier
            """,
            (container, name, data, content_type, etag, md5,
             last_modified, meta_json, tier),
        )
        return {
            "container": container,
            "name": name,
            "etag": etag,
            "content_md5": md5,
            "last_modified": last_modified,
            "size": len(data),
            "metadata": metadata or {},
            "tier": tier,
        }

    def get_blob(self, container: str, name: str) -> dict:
        self._require_container(container)
        rows = self.store.query(
            "SELECT * FROM blobs WHERE container=? AND name=?",
            (container, name),
        )
        if not rows:
            raise NotFound(
                f"blob '{name}' not found in container '{container}'"
            )
        r = rows[0]
        content = r["content"]
        if isinstance(content, str):
            content = content.encode("utf-8")
        metadata = {}
        try:
            metadata = json.loads(r["metadata"] or "{}")
        except Exception:
            pass
        return {
            "container": container,
            "name": name,
            "content": content,
            "content_type": r["content_type"],
            "etag": r["etag"],
            "content_md5": r["md5"],
            "last_modified": r["last_modified"],
            "size": len(content),
            "metadata": metadata,
            "tier": r["tier"] if "tier" in r.keys() else "Hot",
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
            raise NotFound(
                f"blob '{name}' not found in container '{container}'"
            )
        self.store.execute(
            "DELETE FROM blobs WHERE container=? AND name=?",
            (container, name),
        )

    def list_blobs(self, container: str,
                   prefix: Optional[str] = None) -> list[dict]:
        self._require_container(container)
        if prefix:
            rows = self.store.query(
                "SELECT name, content_type, etag, md5, last_modified, "
                "length(content) AS size, metadata, tier FROM blobs "
                "WHERE container=? AND name LIKE ? ORDER BY name",
                (container, prefix + "%"),
            )
        else:
            rows = self.store.query(
                "SELECT name, content_type, etag, md5, last_modified, "
                "length(content) AS size, metadata, tier FROM blobs "
                "WHERE container=? ORDER BY name",
                (container,),
            )
        result = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"] or "{}")
            except Exception:
                meta = {}
            result.append({
                "name": r["name"],
                "content_type": r["content_type"],
                "etag": r["etag"],
                "content_md5": r["md5"],
                "last_modified": r["last_modified"],
                "size": r["size"],
                "metadata": meta,
                "tier": r["tier"],
            })
        return result

    # ------------------------------------------------------------------
    # Blob metadata
    # ------------------------------------------------------------------
    def set_blob_metadata(self, container: str, name: str,
                          metadata: dict) -> None:
        """Replace the metadata dict for an existing blob."""
        self._require_container(container)
        rows = self.store.query(
            "SELECT name FROM blobs WHERE container=? AND name=?",
            (container, name),
        )
        if not rows:
            raise NotFound(
                f"blob '{name}' not found in container '{container}'"
            )
        self.store.execute(
            "UPDATE blobs SET metadata=? WHERE container=? AND name=?",
            (json.dumps(metadata), container, name),
        )

    # ------------------------------------------------------------------
    # Blob access tier
    # ------------------------------------------------------------------
    def set_blob_tier(self, container: str, name: str, tier: str) -> None:
        """Set access tier (Hot / Cool / Archive)."""
        if tier not in ("Hot", "Cool", "Archive"):
            raise BadRequest(f"invalid tier '{tier}'; must be Hot, Cool, or Archive")
        self._require_container(container)
        rows = self.store.query(
            "SELECT name FROM blobs WHERE container=? AND name=?",
            (container, name),
        )
        if not rows:
            raise NotFound(
                f"blob '{name}' not found in container '{container}'"
            )
        self.store.execute(
            "UPDATE blobs SET tier=? WHERE container=? AND name=?",
            (tier, container, name),
        )

    # ------------------------------------------------------------------
    # Server-side copy
    # ------------------------------------------------------------------
    def copy_blob(self, src_container: str, src_name: str,
                  dst_container: str, dst_name: str) -> dict:
        """Copy a blob within (or across) containers."""
        src = self.get_blob(src_container, src_name)
        return self.put_blob(
            dst_container, dst_name,
            src["content"],
            content_type=src["content_type"],
            metadata=src.get("metadata", {}),
            tier=src.get("tier", "Hot"),
        )

    # ------------------------------------------------------------------
    # Block blobs (stage / commit)
    # ------------------------------------------------------------------
    def stage_block(self, container: str, blob_name: str,
                    block_id: str, data: bytes) -> None:
        """Stage a single block; it is not visible until committed."""
        self._require_container(container)
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.store.execute(
            """
            INSERT INTO blob_staged_blocks
                (container, blob_name, block_id, content, staged_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(container, blob_name, block_id) DO UPDATE SET
                content=excluded.content,
                staged_at=excluded.staged_at
            """,
            (container, blob_name, block_id, data, _now_epoch()),
        )

    def commit_block_list(self, container: str, blob_name: str,
                          block_ids: list[str],
                          content_type: str = "application/octet-stream",
                          metadata: Optional[dict] = None,
                          tier: str = "Hot") -> dict:
        """Assemble staged blocks in order and write the final blob."""
        self._require_container(container)
        rows = {
            r["block_id"]: bytes(r["content"])
            for r in self.store.query(
                "SELECT block_id, content FROM blob_staged_blocks "
                "WHERE container=? AND blob_name=?",
                (container, blob_name),
            )
        }
        missing = [bid for bid in block_ids if bid not in rows]
        if missing:
            raise BadRequest(
                f"block(s) not staged: {missing}"
            )
        assembled = b"".join(rows[bid] for bid in block_ids)
        result = self.put_blob(
            container, blob_name, assembled,
            content_type=content_type,
            metadata=metadata,
            tier=tier,
        )
        # clean up staged blocks
        self.store.execute(
            "DELETE FROM blob_staged_blocks WHERE container=? AND blob_name=?",
            (container, blob_name),
        )
        return result

    def list_blocks(self, container: str,
                    blob_name: str) -> list[str]:
        """Return staged (uncommitted) block IDs."""
        self._require_container(container)
        rows = self.store.query(
            "SELECT block_id FROM blob_staged_blocks "
            "WHERE container=? AND blob_name=? ORDER BY staged_at",
            (container, blob_name),
        )
        return [r["block_id"] for r in rows]

    # ------------------------------------------------------------------
    # SAS token stub
    # ------------------------------------------------------------------
    def generate_sas(self, container: str, blob_name: str,
                     permissions: str = "r",
                     expiry_seconds: int = 3600,
                     account_key: str = "localdevkey") -> str:
        """Return a stub SAS query string (not cryptographically enforced).

        The format mirrors the essential fields Azure SAS uses so that code
        assembling SAS URLs can be tested without a real account.
        """
        expiry = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(_now_epoch() + expiry_seconds),
        )
        string_to_sign = "\n".join([
            permissions, "", "", expiry,
            f"/{container}/{blob_name}", "", "", "2023-08-03",
        ])
        sig = base64.b64encode(
            hmac.new(
                account_key.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return (
            f"sv=2023-08-03&se={expiry}&sr=b&sp={permissions}&sig={sig}"
        )
