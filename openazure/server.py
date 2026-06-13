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
    * ``POST   /blob/<container>/<blob>?comp=block&blockid=<id>``
                                                      stage a block
    * ``PUT    /blob/<container>/<blob>?comp=blocklist``
                                                      commit block list (JSON {blocks:[...]})
    * ``GET    /blob/<container>/<blob>?comp=blocklist``
                                                      list staged blocks
    * ``PUT    /blob/<container>/<blob>?comp=tier&tier=<tier>``
                                                      set blob tier
    * ``PUT    /blob/<container>/<blob>?comp=metadata``
                                                      set blob metadata (JSON body)
    * ``POST   /blob/<container>?comp=lease&action=acquire``
                                                      acquire container lease
    * ``PUT    /blob/<container>?comp=lease&action=release&lease=<id>``
                                                      release container lease
    * ``PUT    /blob/<dst_container>/<dst_blob>?comp=copy&src_container=<c>&src_blob=<b>``
                                                      server-side blob copy
    * ``GET    /blob/<container>/<blob>?comp=sas``    generate SAS stub
* Table:
    * ``PUT    /table/<table>``                       create table
    * ``DELETE /table/<table>``                       delete table
    * ``GET    /table``                               list tables
    * ``POST   /table/<table>``                       insert entity (JSON)
    * ``PUT    /table/<table>``                       upsert entity (JSON)
    * ``GET    /table/<table>?pk=&rk=``               get one entity
    * ``GET    /table/<table>?pk=[&$filter=][&$top=][&$select=]``
                                                      query partition (OData-lite)
    * ``DELETE /table/<table>?pk=&rk=``               delete entity
    * ``POST   /table/<table>?comp=batch``            batch transaction (JSON list)
* Queue:
    * ``PUT    /queue/<queue>``                       create queue
    * ``DELETE /queue/<queue>``                       delete queue
    * ``GET    /queue``                               list queues
    * ``POST   /queue/<queue>/messages``              enqueue (JSON {body})
    * ``GET    /queue/<queue>/messages?num=&vt=``     dequeue (or peek=true)
    * ``DELETE /queue/<queue>/messages/<id>?pop=``    delete message
    * ``PATCH  /queue/<queue>/messages/<id>?pop=&vt=``
                                                      update message visibility/body
* Functions:
    * ``GET  /functions``                             list functions
    * ``POST /functions/<name>``                      invoke http function
* Cosmos DB:
    * ``PUT    /cosmos/<db>``                         create database
    * ``DELETE /cosmos/<db>``                         delete database
    * ``GET    /cosmos``                              list databases
    * ``GET    /cosmos/<db>``                         get database
    * ``PUT    /cosmos/<db>/<container>``             create container (JSON {partitionKey})
    * ``DELETE /cosmos/<db>/<container>``             delete container
    * ``GET    /cosmos/<db>?comp=containers``         list containers
    * ``GET    /cosmos/<db>/<container>``             get container
    * ``POST   /cosmos/<db>/<container>/items``       create item (JSON)
    * ``PUT    /cosmos/<db>/<container>/items``       upsert item (JSON)
    * ``GET    /cosmos/<db>/<container>/items``       list items [?pk=]
    * ``GET    /cosmos/<db>/<container>/items/<id>``  get item [?pk=]
    * ``DELETE /cosmos/<db>/<container>/items/<id>``  delete item [?pk=]
    * ``POST   /cosmos/<db>/<container>/query``       query (JSON {query:...,[pk:]})
* File Shares:
    * ``PUT    /files/<share>``                       create share (JSON {quota_gb})
    * ``DELETE /files/<share>``                       delete share
    * ``GET    /files``                               list shares
    * ``GET    /files/<share>``                       share properties
    * ``PUT    /files/<share>/<path>?comp=dir``       create directory
    * ``DELETE /files/<share>/<path>?comp=dir``       delete directory
    * ``GET    /files/<share>/<path>?comp=dir``       list directory
    * ``PUT    /files/<share>/<path>``                upload file (body=bytes)
    * ``GET    /files/<share>/<path>``                download file
    * ``DELETE /files/<share>/<path>``                delete file
    * ``PUT    /files/<share>/<path>?comp=metadata``  set file metadata (JSON)
    * ``PUT    /files/<share>/<path>?comp=copy&src_share=&src_path=``
                                                      server-side file copy
