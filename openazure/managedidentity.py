"""Azure Managed Identity / Azure AD-lite emulation.

Supports:
* Identity registration — register a named identity (user-assigned MI or
  service principal placeholder) with an optional set of roles/scopes.
* Token issuance — issue a signed bearer token (HMAC-SHA256 over a JSON
  payload, base64url-encoded) for a given identity + scope.
* Token validation — validate a bearer token string; returns the decoded
  claims or raises ``Unauthorized``.
* Role assignment — assign roles to identities so downstream services can
  check authorization.

Tokens follow a minimal JWT-like structure:
    <base64url(header)>.<base64url(payload)>.<base64url(sig)>

The signing secret is a per-store random 32-byte key (regenerated on each
process start when using in-memory mode, or stored as a row in the DB for
persistence).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


class Unauthorized(Exception):
    """Raised when a bearer token is invalid or expired."""
    http_status = 401
    code = "Unauthorized"

    def __init__(self, message: str = "Unauthorized"):
        super().__init__(message)
        self.message = message


def _now() -> float:
    return time.time()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


class ManagedIdentityService:
    """Local emulation of Azure Managed Identity / Azure AD token service."""

    DEFAULT_TOKEN_LIFETIME = 3600  # 1 hour

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()
        self._signing_key = self._load_or_create_signing_key()

    def _init_schema(self):
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS mi_signing_key (
                id INTEGER PRIMARY KEY,
                key_hex TEXT NOT NULL
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS mi_identities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                client_id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL DEFAULT 'UserAssigned',
                enabled INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,
                tags TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS mi_role_assignments (
                id TEXT PRIMARY KEY,
                identity_id TEXT NOT NULL,
                role TEXT NOT NULL,
                scope TEXT NOT NULL,
                created REAL NOT NULL
            )
        """)
        # Issued tokens (for revocation / introspection)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS mi_tokens (
                jti TEXT PRIMARY KEY,
                identity_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )
        """)

    def _load_or_create_signing_key(self) -> bytes:
        rows = self.store.query("SELECT key_hex FROM mi_signing_key WHERE id=1")
        if rows:
            return bytes.fromhex(rows[0]["key_hex"])
        key = os.urandom(32)
        self.store.execute(
            "INSERT INTO mi_signing_key VALUES (1, ?)", (key.hex(),)
        )
        return key

    # ------------------------------------------------------------------
    # Identity management
    # ------------------------------------------------------------------

    def register_identity(self, name: str, *,
                          identity_type: str = "UserAssigned",
                          enabled: bool = True,
                          tags: dict | None = None) -> dict:
        if self.store.query(
            "SELECT id FROM mi_identities WHERE name=?", (name,)
        ):
            raise Conflict(f"Identity '{name}' already exists")
        identity_id = uuid.uuid4().hex
        client_id = str(uuid.uuid4())
        now = _now()
        self.store.execute(
            "INSERT INTO mi_identities VALUES (?,?,?,?,?,?,?)",
            (identity_id, name, client_id, identity_type,
             1 if enabled else 0, now, json.dumps(tags or {})),
        )
        return self._identity_dict(identity_id)

    def get_identity(self, name: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM mi_identities WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"Identity '{name}' not found")
        return self._identity_dict(rows[0]["id"])

    def list_identities(self) -> list[dict]:
        rows = self.store.query(
            "SELECT id FROM mi_identities ORDER BY name"
        )
        return [self._identity_dict(r["id"]) for r in rows]

    def delete_identity(self, name: str) -> None:
        rows = self.store.query(
            "SELECT id FROM mi_identities WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"Identity '{name}' not found")
        identity_id = rows[0]["id"]
        self.store.execute(
            "DELETE FROM mi_role_assignments WHERE identity_id=?",
            (identity_id,),
        )
        self.store.execute(
            "DELETE FROM mi_tokens WHERE identity_id=?", (identity_id,)
        )
        self.store.execute(
            "DELETE FROM mi_identities WHERE id=?", (identity_id,)
        )

    def _identity_dict(self, identity_id: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM mi_identities WHERE id=?", (identity_id,)
        )
        if not rows:
            raise NotFound(f"Identity '{identity_id}' not found")
        r = rows[0]
        roles = self.store.query(
            "SELECT role, scope FROM mi_role_assignments WHERE identity_id=?",
            (identity_id,),
        )
        return {
            "id": r["id"],
            "name": r["name"],
            "client_id": r["client_id"],
            "type": r["type"],
            "enabled": bool(r["enabled"]),
            "created": r["created"],
            "tags": json.loads(r["tags"] or "{}"),
            "roles": [{"role": ro["role"], "scope": ro["scope"]}
                      for ro in roles],
        }

    # ------------------------------------------------------------------
    # Role assignments
    # ------------------------------------------------------------------

    def assign_role(self, identity_name: str, role: str,
                    scope: str = "/") -> dict:
        rows = self.store.query(
            "SELECT id FROM mi_identities WHERE name=?", (identity_name,)
        )
        if not rows:
            raise NotFound(f"Identity '{identity_name}' not found")
        identity_id = rows[0]["id"]
        # Deduplicate
        existing = self.store.query(
            "SELECT id FROM mi_role_assignments "
            "WHERE identity_id=? AND role=? AND scope=?",
            (identity_id, role, scope),
        )
        if existing:
            return {"id": existing[0]["id"], "identity": identity_name,
                    "role": role, "scope": scope}
        assign_id = uuid.uuid4().hex
        self.store.execute(
            "INSERT INTO mi_role_assignments VALUES (?,?,?,?,?)",
            (assign_id, identity_id, role, scope, _now()),
        )
        return {"id": assign_id, "identity": identity_name,
                "role": role, "scope": scope}

    def remove_role(self, identity_name: str, role: str,
                    scope: str = "/") -> None:
        rows = self.store.query(
            "SELECT id FROM mi_identities WHERE name=?", (identity_name,)
        )
        if not rows:
            raise NotFound(f"Identity '{identity_name}' not found")
        identity_id = rows[0]["id"]
        self.store.execute(
            "DELETE FROM mi_role_assignments "
            "WHERE identity_id=? AND role=? AND scope=?",
            (identity_id, role, scope),
        )

    def list_roles(self, identity_name: str) -> list[dict]:
        rows = self.store.query(
            "SELECT id FROM mi_identities WHERE name=?", (identity_name,)
        )
        if not rows:
            raise NotFound(f"Identity '{identity_name}' not found")
        identity_id = rows[0]["id"]
        role_rows = self.store.query(
            "SELECT role, scope FROM mi_role_assignments WHERE identity_id=?",
            (identity_id,),
        )
        return [{"role": r["role"], "scope": r["scope"]} for r in role_rows]

    # ------------------------------------------------------------------
    # Token issuance and validation
    # ------------------------------------------------------------------

    def issue_token(self, identity_name: str, scope: str = "/.default",
                    lifetime: int | None = None) -> dict:
        """Issue a signed bearer token for the given identity."""
        rows = self.store.query(
            "SELECT * FROM mi_identities WHERE name=?", (identity_name,)
        )
        if not rows:
            raise NotFound(f"Identity '{identity_name}' not found")
        r = rows[0]
        if not bool(r["enabled"]):
            raise BadRequest(f"Identity '{identity_name}' is disabled")

        ttl = lifetime or self.DEFAULT_TOKEN_LIFETIME
        now = _now()
        jti = uuid.uuid4().hex
        payload = {
            "jti": jti,
            "sub": r["id"],
            "name": r["name"],
            "client_id": r["client_id"],
            "type": r["type"],
            "scope": scope,
            "iat": int(now),
            "exp": int(now + ttl),
        }
        token = self._sign_token(payload)
        self.store.execute(
            "INSERT INTO mi_tokens VALUES (?,?,?,?,?,0)",
            (jti, r["id"], scope, now, now + ttl),
        )
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": scope,
            "client_id": r["client_id"],
        }

    def validate_token(self, token: str) -> dict:
        """Validate a bearer token; returns claims dict or raises Unauthorized."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("wrong part count")
            header_b64, payload_b64, sig_b64 = parts
            expected_sig = self._compute_sig(header_b64 + "." + payload_b64)
            if not hmac.compare_digest(expected_sig, sig_b64):
                raise ValueError("signature mismatch")
            payload = json.loads(_b64url_decode(payload_b64))
        except Exception as exc:
            raise Unauthorized(f"Invalid token: {exc}") from exc

        # Expiry
        if payload.get("exp", 0) < _now():
            raise Unauthorized("Token has expired")

        # Check revocation
        rows = self.store.query(
            "SELECT revoked FROM mi_tokens WHERE jti=?",
            (payload.get("jti", ""),),
        )
        if rows and rows[0]["revoked"]:
            raise Unauthorized("Token has been revoked")

        return payload

    def revoke_token(self, jti: str) -> None:
        self.store.execute(
            "UPDATE mi_tokens SET revoked=1 WHERE jti=?", (jti,)
        )

    # ------------------------------------------------------------------
    # Internal signing helpers
    # ------------------------------------------------------------------

    def _sign_token(self, payload: dict) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = _b64url_encode(
            json.dumps(payload, separators=(",", ":")).encode()
        )
        sig = self._compute_sig(header_b64 + "." + payload_b64)
        return f"{header_b64}.{payload_b64}.{sig}"

    def _compute_sig(self, message: str) -> str:
        sig = hmac.new(self._signing_key,
                       message.encode("utf-8"),
                       hashlib.sha256).digest()
        return _b64url_encode(sig)
