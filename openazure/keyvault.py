"""Azure Key Vault emulation.

Supports:
* Secrets — set, get, list, delete, soft-delete + purge + recover, list deleted.
* Keys — create (RSA/EC placeholder), list, get, delete, soft-delete;
  encrypt (XOR-based deterministic cipher for testing) and decrypt;
  key-wrapping (wrap/unwrap).
* Certificates — store metadata only (no PEM chain validation); create,
  get, list, delete, soft-delete.

Soft-delete keeps the object in a ``*_deleted`` shadow table for 90 simulated
seconds (configurable) before it can be permanently purged.

All secret / key / certificate versions are tracked: every Set/Create call
produces a new version. ``get`` with no version returns the latest.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Simple deterministic XOR cipher used to emulate encrypt/decrypt for testing.
# The "key material" is derived from the key name via SHA-256.
# ---------------------------------------------------------------------------

def _derive_key_bytes(name: str) -> bytes:
    """Derive 32 bytes from a key name (for test encrypt/decrypt)."""
    return hashlib.sha256(name.encode("utf-8")).digest()


def _xor_cipher(data: bytes, key_bytes: bytes) -> bytes:
    """XOR ``data`` with a repeated ``key_bytes`` stream."""
    out = bytearray(len(data))
    klen = len(key_bytes)
    for i, b in enumerate(data):
        out[i] = b ^ key_bytes[i % klen]
    return bytes(out)


class KeyVaultService:
    """Local emulation of Azure Key Vault (secrets, keys, certificates)."""

    # Number of seconds a soft-deleted object is retained before it must be
    # purged (not auto-purged; just tracked in the deleted table).
    SOFT_DELETE_RETENTION = 90

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        # Secrets
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS kv_secrets (
                vault TEXT NOT NULL,
                name  TEXT NOT NULL,
                version TEXT NOT NULL,
                value TEXT NOT NULL,
                content_type TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                expires_on REAL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                tags TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (vault, name, version)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS kv_secrets_deleted (
                vault TEXT NOT NULL,
                name  TEXT NOT NULL,
                deleted_on REAL NOT NULL,
                snapshot TEXT NOT NULL,
                PRIMARY KEY (vault, name)
            )
        """)
        # Keys
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS kv_keys (
                vault TEXT NOT NULL,
                name  TEXT NOT NULL,
                version TEXT NOT NULL,
                key_type TEXT NOT NULL DEFAULT 'RSA',
                key_size INTEGER NOT NULL DEFAULT 2048,
                key_ops TEXT NOT NULL DEFAULT '["encrypt","decrypt","wrapKey","unwrapKey","sign","verify"]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                tags TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (vault, name, version)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS kv_keys_deleted (
                vault TEXT NOT NULL,
                name  TEXT NOT NULL,
                deleted_on REAL NOT NULL,
                snapshot TEXT NOT NULL,
                PRIMARY KEY (vault, name)
            )
        """)
        # Certificates
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS kv_certs (
                vault TEXT NOT NULL,
                name  TEXT NOT NULL,
                version TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT 'CN=example',
                issuer  TEXT NOT NULL DEFAULT 'Self',
                thumbprint TEXT NOT NULL,
                not_before REAL NOT NULL,
                expires_on REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                tags TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (vault, name, version)
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS kv_certs_deleted (
                vault TEXT NOT NULL,
                name  TEXT NOT NULL,
                deleted_on REAL NOT NULL,
                snapshot TEXT NOT NULL,
                PRIMARY KEY (vault, name)
            )
        """)

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    def set_secret(self, vault: str, name: str, value: str, *,
                   content_type: str | None = None,
                   enabled: bool = True,
                   expires_on: float | None = None,
                   tags: dict | None = None) -> dict:
        """Create or update a secret; each call produces a new version."""
        if self.store.query(
            "SELECT name FROM kv_secrets_deleted WHERE vault=? AND name=?",
            (vault, name),
        ):
            raise Conflict(
                f"Secret '{name}' in vault '{vault}' is soft-deleted; "
                "recover or purge it first."
            )
        version = _new_id()
        now = _now()
        self.store.execute(
            "INSERT INTO kv_secrets VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vault, name, version, value, content_type,
             1 if enabled else 0,
             expires_on, now, now,
             json.dumps(tags or {})),
        )
        return self._secret_bundle(vault, name, version)

    def get_secret(self, vault: str, name: str,
                   version: str | None = None) -> dict:
        """Return the latest or a specific version of a secret."""
        row = self._latest_secret(vault, name, version)
        return self._secret_bundle_row(vault, row)

    def list_secrets(self, vault: str) -> list[dict]:
        rows = self.store.query(
            """
            SELECT s.name, s.version, s.enabled, s.created, s.updated
            FROM kv_secrets s
            WHERE s.vault=?
              AND s.version = (
                  SELECT version FROM kv_secrets
                  WHERE vault=? AND name=s.name
                  ORDER BY created DESC LIMIT 1
              )
            ORDER BY s.name
            """,
            (vault, vault),
        )
        return [{"name": r["name"], "version": r["version"],
                 "enabled": bool(r["enabled"]),
                 "created": r["created"], "updated": r["updated"]}
                for r in rows]

    def list_secret_versions(self, vault: str, name: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM kv_secrets WHERE vault=? AND name=? ORDER BY created DESC",
            (vault, name),
        )
        return [self._secret_bundle_row(vault, r) for r in rows]

    def delete_secret(self, vault: str, name: str) -> dict:
        """Soft-delete a secret (moves to deleted table)."""
        row = self._latest_secret(vault, name)
        snapshot = json.dumps({
            "name": name,
            "version": row["version"],
            "value": row["value"],
            "content_type": row["content_type"],
            "enabled": row["enabled"],
            "expires_on": row["expires_on"],
            "tags": row["tags"],
            "created": row["created"],
        })
        self.store.execute(
            "INSERT OR REPLACE INTO kv_secrets_deleted VALUES (?,?,?,?)",
            (vault, name, _now(), snapshot),
        )
        self.store.execute(
            "DELETE FROM kv_secrets WHERE vault=? AND name=?", (vault, name)
        )
        return {"name": name, "vault": vault, "deleted": True}

    def recover_secret(self, vault: str, name: str) -> dict:
        """Recover a soft-deleted secret."""
        rows = self.store.query(
            "SELECT * FROM kv_secrets_deleted WHERE vault=? AND name=?",
            (vault, name),
        )
        if not rows:
            raise NotFound(f"Deleted secret '{name}' not found in vault '{vault}'")
        snap = json.loads(rows[0]["snapshot"])
        now = _now()
        self.store.execute(
            "INSERT INTO kv_secrets VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vault, name, snap["version"], snap["value"],
             snap["content_type"], snap["enabled"], snap["expires_on"],
             snap["created"], now, snap["tags"]),
        )
        self.store.execute(
            "DELETE FROM kv_secrets_deleted WHERE vault=? AND name=?",
            (vault, name),
        )
        return self._secret_bundle(vault, name, snap["version"])

    def purge_secret(self, vault: str, name: str) -> None:
        """Permanently delete a soft-deleted secret."""
        if not self.store.query(
            "SELECT name FROM kv_secrets_deleted WHERE vault=? AND name=?",
            (vault, name),
        ):
            raise NotFound(
                f"Deleted secret '{name}' not found in vault '{vault}'"
            )
        self.store.execute(
            "DELETE FROM kv_secrets_deleted WHERE vault=? AND name=?",
            (vault, name),
        )

    def list_deleted_secrets(self, vault: str) -> list[dict]:
        rows = self.store.query(
            "SELECT name, deleted_on FROM kv_secrets_deleted WHERE vault=? ORDER BY name",
            (vault,),
        )
        return [{"name": r["name"], "deleted_on": r["deleted_on"]} for r in rows]

    def _latest_secret(self, vault: str, name: str,
                       version: str | None = None):
        if version:
            rows = self.store.query(
                "SELECT * FROM kv_secrets WHERE vault=? AND name=? AND version=?",
                (vault, name, version),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM kv_secrets WHERE vault=? AND name=? "
                "ORDER BY created DESC LIMIT 1",
                (vault, name),
            )
        if not rows:
            raise NotFound(f"Secret '{name}' not found in vault '{vault}'")
        return rows[0]

    def _secret_bundle(self, vault: str, name: str, version: str) -> dict:
        row = self.store.query(
            "SELECT * FROM kv_secrets WHERE vault=? AND name=? AND version=?",
            (vault, name, version),
        )[0]
        return self._secret_bundle_row(vault, row)

    @staticmethod
    def _secret_bundle_row(vault: str, row) -> dict:
        return {
            "vault": vault,
            "name": row["name"],
            "version": row["version"],
            "value": row["value"],
            "content_type": row["content_type"],
            "enabled": bool(row["enabled"]),
            "expires_on": row["expires_on"],
            "created": row["created"],
            "updated": row["updated"],
            "tags": json.loads(row["tags"] or "{}"),
        }

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def create_key(self, vault: str, name: str, *,
                   key_type: str = "RSA",
                   key_size: int = 2048,
                   key_ops: list[str] | None = None,
                   enabled: bool = True,
                   tags: dict | None = None) -> dict:
        if key_type not in ("RSA", "EC", "oct"):
            raise BadRequest(f"Unsupported key type: {key_type}")
        if self.store.query(
            "SELECT name FROM kv_keys_deleted WHERE vault=? AND name=?",
            (vault, name),
        ):
            raise Conflict(
                f"Key '{name}' in vault '{vault}' is soft-deleted; "
                "recover or purge it first."
            )
        version = _new_id()
        now = _now()
        ops = json.dumps(key_ops or [
            "encrypt", "decrypt", "wrapKey", "unwrapKey", "sign", "verify"
        ])
        self.store.execute(
            "INSERT INTO kv_keys VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vault, name, version, key_type, key_size, ops,
             1 if enabled else 0, now, now, json.dumps(tags or {})),
        )
        return self._key_bundle(vault, name, version)

    def get_key(self, vault: str, name: str,
                version: str | None = None) -> dict:
        row = self._latest_key(vault, name, version)
        return self._key_bundle_row(vault, row)

    def list_keys(self, vault: str) -> list[dict]:
        rows = self.store.query(
            """
            SELECT k.name, k.version, k.key_type, k.enabled, k.created, k.updated
            FROM kv_keys k
            WHERE k.vault=?
              AND k.version = (
                  SELECT version FROM kv_keys
                  WHERE vault=? AND name=k.name
                  ORDER BY created DESC LIMIT 1
              )
            ORDER BY k.name
            """,
            (vault, vault),
        )
        return [{"name": r["name"], "version": r["version"],
                 "key_type": r["key_type"],
                 "enabled": bool(r["enabled"]),
                 "created": r["created"], "updated": r["updated"]}
                for r in rows]

    def list_key_versions(self, vault: str, name: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM kv_keys WHERE vault=? AND name=? ORDER BY created DESC",
            (vault, name),
        )
        return [self._key_bundle_row(vault, r) for r in rows]

    def delete_key(self, vault: str, name: str) -> dict:
        row = self._latest_key(vault, name)
        snapshot = json.dumps({
            "name": name, "version": row["version"],
            "key_type": row["key_type"], "key_size": row["key_size"],
            "key_ops": row["key_ops"], "enabled": row["enabled"],
            "tags": row["tags"], "created": row["created"],
        })
        self.store.execute(
            "INSERT OR REPLACE INTO kv_keys_deleted VALUES (?,?,?,?)",
            (vault, name, _now(), snapshot),
        )
        self.store.execute(
            "DELETE FROM kv_keys WHERE vault=? AND name=?", (vault, name)
        )
        return {"name": name, "vault": vault, "deleted": True}

    def recover_key(self, vault: str, name: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM kv_keys_deleted WHERE vault=? AND name=?",
            (vault, name),
        )
        if not rows:
            raise NotFound(f"Deleted key '{name}' not found in vault '{vault}'")
        snap = json.loads(rows[0]["snapshot"])
        now = _now()
        self.store.execute(
            "INSERT INTO kv_keys VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vault, name, snap["version"], snap["key_type"], snap["key_size"],
             snap["key_ops"], snap["enabled"], snap["created"], now, snap["tags"]),
        )
        self.store.execute(
            "DELETE FROM kv_keys_deleted WHERE vault=? AND name=?",
            (vault, name),
        )
        return self._key_bundle(vault, name, snap["version"])

    def purge_key(self, vault: str, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM kv_keys_deleted WHERE vault=? AND name=?",
            (vault, name),
        ):
            raise NotFound(
                f"Deleted key '{name}' not found in vault '{vault}'"
            )
        self.store.execute(
            "DELETE FROM kv_keys_deleted WHERE vault=? AND name=?",
            (vault, name),
        )

    def list_deleted_keys(self, vault: str) -> list[dict]:
        rows = self.store.query(
            "SELECT name, deleted_on FROM kv_keys_deleted WHERE vault=? ORDER BY name",
            (vault,),
        )
        return [{"name": r["name"], "deleted_on": r["deleted_on"]} for r in rows]

    def encrypt(self, vault: str, name: str, plaintext_b64: str, *,
                algorithm: str = "RSA-OAEP",
                version: str | None = None) -> dict:
        """Encrypt base64-encoded plaintext using a deterministic test cipher."""
        row = self._latest_key(vault, name, version)
        if not bool(row["enabled"]):
            raise BadRequest(f"Key '{name}' is disabled")
        raw = base64.b64decode(plaintext_b64)
        key_bytes = _derive_key_bytes(row["name"] + row["version"])
        ciphertext = _xor_cipher(raw, key_bytes)
        return {
            "vault": vault,
            "key_name": name,
            "version": row["version"],
            "algorithm": algorithm,
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    def decrypt(self, vault: str, name: str, ciphertext_b64: str, *,
                algorithm: str = "RSA-OAEP",
                version: str | None = None) -> dict:
        """Decrypt base64-encoded ciphertext produced by encrypt()."""
        row = self._latest_key(vault, name, version)
        if not bool(row["enabled"]):
            raise BadRequest(f"Key '{name}' is disabled")
        raw = base64.b64decode(ciphertext_b64)
        key_bytes = _derive_key_bytes(row["name"] + row["version"])
        plaintext = _xor_cipher(raw, key_bytes)  # XOR is its own inverse
        return {
            "vault": vault,
            "key_name": name,
            "version": row["version"],
            "algorithm": algorithm,
            "plaintext": base64.b64encode(plaintext).decode(),
        }

    def wrap_key(self, vault: str, name: str, key_to_wrap_b64: str, *,
                 algorithm: str = "RSA-OAEP",
                 version: str | None = None) -> dict:
        """Wrap (encrypt) a symmetric key."""
        result = self.encrypt(vault, name, key_to_wrap_b64,
                              algorithm=algorithm, version=version)
        return {
            "vault": vault,
            "key_name": name,
            "version": result["version"],
            "algorithm": algorithm,
            "wrapped_key": result["ciphertext"],
        }

    def unwrap_key(self, vault: str, name: str, wrapped_key_b64: str, *,
                   algorithm: str = "RSA-OAEP",
                   version: str | None = None) -> dict:
        """Unwrap (decrypt) a wrapped symmetric key."""
        result = self.decrypt(vault, name, wrapped_key_b64,
                              algorithm=algorithm, version=version)
        return {
            "vault": vault,
            "key_name": name,
            "version": result["version"],
            "algorithm": algorithm,
            "unwrapped_key": result["plaintext"],
        }

    def _latest_key(self, vault: str, name: str,
                    version: str | None = None):
        if version:
            rows = self.store.query(
                "SELECT * FROM kv_keys WHERE vault=? AND name=? AND version=?",
                (vault, name, version),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM kv_keys WHERE vault=? AND name=? "
                "ORDER BY created DESC LIMIT 1",
                (vault, name),
            )
        if not rows:
            raise NotFound(f"Key '{name}' not found in vault '{vault}'")
        return rows[0]

    def _key_bundle(self, vault: str, name: str, version: str) -> dict:
        row = self.store.query(
            "SELECT * FROM kv_keys WHERE vault=? AND name=? AND version=?",
            (vault, name, version),
        )[0]
        return self._key_bundle_row(vault, row)

    @staticmethod
    def _key_bundle_row(vault: str, row) -> dict:
        return {
            "vault": vault,
            "name": row["name"],
            "version": row["version"],
            "key_type": row["key_type"],
            "key_size": row["key_size"],
            "key_ops": json.loads(row["key_ops"] or "[]"),
            "enabled": bool(row["enabled"]),
            "created": row["created"],
            "updated": row["updated"],
            "tags": json.loads(row["tags"] or "{}"),
        }

    # ------------------------------------------------------------------
    # Certificates (metadata only)
    # ------------------------------------------------------------------

    def create_certificate(self, vault: str, name: str, *,
                           subject: str = "CN=example",
                           issuer: str = "Self",
                           validity_months: int = 12,
                           enabled: bool = True,
                           tags: dict | None = None) -> dict:
        if self.store.query(
            "SELECT name FROM kv_certs_deleted WHERE vault=? AND name=?",
            (vault, name),
        ):
            raise Conflict(
                f"Certificate '{name}' in vault '{vault}' is soft-deleted; "
                "recover or purge it first."
            )
        version = _new_id()
        now = _now()
        expires = now + validity_months * 30 * 86400
        thumbprint = hashlib.sha1(
            (vault + name + version).encode()
        ).hexdigest().upper()
        self.store.execute(
            "INSERT INTO kv_certs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vault, name, version, subject, issuer, thumbprint,
             now, expires, 1 if enabled else 0,
             now, now, json.dumps(tags or {})),
        )
        return self._cert_bundle(vault, name, version)

    def get_certificate(self, vault: str, name: str,
                        version: str | None = None) -> dict:
        row = self._latest_cert(vault, name, version)
        return self._cert_bundle_row(vault, row)

    def list_certificates(self, vault: str) -> list[dict]:
        rows = self.store.query(
            """
            SELECT c.name, c.version, c.subject, c.thumbprint, c.enabled,
                   c.created, c.updated
            FROM kv_certs c
            WHERE c.vault=?
              AND c.version = (
                  SELECT version FROM kv_certs
                  WHERE vault=? AND name=c.name
                  ORDER BY created DESC LIMIT 1
              )
            ORDER BY c.name
            """,
            (vault, vault),
        )
        return [{"name": r["name"], "version": r["version"],
                 "subject": r["subject"],
                 "thumbprint": r["thumbprint"],
                 "enabled": bool(r["enabled"]),
                 "created": r["created"]}
                for r in rows]

    def delete_certificate(self, vault: str, name: str) -> dict:
        row = self._latest_cert(vault, name)
        snapshot = json.dumps({
            "name": name, "version": row["version"],
            "subject": row["subject"], "issuer": row["issuer"],
            "thumbprint": row["thumbprint"],
            "not_before": row["not_before"],
            "expires_on": row["expires_on"],
            "enabled": row["enabled"],
            "tags": row["tags"], "created": row["created"],
        })
        self.store.execute(
            "INSERT OR REPLACE INTO kv_certs_deleted VALUES (?,?,?,?)",
            (vault, name, _now(), snapshot),
        )
        self.store.execute(
            "DELETE FROM kv_certs WHERE vault=? AND name=?", (vault, name)
        )
        return {"name": name, "vault": vault, "deleted": True}

    def recover_certificate(self, vault: str, name: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM kv_certs_deleted WHERE vault=? AND name=?",
            (vault, name),
        )
        if not rows:
            raise NotFound(
                f"Deleted certificate '{name}' not found in vault '{vault}'"
            )
        snap = json.loads(rows[0]["snapshot"])
        now = _now()
        self.store.execute(
            "INSERT INTO kv_certs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vault, name, snap["version"], snap["subject"], snap["issuer"],
             snap["thumbprint"], snap["not_before"], snap["expires_on"],
             snap["enabled"], snap["created"], now, snap["tags"]),
        )
        self.store.execute(
            "DELETE FROM kv_certs_deleted WHERE vault=? AND name=?",
            (vault, name),
        )
        return self._cert_bundle(vault, name, snap["version"])

    def purge_certificate(self, vault: str, name: str) -> None:
        if not self.store.query(
            "SELECT name FROM kv_certs_deleted WHERE vault=? AND name=?",
            (vault, name),
        ):
            raise NotFound(
                f"Deleted certificate '{name}' not found in vault '{vault}'"
            )
        self.store.execute(
            "DELETE FROM kv_certs_deleted WHERE vault=? AND name=?",
            (vault, name),
        )

    def list_deleted_certificates(self, vault: str) -> list[dict]:
        rows = self.store.query(
            "SELECT name, deleted_on FROM kv_certs_deleted WHERE vault=? ORDER BY name",
            (vault,),
        )
        return [{"name": r["name"], "deleted_on": r["deleted_on"]} for r in rows]

    def _latest_cert(self, vault: str, name: str,
                     version: str | None = None):
        if version:
            rows = self.store.query(
                "SELECT * FROM kv_certs WHERE vault=? AND name=? AND version=?",
                (vault, name, version),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM kv_certs WHERE vault=? AND name=? "
                "ORDER BY created DESC LIMIT 1",
                (vault, name),
            )
        if not rows:
            raise NotFound(
                f"Certificate '{name}' not found in vault '{vault}'"
            )
        return rows[0]

    def _cert_bundle(self, vault: str, name: str, version: str) -> dict:
        row = self.store.query(
            "SELECT * FROM kv_certs WHERE vault=? AND name=? AND version=?",
            (vault, name, version),
        )[0]
        return self._cert_bundle_row(vault, row)

    @staticmethod
    def _cert_bundle_row(vault: str, row) -> dict:
        return {
            "vault": vault,
            "name": row["name"],
            "version": row["version"],
            "subject": row["subject"],
            "issuer": row["issuer"],
            "thumbprint": row["thumbprint"],
            "not_before": row["not_before"],
            "expires_on": row["expires_on"],
            "enabled": bool(row["enabled"]),
            "created": row["created"],
            "updated": row["updated"],
            "tags": json.loads(row["tags"] or "{}"),
        }
