"""End-to-end HTTP server tests for services added in this pass.

Drives all new endpoints through the live ThreadingHTTPServer using only
urllib (no third-party deps). Imports the same ``live_server`` and ``_req``
helpers defined inline here for clarity.
"""

import json
import threading
import urllib.request
import urllib.error

import pytest

from openazure.server import make_server


@pytest.fixture
def live_server():
    httpd, app = make_server(host="127.0.0.1", port=0, in_memory=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield base, app
    httpd.shutdown()
    app.close()


def _req(method, url, data=None, content_type="application/json",
         extra_headers=None):
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            payload = json.loads(raw) if "json" in ct else raw
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), {}
        except Exception:
            return e.code, raw, {}


# ---------------------------------------------------------------------------
# Extended Blob: block staging, metadata, tier, copy, SAS, lease
# ---------------------------------------------------------------------------

def test_blob_stage_and_commit_http(live_server):
    base, _ = live_server
    assert _req("PUT", base + "/blob/c")[0] == 201
    # stage two blocks
    s, _, _ = _req("POST", base + "/blob/c/big.bin?comp=block&blockid=b1",
                   b"chunk1", "application/octet-stream")
    assert s == 201
    s, _, _ = _req("POST", base + "/blob/c/big.bin?comp=block&blockid=b2",
                   b"chunk2", "application/octet-stream")
    assert s == 201
    # list blocks
    s, body, _ = _req("GET", base + "/blob/c/big.bin?comp=blocklist")
    assert s == 200
    assert "b1" in body["blocks"] and "b2" in body["blocks"]
    # commit
    s, body, _ = _req("PUT", base + "/blob/c/big.bin?comp=blocklist",
                       {"blocks": ["b1", "b2"], "content_type": "text/plain"})
    assert s == 201
    # verify content
    s, content, _ = _req("GET", base + "/blob/c/big.bin")
    assert s == 200
    assert content == b"chunk1chunk2"


def test_blob_metadata_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/blob/c")
    _req("PUT", base + "/blob/c/f.txt", b"data", "text/plain")
    # set metadata
    s, _, _ = _req("PUT", base + "/blob/c/f.txt?comp=metadata",
                   {"owner": "alice"})
    assert s == 200
    # list blobs and check metadata
    s, body, _ = _req("GET", base + "/blob/c?comp=list")
    assert s == 200
    assert body["blobs"][0]["metadata"] == {"owner": "alice"}


def test_blob_tier_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/blob/c")
    _req("PUT", base + "/blob/c/f.bin", b"x")
    s, body, _ = _req("PUT", base + "/blob/c/f.bin?comp=tier&tier=Cool")
    assert s == 200
    assert body["tier"] == "Cool"


def test_blob_copy_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/blob/src")
    _req("PUT", base + "/blob/dst")
    _req("PUT", base + "/blob/src/orig.txt", b"source content", "text/plain")
    s, _, _ = _req(
        "PUT",
        base + "/blob/dst/copy.txt?comp=copy&src_container=src&src_blob=orig.txt",
    )
    assert s == 201
    s, content, _ = _req("GET", base + "/blob/dst/copy.txt")
    assert content == b"source content"


def test_blob_sas_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/blob/c")
    _req("PUT", base + "/blob/c/f.txt", b"x")
    s, body, _ = _req("GET", base + "/blob/c/f.txt?comp=sas")
    assert s == 200
    assert "sig=" in body["sas_token"]


def test_blob_lease_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/blob/c")
    s, body, _ = _req("POST", base + "/blob/c?comp=lease&action=acquire",
                       data=b"")
    # Note: acquire lease via PUT /blob/c?comp=lease&action=acquire
    s, body, _ = _req("PUT", base + "/blob/c?comp=lease&action=acquire")
    assert s == 201
    lease_id = body["lease_id"]
    # double acquire should fail
    s2, _, _ = _req("PUT", base + "/blob/c?comp=lease&action=acquire")
    assert s2 == 409
    # release
    s3, _, _ = _req(
        "PUT", f"{base}/blob/c?comp=lease&action=release&lease={lease_id}"
    )
    assert s3 == 200


# ---------------------------------------------------------------------------
# Table: OData queries and batch via HTTP
# ---------------------------------------------------------------------------

def test_table_odata_filter_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/table/T")
    for i in range(5):
        _req("POST", base + "/table/T",
             {"PartitionKey": "p", "RowKey": str(i), "score": i * 10})
    import urllib.parse
    f = urllib.parse.quote("score gt 20")
    s, body, _ = _req("GET", f"{base}/table/T?pk=p&$filter={f}")
    assert s == 200
    assert len(body["entities"]) == 2


def test_table_top_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/table/T")
    for i in range(5):
        _req("POST", base + "/table/T",
             {"PartitionKey": "p", "RowKey": str(i), "v": i})
    s, body, _ = _req("GET", base + "/table/T?pk=p&$top=2")
    assert s == 200
    assert len(body["entities"]) == 2


def test_table_select_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/table/T")
    _req("POST", base + "/table/T",
         {"PartitionKey": "p", "RowKey": "1", "a": 1, "b": 2})
    import urllib.parse
    sel = urllib.parse.quote("a")
    s, body, _ = _req("GET", f"{base}/table/T?pk=p&$select={sel}")
    assert s == 200
    e = body["entities"][0]
    assert "a" in e and "b" not in e


