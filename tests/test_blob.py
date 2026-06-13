import base64
import hashlib

import pytest

from openazure.errors import NotFound, Conflict


def test_create_and_list_containers(blob):
    blob.create_container("c1")
    blob.create_container("c2")
    assert blob.list_containers() == ["c1", "c2"]


def test_duplicate_container_conflicts(blob):
    blob.create_container("c1")
    with pytest.raises(Conflict):
        blob.create_container("c1")


def test_put_get_blob_roundtrip(blob):
    blob.create_container("docs")
    data = b"hello openazure"
    meta = blob.put_blob("docs", "greeting.txt", data, "text/plain")
    assert meta["size"] == len(data)
    got = blob.get_blob("docs", "greeting.txt")
    assert got["content"] == data
    assert got["content_type"] == "text/plain"
    # Content-MD5 matches Azure base64(md5) semantics
    expected_md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    assert got["content_md5"] == expected_md5


def test_put_str_is_encoded(blob):
    blob.create_container("c")
    blob.put_blob("c", "a", "naïve text")
    assert blob.get_blob("c", "a")["content"] == "naïve text".encode("utf-8")


def test_overwrite_changes_etag(blob):
    blob.create_container("c")
    m1 = blob.put_blob("c", "k", b"one")
    m2 = blob.put_blob("c", "k", b"two")
    assert m1["etag"] != m2["etag"]
    assert blob.get_blob("c", "k")["content"] == b"two"


def test_get_missing_blob_raises(blob):
    blob.create_container("c")
    with pytest.raises(NotFound):
        blob.get_blob("c", "nope")


def test_put_into_missing_container_raises(blob):
    with pytest.raises(NotFound):
        blob.put_blob("ghost", "x", b"y")


def test_list_blobs_with_prefix(blob):
    blob.create_container("c")
    blob.put_blob("c", "logs/a.txt", b"a")
    blob.put_blob("c", "logs/b.txt", b"b")
    blob.put_blob("c", "data/c.txt", b"c")
    names = [b["name"] for b in blob.list_blobs("c", prefix="logs/")]
    assert names == ["logs/a.txt", "logs/b.txt"]
    assert len(blob.list_blobs("c")) == 3


def test_delete_blob(blob):
    blob.create_container("c")
    blob.put_blob("c", "k", b"v")
    blob.delete_blob("c", "k")
    with pytest.raises(NotFound):
        blob.get_blob("c", "k")


def test_delete_container_cascades_blobs(blob):
    blob.create_container("c")
    blob.put_blob("c", "k", b"v")
    blob.delete_container("c")
    blob.create_container("c")
    assert blob.list_blobs("c") == []


def test_get_blob_properties_excludes_content(blob):
    blob.create_container("c")
    blob.put_blob("c", "k", b"payload")
    props = blob.get_blob_properties("c", "k")
    assert "content" not in props
    assert props["size"] == len(b"payload")
