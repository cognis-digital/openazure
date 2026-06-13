"""Tests for Azure File Shares service.

Covers: shares, directories, files (CRUD), copy, metadata, list directory.
"""

import base64
import hashlib

import pytest

from openazure.store import Store
from openazure.fileshare import FileShareService
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def files(store):
    return FileShareService(store)


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------

def test_create_and_list_shares(files):
    files.create_share("logs")
    files.create_share("data")
    names = [s["name"] for s in files.list_shares()]
    assert "logs" in names and "data" in names


def test_duplicate_share_raises(files):
    files.create_share("s")
    with pytest.raises(Conflict):
        files.create_share("s")


def test_get_share_properties(files):
    files.create_share("s", quota_gb=100, metadata={"env": "test"})
    props = files.get_share_properties("s")
    assert props["quota_gb"] == 100
    assert props["metadata"] == {"env": "test"}


def test_delete_share(files):
    files.create_share("s")
    files.delete_share("s")
    with pytest.raises(NotFound):
        files.get_share_properties("s")


def test_delete_missing_share_raises(files):
    with pytest.raises(NotFound):
        files.delete_share("ghost")


def test_delete_share_cascades_files(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"hello")
    files.delete_share("s")
    files.create_share("s")
    assert files.list_directory("s")["files"] == []


# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

def test_create_and_list_directory(files):
    files.create_share("s")
    files.create_directory("s", "logs")
    listing = files.list_directory("s")
    names = [d["name"] for d in listing["directories"]]
    assert "logs" in names


def test_duplicate_directory_raises(files):
    files.create_share("s")
    files.create_directory("s", "d")
    with pytest.raises(Conflict):
        files.create_directory("s", "d")


def test_nested_directory(files):
    files.create_share("s")
    files.create_directory("s", "a")
    files.create_directory("s", "a/b")
    listing = files.list_directory("s", "a")
    names = [d["name"] for d in listing["directories"]]
    assert "b" in names


def test_nested_dir_without_parent_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.create_directory("s", "missing/child")


def test_delete_empty_directory(files):
    files.create_share("s")
    files.create_directory("s", "d")
    files.delete_directory("s", "d")
    listing = files.list_directory("s")
    assert all(d["name"] != "d" for d in listing["directories"])


def test_delete_nonempty_directory_raises(files):
    files.create_share("s")
    files.create_directory("s", "d")
    files.upload_file("s", "d/f.txt", b"data")
    with pytest.raises(BadRequest):
        files.delete_directory("s", "d")


def test_list_directory_with_path(files):
    files.create_share("s")
    files.create_directory("s", "logs")
    files.create_directory("s", "logs/app")
    files.upload_file("s", "logs/a.txt", b"a")
    files.upload_file("s", "logs/b.txt", b"b")
    listing = files.list_directory("s", "logs")
    file_names = [f["name"] for f in listing["files"]]
    dir_names = [d["name"] for d in listing["directories"]]
    assert "a.txt" in file_names and "b.txt" in file_names
    assert "app" in dir_names


def test_list_missing_directory_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.list_directory("s", "ghost")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def test_upload_and_download_file(files):
    files.create_share("s")
    data = b"hello files"
    files.upload_file("s", "hello.txt", data, "text/plain")
    got = files.get_file("s", "hello.txt")
    assert got["content"] == data
    assert got["content_type"] == "text/plain"
    expected_md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    assert got["content_md5"] == expected_md5


def test_upload_str_data_encoded(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", "naïve text")
    assert files.get_file("s", "f.txt")["content"] == "naïve text".encode("utf-8")


def test_overwrite_file(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"v1")
    files.upload_file("s", "f.txt", b"v2")
    assert files.get_file("s", "f.txt")["content"] == b"v2"


def test_get_missing_file_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.get_file("s", "ghost.txt")


def test_upload_file_missing_share_raises(files):
    with pytest.raises(NotFound):
        files.upload_file("ghost", "f.txt", b"data")


def test_upload_file_missing_parent_dir_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.upload_file("s", "nodir/f.txt", b"data")


def test_delete_file(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"x")
    files.delete_file("s", "f.txt")
    with pytest.raises(NotFound):
        files.get_file("s", "f.txt")


def test_delete_missing_file_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.delete_file("s", "ghost.txt")


def test_get_file_properties(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"content")
    props = files.get_file_properties("s", "f.txt")
    assert "content" not in props
    assert props["size"] == len(b"content")


def test_file_in_subdirectory(files):
    files.create_share("s")
    files.create_directory("s", "logs")
    files.upload_file("s", "logs/app.log", b"log data")
    assert files.get_file("s", "logs/app.log")["content"] == b"log data"


# ---------------------------------------------------------------------------
# File metadata
# ---------------------------------------------------------------------------

def test_upload_file_with_metadata(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"x", metadata={"owner": "alice"})
    assert files.get_file("s", "f.txt")["metadata"] == {"owner": "alice"}


def test_set_file_metadata(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"x")
    files.set_file_metadata("s", "f.txt", {"tag": "v2"})
    assert files.get_file("s", "f.txt")["metadata"] == {"tag": "v2"}


def test_set_metadata_missing_file_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.set_file_metadata("s", "ghost.txt", {"k": "v"})


def test_list_directory_includes_metadata(files):
    files.create_share("s")
    files.upload_file("s", "f.txt", b"x", metadata={"k": "v"})
    listing = files.list_directory("s")
    assert listing["files"][0]["metadata"] == {"k": "v"}


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def test_copy_file_same_share(files):
    files.create_share("s")
    files.upload_file("s", "src.txt", b"original", "text/plain",
                      metadata={"src": "yes"})
    files.copy_file("s", "src.txt", "s", "dst.txt")
    dst = files.get_file("s", "dst.txt")
    assert dst["content"] == b"original"
    assert dst["metadata"] == {"src": "yes"}


def test_copy_file_across_shares(files):
    files.create_share("s1")
    files.create_share("s2")
    files.upload_file("s1", "f.bin", b"\x00\xff")
    files.copy_file("s1", "f.bin", "s2", "copy.bin")
    assert files.get_file("s2", "copy.bin")["content"] == b"\x00\xff"


def test_copy_missing_src_raises(files):
    files.create_share("s")
    with pytest.raises(NotFound):
        files.copy_file("s", "ghost.txt", "s", "dst.txt")
