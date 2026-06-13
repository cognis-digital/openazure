"""Tests for the Key Vault emulation."""

import base64
import pytest

from openazure.store import Store
from openazure.keyvault import KeyVaultService
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def kv(store):
    return KeyVaultService(store)


VAULT = "my-vault"


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_set_get_secret(kv):
    res = kv.set_secret(VAULT, "db-pass", "hunter2")
    assert res["name"] == "db-pass"
    assert res["value"] == "hunter2"
    assert res["vault"] == VAULT
    got = kv.get_secret(VAULT, "db-pass")
    assert got["value"] == "hunter2"


def test_set_secret_produces_new_version(kv):
    r1 = kv.set_secret(VAULT, "api-key", "v1")
    r2 = kv.set_secret(VAULT, "api-key", "v2")
    assert r1["version"] != r2["version"]
    got = kv.get_secret(VAULT, "api-key")
    assert got["value"] == "v2"  # latest


def test_get_secret_by_version(kv):
    r1 = kv.set_secret(VAULT, "s", "old")
    kv.set_secret(VAULT, "s", "new")
    assert kv.get_secret(VAULT, "s", r1["version"])["value"] == "old"


def test_list_secrets(kv):
    kv.set_secret(VAULT, "a", "1")
    kv.set_secret(VAULT, "b", "2")
    names = [s["name"] for s in kv.list_secrets(VAULT)]
    assert set(names) == {"a", "b"}


def test_list_secret_versions(kv):
    kv.set_secret(VAULT, "m", "v1")
    kv.set_secret(VAULT, "m", "v2")
    vers = kv.list_secret_versions(VAULT, "m")
    assert len(vers) == 2


def test_delete_secret_soft_delete(kv):
    kv.set_secret(VAULT, "gone", "x")
    kv.delete_secret(VAULT, "gone")
    with pytest.raises(NotFound):
        kv.get_secret(VAULT, "gone")
    deleted = kv.list_deleted_secrets(VAULT)
    assert any(d["name"] == "gone" for d in deleted)


def test_recover_secret(kv):
    kv.set_secret(VAULT, "r", "val")
    kv.delete_secret(VAULT, "r")
    kv.recover_secret(VAULT, "r")
    assert kv.get_secret(VAULT, "r")["value"] == "val"


def test_purge_secret(kv):
    kv.set_secret(VAULT, "p", "val")
    kv.delete_secret(VAULT, "p")
    kv.purge_secret(VAULT, "p")
    with pytest.raises(NotFound):
        kv.recover_secret(VAULT, "p")


def test_set_on_soft_deleted_raises_conflict(kv):
    kv.set_secret(VAULT, "blocked", "v")
    kv.delete_secret(VAULT, "blocked")
    with pytest.raises(Conflict):
        kv.set_secret(VAULT, "blocked", "new")


def test_secret_not_found(kv):
    with pytest.raises(NotFound):
        kv.get_secret(VAULT, "ghost")


def test_secret_with_content_type_and_tags(kv):
    r = kv.set_secret(VAULT, "typed", "val",
                      content_type="text/plain",
                      tags={"env": "prod"})
    assert r["content_type"] == "text/plain"
    assert r["tags"]["env"] == "prod"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_create_get_key(kv):
    r = kv.create_key(VAULT, "mykey", key_type="RSA")
    assert r["name"] == "mykey"
    assert r["key_type"] == "RSA"
    got = kv.get_key(VAULT, "mykey")
    assert got["version"] == r["version"]


def test_create_key_new_version(kv):
    r1 = kv.create_key(VAULT, "k", key_type="RSA")
    r2 = kv.create_key(VAULT, "k2", key_type="EC")
    assert r1["name"] != r2["name"]


def test_list_keys(kv):
    kv.create_key(VAULT, "k1")
    kv.create_key(VAULT, "k2")
    names = [k["name"] for k in kv.list_keys(VAULT)]
    assert "k1" in names and "k2" in names


def test_delete_recover_key(kv):
    kv.create_key(VAULT, "delme")
    kv.delete_key(VAULT, "delme")
    with pytest.raises(NotFound):
        kv.get_key(VAULT, "delme")
    kv.recover_key(VAULT, "delme")
    assert kv.get_key(VAULT, "delme") is not None


