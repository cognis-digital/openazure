"""Tests for extended Blob Storage operations (this pass).

Covers: block blobs, metadata, copy, tiers, SAS token stub,
container lease stub.
"""

import base64
import hashlib

import pytest

from openazure.errors import NotFound, Conflict, BadRequest


# ---------------------------------------------------------------------------
# Block blobs
# ---------------------------------------------------------------------------

def test_stage_and_commit_block_list(blob):
    blob.create_container("c")
    blob.stage_block("c", "data.bin", "block1", b"hello ")
    blob.stage_block("c", "data.bin", "block2", b"world")
    result = blob.commit_block_list(
        "c", "data.bin", ["block1", "block2"],
        content_type="text/plain",
    )
    got = blob.get_blob("c", "data.bin")
    assert got["content"] == b"hello world"
    assert got["content_type"] == "text/plain"
    expected_md5 = base64.b64encode(
        hashlib.md5(b"hello world").digest()
    ).decode()
    assert got["content_md5"] == expected_md5


def test_stage_block_overwrite(blob):
    """Re-staging the same block_id replaces the staged content."""
    blob.create_container("c")
    blob.stage_block("c", "f.bin", "b1", b"old")
    blob.stage_block("c", "f.bin", "b1", b"new")
    blob.commit_block_list("c", "f.bin", ["b1"])
    assert blob.get_blob("c", "f.bin")["content"] == b"new"


def test_commit_missing_block_raises(blob):
    blob.create_container("c")
    blob.stage_block("c", "f.bin", "b1", b"x")
    with pytest.raises(BadRequest):
        blob.commit_block_list("c", "f.bin", ["b1", "b2"])


def test_list_blocks(blob):
    blob.create_container("c")
    blob.stage_block("c", "f.bin", "aa", b"1")
    blob.stage_block("c", "f.bin", "bb", b"2")
    blocks = blob.list_blocks("c", "f.bin")
    assert "aa" in blocks and "bb" in blocks


def test_staged_blocks_cleared_after_commit(blob):
    blob.create_container("c")
    blob.stage_block("c", "f.bin", "b1", b"data")
    blob.commit_block_list("c", "f.bin", ["b1"])
    assert blob.list_blocks("c", "f.bin") == []


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_put_blob_with_metadata(blob):
    blob.create_container("c")
    meta = {"author": "alice", "project": "demo"}
    blob.put_blob("c", "doc.txt", b"content", metadata=meta)
    props = blob.get_blob_properties("c", "doc.txt")
    assert props["metadata"] == meta


def test_set_blob_metadata(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.txt", b"data")
    blob.set_blob_metadata("c", "f.txt", {"key": "value"})
    assert blob.get_blob("c", "f.txt")["metadata"] == {"key": "value"}


def test_set_metadata_on_missing_blob_raises(blob):
    blob.create_container("c")
    with pytest.raises(NotFound):
        blob.set_blob_metadata("c", "ghost.txt", {"x": "y"})


def test_list_blobs_includes_metadata(blob):
    blob.create_container("c")
    blob.put_blob("c", "a.txt", b"a", metadata={"tag": "1"})
    blobs = blob.list_blobs("c")
    assert blobs[0]["metadata"] == {"tag": "1"}


# ---------------------------------------------------------------------------
# Blob copy
# ---------------------------------------------------------------------------

def test_copy_blob_same_container(blob):
    blob.create_container("c")
    blob.put_blob("c", "src.txt", b"original", "text/plain",
                  metadata={"src": "yes"})
    blob.copy_blob("c", "src.txt", "c", "dst.txt")
    dst = blob.get_blob("c", "dst.txt")
    assert dst["content"] == b"original"
    assert dst["content_type"] == "text/plain"
    assert dst["metadata"] == {"src": "yes"}


def test_copy_blob_across_containers(blob):
    blob.create_container("src")
    blob.create_container("dst")
    blob.put_blob("src", "file.bin", b"\x00\x01\x02")
    blob.copy_blob("src", "file.bin", "dst", "copy.bin")
    assert blob.get_blob("dst", "copy.bin")["content"] == b"\x00\x01\x02"


def test_copy_missing_src_raises(blob):
    blob.create_container("c")
    with pytest.raises(NotFound):
        blob.copy_blob("c", "noexist.txt", "c", "dst.txt")


# ---------------------------------------------------------------------------
# Access tiers
# ---------------------------------------------------------------------------

def test_put_blob_with_tier(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.bin", b"data", tier="Cool")
    assert blob.get_blob("c", "f.bin")["tier"] == "Cool"


def test_set_blob_tier(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.bin", b"data", tier="Hot")
    blob.set_blob_tier("c", "f.bin", "Archive")
    assert blob.get_blob("c", "f.bin")["tier"] == "Archive"


def test_set_blob_tier_invalid_raises(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.bin", b"data")
    with pytest.raises(BadRequest):
        blob.set_blob_tier("c", "f.bin", "Glacier")


def test_list_blobs_includes_tier(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.bin", b"x", tier="Cool")
    blobs = blob.list_blobs("c")
    assert blobs[0]["tier"] == "Cool"


# ---------------------------------------------------------------------------
# SAS token stub
# ---------------------------------------------------------------------------

def test_generate_sas_returns_string(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.txt", b"x")
    token = blob.generate_sas("c", "f.txt")
    assert isinstance(token, str)
    assert "sig=" in token
    assert "se=" in token
    assert "sv=" in token


def test_generate_sas_different_permissions(blob):
    blob.create_container("c")
    blob.put_blob("c", "f.txt", b"x")
    token_r = blob.generate_sas("c", "f.txt", permissions="r")
    token_rw = blob.generate_sas("c", "f.txt", permissions="rw")
    # Different permissions must produce different signatures
    assert token_r != token_rw


# ---------------------------------------------------------------------------
# Container lease stub
# ---------------------------------------------------------------------------

def test_acquire_and_release_lease(blob):
    blob.create_container("c")
    lease_id = blob.acquire_lease("c")
    assert isinstance(lease_id, str) and len(lease_id) > 0
    blob.release_lease("c", lease_id)


def test_double_acquire_raises(blob):
    blob.create_container("c")
    blob.acquire_lease("c")
    with pytest.raises(Conflict):
        blob.acquire_lease("c")


def test_release_wrong_lease_raises(blob):
    blob.create_container("c")
    blob.acquire_lease("c")
    with pytest.raises(BadRequest):
        blob.release_lease("c", "wrongid")


def test_acquire_on_missing_container_raises(blob):
    with pytest.raises(NotFound):
        blob.acquire_lease("ghost")
