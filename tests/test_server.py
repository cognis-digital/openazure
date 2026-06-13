import json
import threading
import urllib.request
import urllib.error

import pytest

from openazure.server import make_server


@pytest.fixture
def live_server():
    # port 0 -> OS-assigned free port; in-memory store
    httpd, app = make_server(host="127.0.0.1", port=0, in_memory=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield base, app
    httpd.shutdown()
    app.close()


def _req(method, url, data=None, content_type="application/json"):
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", content_type)
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


def test_index(live_server):
    base, _ = live_server
    status, body, _ = _req("GET", base + "/")
    assert status == 200
    assert set(body["services"]) == {"blob", "table", "queue", "functions"}


def test_blob_http_roundtrip(live_server):
    base, _ = live_server
    assert _req("PUT", base + "/blob/c")[0] == 201
    s, _, _ = _req("PUT", base + "/blob/c/hello.txt", b"hi there", "text/plain")
    assert s == 201
    status, payload, headers = _req("GET", base + "/blob/c/hello.txt")
    assert status == 200
    assert payload == b"hi there"
    assert headers.get("ETag")
    # list
    status, body, _ = _req("GET", base + "/blob/c?comp=list")
    assert [b["name"] for b in body["blobs"]] == ["hello.txt"]
    # delete
    assert _req("DELETE", base + "/blob/c/hello.txt")[0] == 200
    assert _req("GET", base + "/blob/c/hello.txt")[0] == 404


def test_table_http_roundtrip(live_server):
    base, _ = live_server
    assert _req("PUT", base + "/table/People")[0] == 201
    s, _, _ = _req("POST", base + "/table/People",
                   {"PartitionKey": "us", "RowKey": "a", "age": 41})
    assert s == 201
    status, body, _ = _req("GET", base + "/table/People?pk=us&rk=a")
    assert status == 200 and body["age"] == 41
    # query partition
    status, body, _ = _req("GET", base + "/table/People?pk=us")
    assert len(body["entities"]) == 1
    # delete entity
    assert _req("DELETE", base + "/table/People?pk=us&rk=a")[0] == 200
    assert _req("GET", base + "/table/People?pk=us&rk=a")[0] == 404


def test_queue_http_roundtrip(live_server):
    base, _ = live_server
    assert _req("PUT", base + "/queue/jobs")[0] == 201
    assert _req("POST", base + "/queue/jobs/messages", {"body": "work-1"})[0] == 201
    status, body, _ = _req("GET", base + "/queue/jobs/messages?num=1&vt=30")
    assert status == 200
    msg = body["messages"][0]
    assert msg["body"] == "work-1"
    # delete with receipt
    url = f"{base}/queue/jobs/messages/{msg['id']}?pop={msg['pop_receipt']}"
    assert _req("DELETE", url)[0] == 200
    status, body, _ = _req("GET", base + "/queue/jobs")
    assert body["count"] == 0


def test_function_http_invoke(live_server):
    base, app = live_server

    @app.functions.http_function("upper")
    def upper(req):
        return {"status": 200, "body": (req["body"] or "").upper()}

    status, body, _ = _req("POST", base + "/functions/upper", b"hello",
                           content_type="text/plain")
    assert status == 200
    assert body == b"HELLO"
    status, body, _ = _req("GET", base + "/functions")
    assert "upper" in body["http"]


def test_404_for_missing_container(live_server):
    base, _ = live_server
    status, body, _ = _req("GET", base + "/blob/ghost?comp=list")
    assert status == 404
    assert body["error"]["code"] == "ResourceNotFound"