def test_purge_key(kv):
    kv.create_key(VAULT, "pk")
    kv.delete_key(VAULT, "pk")
    kv.purge_key(VAULT, "pk")
    with pytest.raises(NotFound):
        kv.recover_key(VAULT, "pk")


def test_invalid_key_type(kv):
    with pytest.raises(BadRequest):
        kv.create_key(VAULT, "bad", key_type="XYZ")


# ---------------------------------------------------------------------------
# Encrypt / Decrypt / Wrap / Unwrap
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip(kv):
    kv.create_key(VAULT, "enc-key")
    plaintext = b"hello openazure"
    pt_b64 = base64.b64encode(plaintext).decode()
    enc = kv.encrypt(VAULT, "enc-key", pt_b64)
    assert "ciphertext" in enc
    assert enc["ciphertext"] != pt_b64

    dec = kv.decrypt(VAULT, "enc-key", enc["ciphertext"])
    recovered = base64.b64decode(dec["plaintext"])
    assert recovered == plaintext


def test_encrypt_different_keys_produce_different_ciphertext(kv):
    kv.create_key(VAULT, "ka")
    kv.create_key(VAULT, "kb")
    pt_b64 = base64.b64encode(b"secret").decode()
    ca = kv.encrypt(VAULT, "ka", pt_b64)["ciphertext"]
    cb = kv.encrypt(VAULT, "kb", pt_b64)["ciphertext"]
    assert ca != cb


def test_wrap_unwrap_key(kv):
    kv.create_key(VAULT, "wrap-key")
    sym_key = base64.b64encode(b"\xde\xad\xbe\xef" * 4).decode()
    wrapped = kv.wrap_key(VAULT, "wrap-key", sym_key)
    assert "wrapped_key" in wrapped
    unwrapped = kv.unwrap_key(VAULT, "wrap-key", wrapped["wrapped_key"])
    assert unwrapped["unwrapped_key"] == sym_key


def test_encrypt_missing_key(kv):
    pt = base64.b64encode(b"x").decode()
    with pytest.raises(NotFound):
        kv.encrypt(VAULT, "ghost-key", pt)


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

def test_create_get_certificate(kv):
    r = kv.create_certificate(VAULT, "mycert", subject="CN=test.example.com")
    assert r["name"] == "mycert"
    assert r["subject"] == "CN=test.example.com"
    assert r["thumbprint"]
    got = kv.get_certificate(VAULT, "mycert")
    assert got["thumbprint"] == r["thumbprint"]


def test_list_certificates(kv):
    kv.create_certificate(VAULT, "c1")
    kv.create_certificate(VAULT, "c2")
    names = [c["name"] for c in kv.list_certificates(VAULT)]
    assert "c1" in names and "c2" in names


def test_delete_recover_certificate(kv):
    kv.create_certificate(VAULT, "dc")
    kv.delete_certificate(VAULT, "dc")
    with pytest.raises(NotFound):
        kv.get_certificate(VAULT, "dc")
    kv.recover_certificate(VAULT, "dc")
    assert kv.get_certificate(VAULT, "dc") is not None


def test_purge_certificate(kv):
    kv.create_certificate(VAULT, "purgeme")
    kv.delete_certificate(VAULT, "purgeme")
    kv.purge_certificate(VAULT, "purgeme")
    with pytest.raises(NotFound):
        kv.recover_certificate(VAULT, "purgeme")


def test_cert_expires_on_set(kv):
    r = kv.create_certificate(VAULT, "expiry", validity_months=6)
    assert r["expires_on"] > r["not_before"]


def test_cert_not_found(kv):
    with pytest.raises(NotFound):
        kv.get_certificate(VAULT, "ghost")


def test_list_deleted_keys(kv):
    kv.create_key(VAULT, "del1")
    kv.create_key(VAULT, "del2")
    kv.delete_key(VAULT, "del1")
    kv.delete_key(VAULT, "del2")
    deleted = kv.list_deleted_keys(VAULT)
    names = [d["name"] for d in deleted]
    assert "del1" in names and "del2" in names


def test_list_deleted_certificates(kv):
    kv.create_certificate(VAULT, "dc1")
    kv.delete_certificate(VAULT, "dc1")
    deleted = kv.list_deleted_certificates(VAULT)
    assert any(d["name"] == "dc1" for d in deleted)
