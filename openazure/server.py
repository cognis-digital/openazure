"""Single local HTTP server exposing all openazure services.

Path layout (all under the server root):

* ``GET  /``                                 health / service index
* Blob:
    * ``PUT    /blob/<container>``                    create container
    * ``DELETE /blob/<container>``                    delete container
    * ``GET    /blob/<container>?comp=list``          list blobs (prefix=)
    * ``GET    /blob``                                list containers
    * ``PUT    /blob/<container>/<blob>``             put blob (body = bytes)
    * ``GET    /blob/<container>/<blob>``             get blob (raw bytes)
    * ``DELETE /blob/<container>/<blob>``             delete blob
* Table:
    * ``PUT    /table/<table>``                       create table
    * ``DELETE /table/<table>``                       delete table
    * ``GET    /table``                               list tables
    * ``POST   /table/<table>``                       insert entity (JSON)
    * ``PUT    /table/<table>``                       upsert entity (JSON)
    * ``GET    /table/<table>?pk=&rk=``               get one entity
    * ``GET    /table/<table>?pk=``                   query partition
    * ``DELETE /table/<table>?pk=&rk=``               delete entity
* Queue:
    * ``PUT    /queue/<queue>``                       create queue
    * ``DELETE /queue/<queue>``                       delete queue
    * ``GET    /queue``                               list queues
    * ``POST   /queue/<queue>/messages``              enqueue (JSON {body})
    * ``GET    /queue/<queue>/messages?num=&vt=``     dequeue
    * ``DELETE /queue/<queue>/messages/<id>?pop=``    delete message
* Functions:
    * ``GET  /functions``                             list functions
    * ``POST /functions/<name>``                      invoke http function

The handler maps these onto the service classes; all share one
:class:`~openazure.store.Store`.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from . import __version__
from .blob import BlobService
from .table import TableService
from .queue import QueueService
from .functions import FunctionRunner
from .errors import OpenAzureError
from .store import Store


class OpenAzure:
    """Aggregates all services over a single shared store."""

    def __init__(self, data_dir: str | None = None, in_memory: bool = False):
        self.store = Store(data_dir=data_dir, in_memory=in_memory)
        self.blob = BlobService(self.store)
        self.table = TableService(self.store)
        self.queue = QueueService(self.store)
        self.functions = FunctionRunner(self.queue)

    def close(self):
        self.store.close()


def _make_handler(app: OpenAzure):
    class Handler(BaseHTTPRequestHandler):
        server_version = "openazure/" + __version__

        # -- helpers --------------------------------------------------
        def log_message(self, *args):  # silence default stderr logging
            pass

        def _send_json(self, obj, status=200):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, data: bytes, content_type: str, status=200,
                        extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, exc: Exception):
            if isinstance(exc, OpenAzureError):
                self._send_json({"error": {"code": exc.code,
                                           "message": exc.message}},
                                status=exc.http_status)
            else:
                self._send_json({"error": {"code": "InternalError",
                                           "message": str(exc)}}, status=500)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(length) if length else b""

        def _read_json(self) -> dict:
            raw = self._read_body()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _parts(self):
            parsed = urlparse(self.path)
            segs = [unquote(s) for s in parsed.path.strip("/").split("/") if s != ""]
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            return segs, qs

        # -- verbs ----------------------------------------------------
        def do_GET(self):
            try:
                segs, qs = self._parts()
                if not segs:
                    self._send_json({
                        "service": "openazure",
                        "version": __version__,
                        "services": ["blob", "table", "queue", "functions"],
                    })
                    return
                svc = segs[0]
                if svc == "blob":
                    self._get_blob(segs, qs)
                elif svc == "table":
                    self._get_table(segs, qs)
                elif svc == "queue":
                    self._get_queue(segs, qs)
                elif svc == "functions":
                    self._send_json({"http": app.functions.list_http(),
                                     "queue": app.functions.list_queue()})
                else:
                    self._send_json({"error": {"code": "NotFound",
                                               "message": "unknown service"}},
                                    status=404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_PUT(self):
            try:
                segs, qs = self._parts()
                svc = segs[0] if segs else ""
                if svc == "blob":
                    if len(segs) == 2:
                        self._send_json(app.blob.create_container(segs[1]), 201)
                    elif len(segs) >= 3:
                        name = "/".join(segs[2:])
                        ct = self.headers.get("Content-Type", "application/octet-stream")
                        res = app.blob.put_blob(segs[1], name, self._read_body(), ct)
                        self._send_json(res, 201)
                    else:
                        self._send_json({"error": {"code": "BadRequest"}}, 400)
                elif svc == "table":
                    if len(segs) == 2 and not self._has_body():
                        self._send_json(app.table.create_table(segs[1]), 201)
                    elif len(segs) == 2:
                        self._send_json(app.table.upsert_entity(segs[1], self._read_json()))
                    else:
                        self._send_json({"error": {"code": "BadRequest"}}, 400)
                elif svc == "queue":
                    if len(segs) == 2:
                        self._send_json(app.queue.create_queue(segs[1]), 201)
                    else:
                        self._send_json({"error": {"code": "BadRequest"}}, 400)
                else:
                    self._send_json({"error": {"code": "NotFound"}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def _has_body(self):
            return int(self.headers.get("Content-Length", 0) or 0) > 0

        def do_POST(self):
            try:
                segs, qs = self._parts()
                svc = segs[0] if segs else ""
                if svc == "table" and len(segs) == 2:
                    self._send_json(app.table.insert_entity(segs[1], self._read_json()), 201)
                elif svc == "queue" and len(segs) == 3 and segs[2] == "messages":
                    body = self._read_json()
                    res = app.queue.enqueue(segs[1], body.get("body", ""),
                                            float(body.get("visibility_delay", 0)))
                    self._send_json(res, 201)
                elif svc == "functions" and len(segs) == 2:
                    req = {
                        "method": "POST",
                        "headers": dict(self.headers),
                        "params": qs,
                        "body": self._read_body().decode("utf-8", "replace"),
                    }
                    res = app.functions.invoke_http(segs[1], req)
                    body = res.get("body", "")
                    if isinstance(body, (dict, list)):
                        self._send_json(body, res.get("status", 200))
                    else:
                        self._send_bytes(str(body).encode("utf-8"),
                                         "text/plain", res.get("status", 200))
                else:
                    self._send_json({"error": {"code": "NotFound"}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_DELETE(self):
            try:
                segs, qs = self._parts()
                svc = segs[0] if segs else ""
                if svc == "blob":
                    if len(segs) == 2:
                        app.blob.delete_container(segs[1])
                    else:
                        app.blob.delete_blob(segs[1], "/".join(segs[2:]))
                    self._send_json({"deleted": True})
                elif svc == "table":
                    if len(segs) == 2 and "pk" in qs:
                        app.table.delete_entity(segs[1], qs["pk"], qs["rk"])
                    elif len(segs) == 2:
                        app.table.delete_table(segs[1])
                    self._send_json({"deleted": True})
                elif svc == "queue":
                    if len(segs) == 4 and segs[2] == "messages":
                        app.queue.delete_message(segs[1], segs[3], qs.get("pop", ""))
                    elif len(segs) == 2:
                        app.queue.delete_queue(segs[1])
                    self._send_json({"deleted": True})
                else:
                    self._send_json({"error": {"code": "NotFound"}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        # -- GET sub-dispatch ----------------------------------------
        def _get_blob(self, segs, qs):
            if len(segs) == 1:
                self._send_json({"containers": app.blob.list_containers()})
            elif len(segs) == 2 and qs.get("comp") == "list":
                self._send_json({"blobs": app.blob.list_blobs(segs[1], qs.get("prefix"))})
            elif len(segs) >= 3:
                blob = app.blob.get_blob(segs[1], "/".join(segs[2:]))
                self._send_bytes(blob["content"], blob["content_type"],
                                 extra={"ETag": blob["etag"],
                                        "Content-MD5": blob["content_md5"]})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_table(self, segs, qs):
            if len(segs) == 1:
                self._send_json({"tables": app.table.list_tables()})
            elif len(segs) == 2 and "pk" in qs and "rk" in qs:
                self._send_json(app.table.get_entity(segs[1], qs["pk"], qs["rk"]))
            elif len(segs) == 2:
                pk = qs.get("pk")
                self._send_json({"entities": app.table.query_entities(segs[1], pk)})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_queue(self, segs, qs):
            if len(segs) == 1:
                self._send_json({"queues": app.queue.list_queues()})
            elif len(segs) == 3 and segs[2] == "messages":
                num = int(qs.get("num", 1))
                vt = float(qs.get("vt", 30))
                if qs.get("peek") == "true":
                    self._send_json({"messages": app.queue.peek(segs[1], num)})
                else:
                    self._send_json({"messages": app.queue.dequeue(segs[1], num, vt)})
            elif len(segs) == 2:
                self._send_json({"count": app.queue.count(segs[1])})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 10000,
                data_dir: str | None = None, in_memory: bool = False,
                app: OpenAzure | None = None) -> tuple[ThreadingHTTPServer, OpenAzure]:
    """Create (but do not start) a threaded HTTP server and its app."""
    if app is None:
        app = OpenAzure(data_dir=data_dir, in_memory=in_memory)
    httpd = ThreadingHTTPServer((host, port), _make_handler(app))
    return httpd, app


def serve(host: str = "127.0.0.1", port: int = 10000,
          data_dir: str | None = None, in_memory: bool = False):
    httpd, app = make_server(host, port, data_dir, in_memory)
    print(f"openazure listening on http://{host}:{port} "
          f"(data_dir={'memory' if in_memory or not data_dir else data_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        app.close()