def test_table_batch_http(live_server):
    base, _ = live_server
    _req("PUT", base + "/table/T")
    ops = [
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "1", "v": 10}},
        {"op": "insert", "entity": {"PartitionKey": "p", "RowKey": "2", "v": 20}},
    ]
    s, body, _ = _req("POST", base + "/table/T?comp=batch", ops)
    assert s == 200
    assert len(body["results"]) == 2
    # verify entities exist
    s2, e, _ = _req("GET", base + "/table/T?pk=p&rk=1")
    assert s2 == 200 and e["v"] == 10


# ---------------------------------------------------------------------------
# Cosmos DB HTTP round-trip
# ---------------------------------------------------------------------------

def test_cosmos_http_roundtrip(live_server):
    base, _ = live_server
    # create db
    assert _req("PUT", base + "/cosmos/mydb")[0] == 201
    # list dbs
    s, body, _ = _req("GET", base + "/cosmos")
    assert any(d["id"] == "mydb" for d in body["databases"])
    # create container
    s, _, _ = _req("PUT", base + "/cosmos/mydb/users",
                   {"partitionKey": "/country"})
    assert s == 201
    # list containers
    s, body, _ = _req("GET", base + "/cosmos/mydb?comp=containers")
    assert any(c["id"] == "users" for c in body["containers"])
    # create item
    s, item, _ = _req("POST", base + "/cosmos/mydb/users/items",
                       {"id": "u1", "country": "us", "name": "Alice"})
    assert s == 201 and item["id"] == "u1"
    # get item
    s, got, _ = _req("GET", base + "/cosmos/mydb/users/items/u1?pk=us")
    assert s == 200 and got["name"] == "Alice"
    # list items
    s, body, _ = _req("GET", base + "/cosmos/mydb/users/items?pk=us")
    assert len(body["items"]) == 1
    # query
    import urllib.parse
    payload = {"query": "SELECT * FROM c WHERE c.country = 'us'"}
    s, body, _ = _req("POST", base + "/cosmos/mydb/users/query", payload)
    assert s == 200
    assert len(body["items"]) == 1 and body["items"][0]["name"] == "Alice"
    # delete item
    assert _req("DELETE", base + "/cosmos/mydb/users/items/u1?pk=us")[0] == 200
    assert _req("GET", base + "/cosmos/mydb/users/items/u1?pk=us")[0] == 404
    # delete container
    assert _req("DELETE", base + "/cosmos/mydb/users")[0] == 200
    # delete db
    assert _req("DELETE", base + "/cosmos/mydb")[0] == 200
    assert _req("GET", base + "/cosmos/mydb")[0] == 404


# ---------------------------------------------------------------------------
# File Shares HTTP round-trip
# ---------------------------------------------------------------------------

def test_files_http_roundtrip(live_server):
    base, _ = live_server
    # create share
    s, body, _ = _req("PUT", base + "/files/myshare",
                       {"quota_gb": 100, "metadata": {"env": "test"}})
    assert s == 201
    # list shares
    s, body, _ = _req("GET", base + "/files")
    assert any(sh["name"] == "myshare" for sh in body["shares"])
    # share properties
    s, body, _ = _req("GET", base + "/files/myshare")
    assert s == 200 and body["quota_gb"] == 100
    # create directory
    s, _, _ = _req("PUT", base + "/files/myshare/docs?comp=dir", {})
    assert s == 201
    # list root directory (comp=dir on the share endpoint)
    s, body, _ = _req("GET", base + "/files/myshare?comp=dir")
    dir_names = [d["name"] for d in body["directories"]]
    assert "docs" in dir_names
    # upload file into directory
    s, _, _ = _req("PUT", base + "/files/myshare/docs/readme.txt",
                   b"readme content", "text/plain")
    assert s == 201
    # list directory
    s, body, _ = _req("GET", base + "/files/myshare/docs?comp=dir")
    assert s == 200
    file_names = [f["name"] for f in body["files"]]
    assert "readme.txt" in file_names
    # download file
    s, content, _ = _req("GET", base + "/files/myshare/docs/readme.txt")
    assert s == 200 and content == b"readme content"
    # set metadata
    s, _, _ = _req("PUT",
                   base + "/files/myshare/docs/readme.txt?comp=metadata",
                   {"owner": "bob"})
    assert s == 200
    # copy file
    s, _, _ = _req(
        "PUT",
        base + "/files/myshare/docs/copy.txt"
        "?comp=copy&src_share=myshare&src_path=docs/readme.txt",
    )
    assert s == 201
    s, content2, _ = _req("GET", base + "/files/myshare/docs/copy.txt")
    assert content2 == b"readme content"
    # delete file
    assert _req("DELETE", base + "/files/myshare/docs/copy.txt")[0] == 200
    assert _req("GET", base + "/files/myshare/docs/copy.txt")[0] == 404
    # delete directory (must be empty of our files first)
    _req("DELETE", base + "/files/myshare/docs/readme.txt")
    assert _req("DELETE", base + "/files/myshare/docs?comp=dir")[0] == 200
    # delete share
    assert _req("DELETE", base + "/files/myshare")[0] == 200
    assert _req("GET", base + "/files/myshare")[0] == 404