* Service Bus:
    * ``PUT    /servicebus/queues/<queue>``           create queue (JSON props)
    * ``DELETE /servicebus/queues/<queue>``           delete queue
    * ``GET    /servicebus/queues``                   list queues
    * ``GET    /servicebus/queues/<queue>``           queue properties
    * ``POST   /servicebus/queues/<queue>/messages``  send message (JSON {body,...})
    * ``GET    /servicebus/queues/<queue>/messages?num=&lock=``
                                                      receive messages (peek-lock)
    * ``DELETE /servicebus/queues/<queue>/messages?lock=``
                                                      complete message
    * ``POST   /servicebus/queues/<queue>/messages?comp=abandon&lock=``
                                                      abandon message
    * ``POST   /servicebus/queues/<queue>/messages?comp=deadletter&lock=``
                                                      dead-letter message
    * ``GET    /servicebus/queues/<queue>/deadletter?num=``
                                                      peek dead-letter sub-queue
    * ``PUT    /servicebus/topics/<topic>``           create topic
    * ``DELETE /servicebus/topics/<topic>``           delete topic
    * ``GET    /servicebus/topics``                   list topics
    * ``PUT    /servicebus/topics/<topic>/subscriptions/<sub>``
                                                      create subscription
    * ``DELETE /servicebus/topics/<topic>/subscriptions/<sub>``
                                                      delete subscription
    * ``GET    /servicebus/topics/<topic>/subscriptions``
                                                      list subscriptions
    * ``POST   /servicebus/topics/<topic>/messages``  publish to topic
    * ``GET    /servicebus/topics/<topic>/subscriptions/<sub>/messages``
                                                      receive from subscription
    * ``PUT    /servicebus/topics/<topic>/subscriptions/<sub>/rules/<rule>``
                                                      add SQL-filter rule
    * ``DELETE /servicebus/topics/<topic>/subscriptions/<sub>/rules/<rule>``
                                                      remove rule
    * ``GET    /servicebus/topics/<topic>/subscriptions/<sub>/rules``
                                                      list rules
* Event Hubs:
    * ``PUT    /eventhubs/<hub>``                     create hub (JSON {partition_count})
    * ``DELETE /eventhubs/<hub>``                     delete hub
    * ``GET    /eventhubs``                           list hubs
    * ``GET    /eventhubs/<hub>``                     hub properties
    * ``GET    /eventhubs/<hub>/partitions``          list partitions
    * ``GET    /eventhubs/<hub>/partitions/<p>``      partition properties
    * ``PUT    /eventhubs/<hub>/consumergroups/<cg>`` create consumer group
    * ``DELETE /eventhubs/<hub>/consumergroups/<cg>`` delete consumer group
    * ``GET    /eventhubs/<hub>/consumergroups``      list consumer groups
    * ``POST   /eventhubs/<hub>/events``              send event(s) (JSON {body,...} or list)
    * ``GET    /eventhubs/<hub>/partitions/<p>/events?cg=&num=&from_seq=``
                                                      receive events
    * ``GET    /eventhubs/<hub>/partitions/<p>/checkpoint?cg=``
                                                      get checkpoint
    * ``PUT    /eventhubs/<hub>/partitions/<p>/checkpoint``
                                                      update checkpoint (JSON {cg,seq,offset})
* Event Grid:
    * ``PUT    /eventgrid/topics/<topic>``            create topic
    * ``DELETE /eventgrid/topics/<topic>``            delete topic
    * ``GET    /eventgrid/topics``                    list topics
    * ``GET    /eventgrid/topics/<topic>``            topic properties
    * ``POST   /eventgrid/topics/<topic>/events``     publish events (JSON list)
    * ``GET    /eventgrid/topics/<topic>/events``     list stored events [?sub=&type=&limit=]
    * ``PUT    /eventgrid/topics/<topic>/subscriptions/<sub>``
                                                      create subscription (JSON props)
    * ``DELETE /eventgrid/topics/<topic>/subscriptions/<sub>``
                                                      delete subscription
    * ``GET    /eventgrid/topics/<topic>/subscriptions``
                                                      list subscriptions
    * ``GET    /eventgrid/topics/<topic>/subscriptions/<sub>``
                                                      get subscription

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
from .cosmos import CosmosService
from .fileshare import FileShareService
from .servicebus import ServiceBusService
from .eventhubs import EventHubsService
from .eventgrid import EventGridService
from .errors import OpenAzureError
from .store import Store


