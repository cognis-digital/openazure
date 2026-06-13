"""Tests for the Managed Identity / Azure AD-lite emulation."""

import pytest

from openazure.store import Store
from openazure.managedidentity import ManagedIdentityService, Unauthorized
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def mi(store):
    return ManagedIdentityService(store)


# ---------------------------------------------------------------------------
# Identity management
# ---------------------------------------------------------------------------

def test_register_list_get_identity(mi):
    r = mi.register_identity("my-app")
    assert r["name"] == "my-app"
    assert r["client_id"]
    assert r["type"] == "UserAssigned"

    identities = mi.list_identities()
    assert any(i["name"] == "my-app" for i in identities)

    got = mi.get_identity("my-app")
    assert got["id"] == r["id"]


def test_duplicate_identity_raises_conflict(mi):
    mi.register_identity("app")
    with pytest.raises(Conflict):
        mi.register_identity("app")


def test_get_missing_identity_raises_not_found(mi):
    with pytest.raises(NotFound):
        mi.get_identity("ghost")


def test_delete_identity(mi):
    mi.register_identity("temp")
    mi.delete_identity("temp")
    with pytest.raises(NotFound):
        mi.get_identity("temp")


def test_delete_missing_identity_raises(mi):
    with pytest.raises(NotFound):
        mi.delete_identity("nobody")


def test_identity_has_unique_client_id(mi):
    a = mi.register_identity("a")
    b = mi.register_identity("b")
    assert a["client_id"] != b["client_id"]


# ---------------------------------------------------------------------------
# Role assignments
# ---------------------------------------------------------------------------

def test_assign_and_list_roles(mi):
    mi.register_identity("svc")
    mi.assign_role("svc", "Storage.Reader", scope="/subscriptions/sub1")
    mi.assign_role("svc", "Contributor", scope="/")
    roles = mi.list_roles("svc")
    role_names = [r["role"] for r in roles]
    assert "Storage.Reader" in role_names
    assert "Contributor" in role_names


def test_assign_duplicate_role_is_idempotent(mi):
    mi.register_identity("svc2")
    r1 = mi.assign_role("svc2", "Reader")
    r2 = mi.assign_role("svc2", "Reader")
    assert r1["id"] == r2["id"]


def test_remove_role(mi):
    mi.register_identity("svc3")
    mi.assign_role("svc3", "Contributor", scope="/")
    mi.remove_role("svc3", "Contributor", scope="/")
    roles = mi.list_roles("svc3")
    assert not any(r["role"] == "Contributor" for r in roles)


def test_assign_role_to_missing_identity(mi):
    with pytest.raises(NotFound):
        mi.assign_role("ghost", "Reader")


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------

def test_issue_token_returns_bearer_token(mi):
    mi.register_identity("webapp")
    result = mi.issue_token("webapp", scope="https://storage.azure.com/.default")
    assert result["token_type"] == "Bearer"
    assert result["access_token"]
    assert result["expires_in"] > 0
    assert result["scope"] == "https://storage.azure.com/.default"


def test_issue_token_for_missing_identity(mi):
    with pytest.raises(NotFound):
        mi.issue_token("nobody")


def test_issue_token_disabled_identity(mi):
    mi.register_identity("disabled-app", enabled=False)
    with pytest.raises(BadRequest):
        mi.issue_token("disabled-app")


def test_token_has_three_parts(mi):
    mi.register_identity("jwt-test")
    result = mi.issue_token("jwt-test")
    parts = result["access_token"].split(".")
    assert len(parts) == 3


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def test_validate_valid_token(mi):
    mi.register_identity("checker")
    token_result = mi.issue_token("checker", scope="openid")
    claims = mi.validate_token(token_result["access_token"])
    assert claims["name"] == "checker"
    assert claims["scope"] == "openid"


def test_validate_tampered_token_raises(mi):
    mi.register_identity("tamper-test")
    result = mi.issue_token("tamper-test")
    token = result["access_token"]
    parts = token.split(".")
    tampered = parts[0] + "." + parts[1] + ".badsignature"
    with pytest.raises(Unauthorized):
        mi.validate_token(tampered)


def test_validate_garbage_raises(mi):
    with pytest.raises(Unauthorized):
        mi.validate_token("not-a-token")


def test_validate_expired_token_raises(mi):
    mi.register_identity("expiry-test")
    result = mi.issue_token("expiry-test", lifetime=1)
    # Manually forge an expired token by using a past exp value
    import time as _time
    import json as _json
    from openazure.managedidentity import _b64url_decode, _b64url_encode
    parts = result["access_token"].split(".")
    payload = _json.loads(_b64url_decode(parts[1]))
    payload["exp"] = int(_time.time()) - 3600  # 1 hour in the past
    expired_payload_b64 = _b64url_encode(
        _json.dumps(payload, separators=(",", ":")).encode()
    )
    # Re-sign with the real key so signature passes but time check fails
    real_sig = mi._compute_sig(parts[0] + "." + expired_payload_b64)
    expired_token = f"{parts[0]}.{expired_payload_b64}.{real_sig}"
    with pytest.raises(Unauthorized):
        mi.validate_token(expired_token)


def test_revoke_token(mi):
    mi.register_identity("revoke-test")
    result = mi.issue_token("revoke-test")
    token = result["access_token"]
    claims = mi.validate_token(token)
    mi.revoke_token(claims["jti"])
    with pytest.raises(Unauthorized):
        mi.validate_token(token)


def test_two_identities_same_store_independent_tokens(mi):
    mi.register_identity("app-x")
    mi.register_identity("app-y")
    tx = mi.issue_token("app-x")["access_token"]
    ty = mi.issue_token("app-y")["access_token"]
    assert tx != ty
    cx = mi.validate_token(tx)
    cy = mi.validate_token(ty)
    assert cx["name"] == "app-x"
    assert cy["name"] == "app-y"