class OpenAzure:
    """Aggregates all services over a single shared store."""

    def __init__(self, data_dir: str | None = None, in_memory: bool = False):
        self.store = Store(data_dir=data_dir, in_memory=in_memory)
        self.blob = BlobService(self.store)
        self.table = TableService(self.store)
        self.queue = QueueService(self.store)
        self.servicebus = ServiceBusService(self.store)
        self.functions = FunctionRunner(self.queue,
                                        service_bus_service=self.servicebus)
        self.cosmos = CosmosService(self.store)
        self.files = FileShareService(self.store)
        self.eventhubs = EventHubsService(self.store)
        self.eventgrid = EventGridService(self.store)

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
            segs = [unquote(s) for s in parsed.path.strip("/").split("/")
                    if s != ""]
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            return segs, qs

        def _has_body(self):
            return int(self.headers.get("Content-Length", 0) or 0) > 0

        # -- verbs ----------------------------------------------------
        def do_GET(self):
            try:
                segs, qs = self._parts()
                if not segs:
                    self._send_json({
                        "service": "openazure",
                        "version": __version__,
                        "services": [
                            "blob", "table", "queue",
                            "functions", "cosmos", "files",
                            "servicebus", "eventhubs", "eventgrid",
                        ],
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
                elif svc == "cosmos":
                    self._get_cosmos(segs, qs)
                elif svc == "files":
                    self._get_files(segs, qs)
                elif svc == "servicebus":
                    self._get_servicebus(segs, qs)
                elif svc == "eventhubs":
                    self._get_eventhubs(segs, qs)
                elif svc == "eventgrid":
                    self._get_eventgrid(segs, qs)
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
                    self._put_blob(segs, qs)
                elif svc == "table":
                    if len(segs) == 2 and not self._has_body():
                        self._send_json(
                            app.table.create_table(segs[1]), 201
                        )
                    elif len(segs) == 2:
                        self._send_json(
                            app.table.upsert_entity(
                                segs[1], self._read_json()
                            )
                        )
                    else:
                        self._send_json(
                            {"error": {"code": "BadRequest"}}, 400
                        )
                elif svc == "queue":
                    if len(segs) == 2:
                        self._send_json(
                            app.queue.create_queue(segs[1]), 201
                        )
                    else:
                        self._send_json(
                            {"error": {"code": "BadRequest"}}, 400
                        )
                elif svc == "cosmos":
                    self._put_cosmos(segs, qs)
                elif svc == "files":
                    self._put_files(segs, qs)
                elif svc == "servicebus":
                    self._put_servicebus(segs, qs)
                elif svc == "eventhubs":
                    self._put_eventhubs(segs, qs)
                elif svc == "eventgrid":
                    self._put_eventgrid(segs, qs)
                else:
                    self._send_json({"error": {"code": "NotFound"}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_POST(self):
            try:
                segs, qs = self._parts()
                svc = segs[0] if segs else ""
                if svc == "table" and len(segs) == 2:
                    if qs.get("comp") == "batch":
                        ops = self._read_json()
                        if isinstance(ops, list):
                            operations = ops
                        else:
                            operations = ops.get("operations", [])
                        self._send_json(
                            {"results": app.table.batch_execute(
                                segs[1], operations
                            )}
                        )
                    else:
                        self._send_json(
                            app.table.insert_entity(
                                segs[1], self._read_json()
                            ), 201
                        )
                elif (svc == "queue" and len(segs) == 3
                      and segs[2] == "messages"):
                    body = self._read_json()
                    res = app.queue.enqueue(
                        segs[1], body.get("body", ""),
                        float(body.get("visibility_delay", 0)),
                    )
                    self._send_json(res, 201)
                elif svc == "functions" and len(segs) == 2:
                    req = {
                        "method": "POST",
                        "headers": dict(self.headers),
                        "params": qs,
                        "body": self._read_body().decode("utf-8", "replace"),
                    }
                    res = app.functions.invoke_http(segs[1], req)
                    body_val = res.get("body", "")
                    if isinstance(body_val, (dict, list)):
                        self._send_json(body_val, res.get("status", 200))
                    else:
                        self._send_bytes(
                            str(body_val).encode("utf-8"),
                            "text/plain", res.get("status", 200),
                        )
                elif svc == "cosmos":
                    self._post_cosmos(segs, qs)
                elif svc == "servicebus":
                    self._post_servicebus(segs, qs)
                elif svc == "eventhubs":
                    self._post_eventhubs(segs, qs)
                elif svc == "eventgrid":
                    self._post_eventgrid(segs, qs)
                else:
                    self._send_json({"error": {"code": "NotFound"}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        def do_PATCH(self):
            try:
                segs, qs = self._parts()
                svc = segs[0] if segs else ""
                if (svc == "queue" and len(segs) == 4
                        and segs[2] == "messages"):
                    # PATCH /queue/<queue>/messages/<id>?pop=<receipt>&vt=<seconds>
                    body = self._read_json() if self._has_body() else {}
                    vt = qs.get("vt")
                    new_body = body.get("body")
                    res = app.queue.update_message(
                        segs[1], segs[3], qs.get("pop", ""),
                        visibility_timeout=float(vt) if vt is not None else None,
                        body=new_body,
                    )
                    self._send_json(res)
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
                        app.table.delete_entity(
                            segs[1], qs["pk"], qs["rk"]
                        )
                    elif len(segs) == 2:
                        app.table.delete_table(segs[1])
                    self._send_json({"deleted": True})
                elif svc == "queue":
                    if len(segs) == 4 and segs[2] == "messages":
                        app.queue.delete_message(
                            segs[1], segs[3], qs.get("pop", "")
                        )
                    elif len(segs) == 2:
                        app.queue.delete_queue(segs[1])
                    self._send_json({"deleted": True})
                elif svc == "cosmos":
                    self._delete_cosmos(segs, qs)
                elif svc == "files":
                    self._delete_files(segs, qs)
                elif svc == "servicebus":
                    self._delete_servicebus(segs, qs)
                elif svc == "eventhubs":
                    self._delete_eventhubs(segs, qs)
                elif svc == "eventgrid":
                    self._delete_eventgrid(segs, qs)
                else:
                    self._send_json({"error": {"code": "NotFound"}}, 404)
            except Exception as e:  # noqa: BLE001
                self._error(e)

        # -- Blob sub-dispatch ----------------------------------------
        def _put_blob(self, segs, qs):
            comp = qs.get("comp", "")
            if len(segs) == 2:
                if comp == "lease":
                    action = qs.get("action", "")
                    if action == "acquire":
                        lease_id = app.blob.acquire_lease(segs[1])
                        self._send_json({"lease_id": lease_id}, 201)
                    elif action == "release":
                        app.blob.release_lease(segs[1], qs.get("lease", ""))
                        self._send_json({"released": True})
                    else:
                        self._send_json(
                            {"error": {"code": "BadRequest",
                                       "message": "unknown lease action"}},
                            400,
                        )
                else:
                    self._send_json(
                        app.blob.create_container(segs[1]), 201
                    )
            elif len(segs) >= 3:
                blob_name = "/".join(segs[2:])
                if comp == "blocklist":
                    body = self._read_json()
                    blocks = body.get("blocks", [])
                    ct = body.get("content_type",
                                  "application/octet-stream")
                    meta = body.get("metadata")
                    tier = body.get("tier", "Hot")
                    res = app.blob.commit_block_list(
                        segs[1], blob_name, blocks,
                        content_type=ct, metadata=meta, tier=tier,
                    )
                    self._send_json(res, 201)
                elif comp == "tier":
                    tier = qs.get("tier", "Hot")
                    app.blob.set_blob_tier(segs[1], blob_name, tier)
                    self._send_json({"tier": tier})
                elif comp == "metadata":
                    meta = self._read_json()
                    app.blob.set_blob_metadata(segs[1], blob_name, meta)
                    self._send_json({"updated": True})
                elif comp == "copy":
                    src_c = qs.get("src_container", "")
                    src_b = qs.get("src_blob", "")
                    res = app.blob.copy_blob(src_c, src_b,
                                              segs[1], blob_name)
                    self._send_json(res, 201)
                else:
                    ct = self.headers.get(
                        "Content-Type", "application/octet-stream"
                    )
                    res = app.blob.put_blob(
                        segs[1], blob_name, self._read_body(), ct
                    )
                    self._send_json(res, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_blob(self, segs, qs):
            comp = qs.get("comp", "")
            if len(segs) == 1:
                self._send_json(
                    {"containers": app.blob.list_containers()}
                )
            elif len(segs) == 2:
                if comp == "list":
                    self._send_json(
                        {"blobs": app.blob.list_blobs(
                            segs[1], qs.get("prefix")
                        )}
                    )
                elif comp == "lease":
                    # GET lease status - just return current state
                    rows = app.blob.store.query(
                        "SELECT lease_id FROM blob_containers WHERE name=?",
                        (segs[1],),
                    )
                    if not rows:
                        from .errors import NotFound as NF
                        raise NF(f"container '{segs[1]}' not found")
                    self._send_json(
                        {"has_lease": bool(rows[0]["lease_id"])}
                    )
                else:
                    self._send_json({"error": {"code": "BadRequest"}}, 400)
            elif len(segs) >= 3:
                blob_name = "/".join(segs[2:])
                if comp == "blocklist":
                    blocks = app.blob.list_blocks(segs[1], blob_name)
                    self._send_json({"blocks": blocks})
                elif comp == "sas":
                    token = app.blob.generate_sas(
                        segs[1], blob_name,
                        permissions=qs.get("sp", "r"),
                        expiry_seconds=int(qs.get("expiry", 3600)),
                    )
                    self._send_json({"sas_token": token})
                else:
                    blob = app.blob.get_blob(segs[1], blob_name)
                    self._send_bytes(
                        blob["content"], blob["content_type"],
                        extra={
                            "ETag": blob["etag"],
                            "Content-MD5": blob["content_md5"],
                        },
                    )
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _post_blob(self, segs, qs):
            comp = qs.get("comp", "")
            if len(segs) >= 3 and comp == "block":
                blob_name = "/".join(segs[2:])
                block_id = qs.get("blockid", "")
                if not block_id:
                    self._send_json(
                        {"error": {"code": "BadRequest",
                                   "message": "blockid required"}}, 400
                    )
                    return
                app.blob.stage_block(
                    segs[1], blob_name, block_id, self._read_body()
                )
                self._send_json({"staged": True}, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- Table sub-dispatch ---------------------------------------
        def _get_table(self, segs, qs):
            if len(segs) == 1:
                self._send_json({"tables": app.table.list_tables()})
            elif len(segs) == 2 and "pk" in qs and "rk" in qs:
                self._send_json(
                    app.table.get_entity(segs[1], qs["pk"], qs["rk"])
                )
            elif len(segs) == 2:
                pk = qs.get("pk")
                odata_filter = qs.get("$filter")
                top = int(qs["$top"]) if "$top" in qs else None
                select = qs.get("$select")
                self._send_json(
                    {"entities": app.table.query_entities(
                        segs[1], pk,
                        odata_filter=odata_filter,
                        top=top,
                        select=select,
                    )}
                )
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- Queue sub-dispatch ---------------------------------------
        def _get_queue(self, segs, qs):
            if len(segs) == 1:
                self._send_json({"queues": app.queue.list_queues()})
            elif len(segs) == 3 and segs[2] == "messages":
                num = int(qs.get("num", 1))
                vt = float(qs.get("vt", 30))
                if qs.get("peek") == "true":
                    self._send_json(
                        {"messages": app.queue.peek(segs[1], num)}
                    )
                else:
                    self._send_json(
                        {"messages": app.queue.dequeue(segs[1], num, vt)}
                    )
            elif len(segs) == 2:
                self._send_json({"count": app.queue.count(segs[1])})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- Cosmos sub-dispatch -------------------------------------
        def _put_cosmos(self, segs, qs):
            # PUT /cosmos/<db>
            # PUT /cosmos/<db>/<container>
            if len(segs) == 2:
                self._send_json(
                    app.cosmos.create_database(segs[1]), 201
                )
            elif len(segs) == 3:
                body = self._read_json()
                pk_path = body.get("partitionKey", "/id")
                self._send_json(
                    app.cosmos.create_container(segs[1], segs[2], pk_path),
                    201,
                )
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_cosmos(self, segs, qs):
            # GET /cosmos
            # GET /cosmos/<db>[?comp=containers]
            # GET /cosmos/<db>/<container>
            # GET /cosmos/<db>/<container>/items[?pk=]
            # GET /cosmos/<db>/<container>/items/<id>[?pk=]
            if len(segs) == 1:
                self._send_json({"databases": app.cosmos.list_databases()})
            elif len(segs) == 2:
                if qs.get("comp") == "containers":
                    self._send_json(
                        {"containers": app.cosmos.list_containers(segs[1])}
                    )
                else:
                    self._send_json(app.cosmos.get_database(segs[1]))
            elif len(segs) == 3:
                self._send_json(
                    app.cosmos.get_container(segs[1], segs[2])
                )
            elif len(segs) == 4 and segs[3] == "items":
                pk = qs.get("pk")
                self._send_json(
                    {"items": app.cosmos.list_items(
                        segs[1], segs[2], pk
                    )}
                )
            elif len(segs) == 5 and segs[3] == "items":
                pk = qs.get("pk")
                self._send_json(
                    app.cosmos.get_item(
                        segs[1], segs[2], segs[4], pk
                    )
                )
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _post_cosmos(self, segs, qs):
            # POST /cosmos/<db>/<container>/items    -> create item
            # POST /cosmos/<db>/<container>/query    -> query items
            if len(segs) == 4 and segs[3] == "items":
                self._send_json(
                    app.cosmos.create_item(
                        segs[1], segs[2], self._read_json()
                    ), 201
                )
            elif len(segs) == 4 and segs[3] == "query":
                body = self._read_json()
                sql = body.get("query", "SELECT * FROM c")
                pk = body.get("pk")
                self._send_json(
                    {"items": app.cosmos.query_items(
                        segs[1], segs[2], sql, pk
                    )}
                )
            else:
                self._send_json({"error": {"code": "NotFound"}}, 404)

        def _delete_cosmos(self, segs, qs):
            # DELETE /cosmos/<db>
            # DELETE /cosmos/<db>/<container>
            # DELETE /cosmos/<db>/<container>/items/<id>[?pk=]
            if len(segs) == 2:
                app.cosmos.delete_database(segs[1])
                self._send_json({"deleted": True})
            elif len(segs) == 3:
                app.cosmos.delete_container(segs[1], segs[2])
                self._send_json({"deleted": True})
            elif len(segs) == 5 and segs[3] == "items":
                pk = qs.get("pk")
                app.cosmos.delete_item(
                    segs[1], segs[2], segs[4], pk
                )
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- File Shares sub-dispatch --------------------------------
        def _put_files(self, segs, qs):
            # PUT /files/<share>                         create share
            # PUT /files/<share>/<path>?comp=dir         create dir
            # PUT /files/<share>/<path>?comp=metadata    set metadata
            # PUT /files/<share>/<path>?comp=copy        copy file
            # PUT /files/<share>/<path>                  upload file
            comp = qs.get("comp", "")
            if len(segs) == 2:
                body = self._read_json() if self._has_body() else {}
                quota = int(body.get("quota_gb", 5120))
                meta = body.get("metadata")
                self._send_json(
                    app.files.create_share(segs[1], quota, meta), 201
                )
            elif len(segs) >= 3:
                path = "/".join(segs[2:])
                if comp == "dir":
                    body = self._read_json() if self._has_body() else {}
                    meta = body.get("metadata")
                    self._send_json(
                        app.files.create_directory(segs[1], path, meta),
                        201,
                    )
                elif comp == "metadata":
                    meta = self._read_json()
                    app.files.set_file_metadata(segs[1], path, meta)
                    self._send_json({"updated": True})
                elif comp == "copy":
                    src_share = qs.get("src_share", segs[1])
                    src_path = qs.get("src_path", "")
                    res = app.files.copy_file(
                        src_share, src_path, segs[1], path
                    )
                    self._send_json(res, 201)
                else:
                    ct = self.headers.get(
                        "Content-Type", "application/octet-stream"
                    )
                    body_bytes = self._read_body()
                    meta_hdr = self.headers.get("x-ms-meta-json")
                    meta = None
                    if meta_hdr:
                        try:
                            meta = json.loads(meta_hdr)
                        except Exception:
                            pass
                    res = app.files.upload_file(
                        segs[1], path, body_bytes, ct, meta
                    )
                    self._send_json(res, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_files(self, segs, qs):
            # GET /files                                  list shares
            # GET /files/<share>                          share properties
            #   or ?comp=dir                              list root directory
            # GET /files/<share>/<path>?comp=dir          list dir
            # GET /files/<share>/<path>                   download file
            comp = qs.get("comp", "")
            if len(segs) == 1:
                self._send_json({"shares": app.files.list_shares()})
            elif len(segs) == 2:
                if comp == "dir":
                    # list root directory
                    self._send_json(
                        app.files.list_directory(segs[1], "")
                    )
                else:
                    self._send_json(
                        app.files.get_share_properties(segs[1])
                    )
            elif len(segs) >= 3:
                path = "/".join(segs[2:])
                if comp == "dir":
                    self._send_json(
                        app.files.list_directory(segs[1], path)
                    )
                else:
                    f = app.files.get_file(segs[1], path)
                    self._send_bytes(
                        f["content"], f["content_type"],
                        extra={
                            "ETag": f["etag"],
                            "Content-MD5": f["content_md5"],
                        },
                    )
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _delete_files(self, segs, qs):
            # DELETE /files/<share>                       delete share
            # DELETE /files/<share>/<path>?comp=dir       delete dir
            # DELETE /files/<share>/<path>                delete file
            comp = qs.get("comp", "")
            if len(segs) == 2:
                app.files.delete_share(segs[1])
                self._send_json({"deleted": True})
            elif len(segs) >= 3:
                path = "/".join(segs[2:])
                if comp == "dir":
                    app.files.delete_directory(segs[1], path)
                else:
                    app.files.delete_file(segs[1], path)
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- Service Bus sub-dispatch --------------------------------
        def _put_servicebus(self, segs, qs):
            # PUT /servicebus/queues/<q>
            # PUT /servicebus/topics/<t>
            # PUT /servicebus/topics/<t>/subscriptions/<s>
            # PUT /servicebus/topics/<t>/subscriptions/<s>/rules/<r>
            if len(segs) < 3:
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            kind = segs[1]
            if kind == "queues" and len(segs) == 3:
                body = self._read_json() if self._has_body() else {}
                res = app.servicebus.create_queue(
                    segs[2],
                    max_size_mb=int(body.get("max_size_mb", 1024)),
                    lock_duration=int(body.get("lock_duration", 60)),
                    max_delivery_count=int(body.get("max_delivery_count", 10)),
                    requires_session=bool(body.get("requires_session", False)),
                    dead_letter_on_expiry=bool(body.get("dead_letter_on_expiry", False)),
                )
                self._send_json(res, 201)
            elif kind == "topics" and len(segs) == 3:
                body = self._read_json() if self._has_body() else {}
                res = app.servicebus.create_topic(
                    segs[2],
                    max_size_mb=int(body.get("max_size_mb", 1024)),
                )
                self._send_json(res, 201)
            elif kind == "topics" and len(segs) == 5 and segs[3] == "subscriptions":
                res = app.servicebus.create_subscription(segs[2], segs[4])
                self._send_json(res, 201)
            elif kind == "topics" and len(segs) == 7 and segs[3] == "subscriptions" and segs[5] == "rules":
                body = self._read_json()
                res = app.servicebus.add_rule(
                    segs[2], segs[4], segs[6],
                    filter_sql=body.get("filter_sql", "1=1"),
                )
                self._send_json(res, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_servicebus(self, segs, qs):
            if len(segs) < 2:
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            kind = segs[1]
            if kind == "queues" and len(segs) == 2:
                self._send_json({"queues": app.servicebus.list_queues()})
            elif kind == "queues" and len(segs) == 3:
                self._send_json(app.servicebus.get_queue_properties(segs[2]))
            elif kind == "queues" and len(segs) == 4 and segs[3] == "messages":
                num = int(qs.get("num", 1))
                lock = int(qs.get("lock", 60))
                msgs = app.servicebus.receive_queue(segs[2], num, lock)
                self._send_json({"messages": msgs})
            elif kind == "queues" and len(segs) == 4 and segs[3] == "deadletter":
                num = int(qs.get("num", 1))
                msgs = app.servicebus.receive_dead_letter(segs[2], num)
                self._send_json({"messages": msgs})
            elif kind == "topics" and len(segs) == 2:
                self._send_json({"topics": app.servicebus.list_topics()})
            elif kind == "topics" and len(segs) == 4 and segs[3] == "subscriptions":
                self._send_json(
                    {"subscriptions": app.servicebus.list_subscriptions(segs[2])}
                )
            elif kind == "topics" and len(segs) == 5 and segs[3] == "subscriptions":
                # GET /servicebus/topics/<t>/subscriptions/<s>/messages
                # handled below
                self._send_json({"error": {"code": "BadRequest",
                                           "message": "append /messages"}}, 400)
            elif kind == "topics" and len(segs) == 6 and segs[3] == "subscriptions" and segs[5] == "messages":
                num = int(qs.get("num", 1))
                lock = int(qs.get("lock", 60))
                msgs = app.servicebus.receive_subscription(segs[2], segs[4], num, lock)
                self._send_json({"messages": msgs})
            elif kind == "topics" and len(segs) == 6 and segs[3] == "subscriptions" and segs[5] == "rules":
                rules = app.servicebus.list_rules(segs[2], segs[4])
                self._send_json({"rules": rules})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _post_servicebus(self, segs, qs):
            if len(segs) < 3:
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            kind = segs[1]
            comp = qs.get("comp", "")
            if kind == "queues" and len(segs) == 4 and segs[3] == "messages":
                body = self._read_json()
                if comp == "abandon":
                    app.servicebus.abandon_message(segs[2], qs.get("lock", ""))
                    self._send_json({"abandoned": True})
                elif comp == "deadletter":
                    app.servicebus.dead_letter_message(
                        segs[2], qs.get("lock", ""),
                        reason=body.get("reason", "UserDeadLettered"),
                    )
                    self._send_json({"dead_lettered": True})
                else:
                    res = app.servicebus.send_queue(
                        segs[2],
                        body.get("body", ""),
                        session_id=body.get("session_id"),
                        label=body.get("label"),
                        properties=body.get("properties"),
                    )
                    self._send_json(res, 201)
            elif kind == "topics" and len(segs) == 4 and segs[3] == "messages":
                body = self._read_json()
                res = app.servicebus.publish_topic(
                    segs[2],
                    body.get("body", ""),
                    label=body.get("label"),
                    properties=body.get("properties"),
                )
                self._send_json(res, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _delete_servicebus(self, segs, qs):
            if len(segs) < 3:
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            kind = segs[1]
            if kind == "queues" and len(segs) == 3:
                app.servicebus.delete_queue(segs[2])
                self._send_json({"deleted": True})
            elif kind == "queues" and len(segs) == 4 and segs[3] == "messages":
                app.servicebus.complete_message(segs[2], qs.get("lock", ""))
                self._send_json({"completed": True})
            elif kind == "topics" and len(segs) == 3:
                app.servicebus.delete_topic(segs[2])
                self._send_json({"deleted": True})
            elif kind == "topics" and len(segs) == 5 and segs[3] == "subscriptions":
                app.servicebus.delete_subscription(segs[2], segs[4])
                self._send_json({"deleted": True})
            elif kind == "topics" and len(segs) == 7 and segs[3] == "subscriptions" and segs[5] == "rules":
                app.servicebus.remove_rule(segs[2], segs[4], segs[6])
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- Event Hubs sub-dispatch ----------------------------------
        def _put_eventhubs(self, segs, qs):
            # PUT /eventhubs/<hub>
            # PUT /eventhubs/<hub>/consumergroups/<cg>
            # PUT /eventhubs/<hub>/partitions/<p>/checkpoint
            if len(segs) == 2:
                body = self._read_json() if self._has_body() else {}
                res = app.eventhubs.create_hub(
                    segs[1],
                    partition_count=int(body.get("partition_count", 4)),
                    message_retention=int(body.get("message_retention", 1)),
                )
                self._send_json(res, 201)
            elif len(segs) == 4 and segs[2] == "consumergroups":
                res = app.eventhubs.create_consumer_group(segs[1], segs[3])
                self._send_json(res, 201)
            elif len(segs) == 5 and segs[2] == "partitions" and segs[4] == "checkpoint":
                body = self._read_json()
                res = app.eventhubs.update_checkpoint(
                    segs[1],
                    body.get("consumer_group", "$Default"),
                    int(segs[3]),
                    int(body.get("sequence_number", 0)),
                    int(body.get("offset", 0)),
                )
                self._send_json(res)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_eventhubs(self, segs, qs):
            if len(segs) == 1:
                self._send_json({"hubs": app.eventhubs.list_hubs()})
            elif len(segs) == 2:
                self._send_json(app.eventhubs.get_hub_properties(segs[1]))
            elif len(segs) == 3 and segs[2] == "partitions":
                parts = app.eventhubs.list_partitions(segs[1])
                self._send_json({"partitions": parts})
            elif len(segs) == 4 and segs[2] == "partitions":
                self._send_json(
                    app.eventhubs.get_partition_properties(segs[1], int(segs[3]))
                )
            elif len(segs) == 3 and segs[2] == "consumergroups":
                cgs = app.eventhubs.list_consumer_groups(segs[1])
                self._send_json({"consumer_groups": cgs})
            elif len(segs) == 5 and segs[2] == "partitions" and segs[4] == "events":
                cg = qs.get("cg", "$Default")
                num = int(qs.get("num", 100))
                from_seq = int(qs["from_seq"]) if "from_seq" in qs else None
                events = app.eventhubs.receive_events(
                    segs[1], int(segs[3]), cg, num, from_seq
                )
                self._send_json({"events": events})
            elif len(segs) == 5 and segs[2] == "partitions" and segs[4] == "checkpoint":
                cg = qs.get("cg", "$Default")
                cp = app.eventhubs.get_checkpoint(segs[1], cg, int(segs[3]))
                self._send_json(cp)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _post_eventhubs(self, segs, qs):
            # POST /eventhubs/<hub>/events   send event or batch
            if len(segs) == 3 and segs[2] == "events":
                body = self._read_json()
                pk = qs.get("partition_key")
                part = int(qs["partition"]) if "partition" in qs else None
                if isinstance(body, list):
                    res = app.eventhubs.send_batch(
                        segs[1], body, partition_key=pk, partition=part
                    )
                    self._send_json({"results": res}, 201)
                else:
                    res = app.eventhubs.send_event(
                        segs[1], body.get("body", ""),
                        partition_key=body.get("partition_key", pk),
                        partition=part,
                        properties=body.get("properties"),
                    )
                    self._send_json(res, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _delete_eventhubs(self, segs, qs):
            if len(segs) == 2:
                app.eventhubs.delete_hub(segs[1])
                self._send_json({"deleted": True})
            elif len(segs) == 4 and segs[2] == "consumergroups":
                app.eventhubs.delete_consumer_group(segs[1], segs[3])
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # -- Event Grid sub-dispatch ----------------------------------
        def _put_eventgrid(self, segs, qs):
            # PUT /eventgrid/topics/<t>
            # PUT /eventgrid/topics/<t>/subscriptions/<s>
            if len(segs) < 3:
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            if segs[1] != "topics":
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            if len(segs) == 3:
                body = self._read_json() if self._has_body() else {}
                res = app.eventgrid.create_topic(
                    segs[2],
                    schema=body.get("schema", "EventGridSchema"),
                )
                self._send_json(res, 201)
            elif len(segs) == 5 and segs[3] == "subscriptions":
                body = self._read_json() if self._has_body() else {}
                res = app.eventgrid.create_subscription(
                    segs[2], segs[4],
                    endpoint_url=body.get("endpoint_url"),
                    event_types=body.get("event_types"),
                    subject_begins_with=body.get("subject_begins_with"),
                    subject_ends_with=body.get("subject_ends_with"),
                    property_filters=body.get("property_filters"),
                )
                self._send_json(res, 201)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _get_eventgrid(self, segs, qs):
            if len(segs) < 2 or segs[1] != "topics":
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            if len(segs) == 2:
                self._send_json({"topics": app.eventgrid.list_topics()})
            elif len(segs) == 3:
                self._send_json(app.eventgrid.get_topic(segs[2]))
            elif len(segs) == 4 and segs[3] == "events":
                sub = qs.get("sub")
                ev_type = qs.get("type")
                limit = int(qs.get("limit", 100))
                events = app.eventgrid.list_events(
                    segs[2], subscription=sub,
                    event_type=ev_type, limit=limit,
                )
                self._send_json({"events": events})
            elif len(segs) == 4 and segs[3] == "subscriptions":
                subs = app.eventgrid.list_subscriptions(segs[2])
                self._send_json({"subscriptions": subs})
            elif len(segs) == 5 and segs[3] == "subscriptions":
                self._send_json(app.eventgrid.get_subscription(segs[2], segs[4]))
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _post_eventgrid(self, segs, qs):
            # POST /eventgrid/topics/<t>/events
            if (len(segs) == 4 and segs[1] == "topics" and segs[3] == "events"):
                events = self._read_json()
                if not isinstance(events, list):
                    events = [events]
                res = app.eventgrid.publish(segs[2], events)
                self._send_json(res, 200)
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        def _delete_eventgrid(self, segs, qs):
            if len(segs) < 3 or segs[1] != "topics":
                self._send_json({"error": {"code": "BadRequest"}}, 400)
                return
            if len(segs) == 3:
                app.eventgrid.delete_topic(segs[2])
                self._send_json({"deleted": True})
            elif len(segs) == 5 and segs[3] == "subscriptions":
                app.eventgrid.delete_subscription(segs[2], segs[4])
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": {"code": "BadRequest"}}, 400)

        # Override do_POST to handle block staging
        _orig_post = do_POST

        def do_POST(self):
            try:
                segs, qs = self._parts()
                svc = segs[0] if segs else ""
                comp = qs.get("comp", "")
                if svc == "blob" and len(segs) >= 3 and comp == "block":
                    self._post_blob(segs, qs)
                    return
            except Exception as e:  # noqa: BLE001
                self._error(e)
                return
            self._orig_post(self)

    # Patch: replace do_POST to support blob block staging too
    # The inner override above won't work as-is because it references
    # _orig_post incorrectly. Let's just rewrite do_POST properly.
    del Handler.do_POST
    del Handler._orig_post

    def do_POST(self_h):
        try:
            segs, qs = self_h._parts()
            svc = segs[0] if segs else ""
            comp = qs.get("comp", "")

            # Block staging
            if svc == "blob" and len(segs) >= 3 and comp == "block":
                self_h._post_blob(segs, qs)
                return

            if svc == "table" and len(segs) == 2:
                if qs.get("comp") == "batch":
                    ops = self_h._read_json()
                    if isinstance(ops, list):
                        operations = ops
                    else:
                        operations = ops.get("operations", [])
                    self_h._send_json(
                        {"results": app.table.batch_execute(
                            segs[1], operations
                        )}
                    )
                else:
                    self_h._send_json(
                        app.table.insert_entity(
                            segs[1], self_h._read_json()
                        ), 201
                    )
            elif (svc == "queue" and len(segs) == 3
                  and segs[2] == "messages"):
                body = self_h._read_json()
                res = app.queue.enqueue(
                    segs[1], body.get("body", ""),
                    float(body.get("visibility_delay", 0)),
                )
                self_h._send_json(res, 201)
            elif svc == "functions" and len(segs) == 2:
                req = {
                    "method": "POST",
                    "headers": dict(self_h.headers),
                    "params": qs,
                    "body": self_h._read_body().decode("utf-8", "replace"),
                }
                res = app.functions.invoke_http(segs[1], req)
                body_val = res.get("body", "")
                if isinstance(body_val, (dict, list)):
                    self_h._send_json(body_val, res.get("status", 200))
                else:
                    self_h._send_bytes(
                        str(body_val).encode("utf-8"),
                        "text/plain", res.get("status", 200),
                    )
            elif svc == "cosmos":
                self_h._post_cosmos(segs, qs)
            elif svc == "servicebus":
                self_h._post_servicebus(segs, qs)
            elif svc == "eventhubs":
                self_h._post_eventhubs(segs, qs)
            elif svc == "eventgrid":
                self_h._post_eventgrid(segs, qs)
            else:
                self_h._send_json({"error": {"code": "NotFound"}}, 404)
        except Exception as e:  # noqa: BLE001
            self_h._error(e)

    Handler.do_POST = do_POST

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
