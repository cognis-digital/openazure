# openazure

## What is this?

**openazure** is a small, self-contained program you run **on your own machine**
that imitates the core building blocks of Microsoft Azure's storage and
serverless stack. Instead of creating a real cloud account, paying for usage,
and needing an internet connection, you start `openazure` locally and talk to
it exactly like you would talk to the real services.

It gives a developer four things:

- **Blob Storage** — store and fetch files ("blobs") grouped into containers.
- **Table Storage** — store JSON records keyed by a `PartitionKey` + `RowKey`.
- **Queue Storage** — push messages onto a queue and pull them off later, with
  a visibility timeout so a message you are working on isn't handed to anyone
  else until you finish (or time out).
- **Functions runner** — register small Python handlers that run on an HTTP
  request or when a queue message arrives, the way Azure Functions do.

**Who is it for?** Developers who want to build and test code that uses Azure
storage/functions **without touching the cloud** — for fast unit tests,
offline work, CI pipelines, demos, and learning. It is in the same spirit as
LocalStack (AWS), MinIO (S3), and the Firebase Emulator Suite.

Everything runs in a single local HTTP server, persists to a local SQLite file
(or to pure memory for tests), and is written entirely against the Python
standard library — **no third-party runtime dependencies**.

> ### Disclaimer
> openazure is an **independent, open reimplementation** intended for **local
> development and testing only**. It is **NOT affiliated with, endorsed by, or
> sponsored by Microsoft Corporation**. "Azure" and related names are used only
> **nominatively**, to describe which API behaviors openazure aims to be
> compatible with. openazure implements a **compatible subset** of those
> services and is **not intended for production use**.

## Architecture

```
openazure/
├── openazure/
│   ├── __init__.py        # package exports + version
│   ├── store.py           # shared sqlite3 backend (disk or :memory:)
│   ├── errors.py          # typed errors -> HTTP status + Azure-style codes
│   ├── blob.py            # BlobService    (containers / blobs)
│   ├── table.py           # TableService   (entities by PartitionKey+RowKey)
│   ├── queue.py           # QueueService   (visibility-timeout messages)
│   ├── functions.py       # FunctionRunner (http + queue triggers)
│   ├── server.py          # one ThreadingHTTPServer exposing all services
│   ├── cli.py             # `openazure` console entry point
│   └── __main__.py        # `python -m openazure`
└── tests/                 # end-to-end pytest suite
```

All services share a single `Store` (one SQLite connection), so a single
in-memory instance is consistent across services within one process. The HTTP
server (`server.py`) maps clean path prefixes onto the service classes; the
service classes can also be imported and called directly with no server.

## Services

| Service   | Module          | Class            | Primitives | Local path prefix |
|-----------|-----------------|------------------|------------|-------------------|
| Blob      | `blob.py`       | `BlobService`    | containers, blobs (bytes, ETag, Content-MD5) | `/blob` |
| Table     | `table.py`      | `TableService`   | tables, entities keyed by PartitionKey+RowKey; insert/upsert/merge/replace/query | `/table` |
| Queue     | `queue.py`      | `QueueService`   | queues, messages with visibility timeout + pop receipts | `/queue` |
| Functions | `functions.py`  | `FunctionRunner` | HTTP-trigger + queue-trigger Python handlers | `/functions` |

## Quickstart

Start the local server (in-memory, nothing persisted):

```bash
openazure serve --in-memory
# openazure listening on http://127.0.0.1:10000 (data_dir=memory)
```

Or persist to a local directory:

```bash
openazure serve --data-dir ./openazure-data --port 10000
```

Talk to it with `curl`:

```bash
# Blob: create a container, upload a file, download it
curl -X PUT  http://127.0.0.1:10000/blob/photos
curl -X PUT  --data-binary @cat.jpg http://127.0.0.1:10000/blob/photos/cat.jpg
curl         http://127.0.0.1:10000/blob/photos/cat.jpg --output out.jpg

# Table: insert and read an entity
curl -X PUT  http://127.0.0.1:10000/table/People
curl -X POST http://127.0.0.1:10000/table/People \
     -d '{"PartitionKey":"us","RowKey":"alice","age":30}'
curl 'http://127.0.0.1:10000/table/People?pk=us&rk=alice'

# Queue: create, enqueue, dequeue
curl -X PUT  http://127.0.0.1:10000/queue/jobs
curl -X POST http://127.0.0.1:10000/queue/jobs/messages -d '{"body":"hello"}'
curl 'http://127.0.0.1:10000/queue/jobs/messages?num=1&vt=30'
```

Or use the classes directly in Python (no server needed):

```python
from openazure.store import Store
from openazure.blob import BlobService

store = Store(in_memory=True)
blob = BlobService(store)
blob.create_container("docs")
blob.put_blob("docs", "hello.txt", b"hi there", "text/plain")
print(blob.get_blob("docs", "hello.txt")["content"])  # b'hi there'
```

Register an Azure-Functions-style handler:

```python
from openazure.server import OpenAzure

app = OpenAzure(in_memory=True)

@app.functions.http_function("greet")
def greet(req):
    name = req["params"].get("name", "world")
    return {"status": 200, "body": f"hello {name}"}

print(app.functions.invoke_http("greet", {"params": {"name": "azure"}}))
```

<!-- cognis:domains:start -->
## Domains

**Primary domain:** Cloud & DevTools  ·  **JTF MERIDIAN division:** ATHENA-PRIME · COGNI-2

**Topics:** `cognis` `devtools` `cloud` `developer-tools` `cloud-emulator`

Part of the **Cognis Neural Suite** — 300+ source-available tools organized across 12 domains under the JTF MERIDIAN command structure. See the [suite on GitHub](https://github.com/cognis-digital) and [jtf-meridian](https://github.com/cognis-digital/jtf-meridian) for how the pieces fit together.
<!-- cognis:domains:end -->

## Install

openazure is **source-available** (COCL 1.0) and is **not published to PyPI**.
Install it directly from the Git repository.

**One-line installers** (clone-free, from this repo):

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/cognis-digital/openazure/main/install.sh | bash
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/cognis-digital/openazure/main/install.ps1 | iex
```

**pipx** (isolated, recommended for a CLI):

```bash
pipx install "git+https://github.com/cognis-digital/openazure.git"
```

**uv**:

```bash
uv tool install "git+https://github.com/cognis-digital/openazure.git"
# or, into a project:
uv pip install "git+https://github.com/cognis-digital/openazure.git"
```

**pip (git+https)**:

```bash
python -m pip install "git+https://github.com/cognis-digital/openazure.git"
```

**From source** (for development / running the tests):

```bash
git clone https://github.com/cognis-digital/openazure.git
cd openazure
python -m pip install -e ".[dev]"
python -m pytest -q
```

After install you get an `openazure` console command (and `python -m openazure`).

**Requirements:** Python 3.10+ and the standard library only. No third-party
runtime dependencies. Works on Linux, macOS, and Windows.

## Topics / Domains

`azure-emulator` · `local-development` · `cloud-emulation` · `blob-storage` ·
`table-storage` · `queue-storage` · `serverless-functions` · `testing` ·
`offline-development` · `developer-tools`

## Verification

The test suite is a real end-to-end pytest suite under `tests/` that exercises
every service both directly (calling the service classes) and over the live
HTTP server (started in-process on an OS-assigned port and driven with
`urllib`). On the development machine:

```
$ python -m pytest -q
52 passed
```

**52 tests pass** (`tests/test_blob.py`, `tests/test_table.py`,
`tests/test_queue.py`, `tests/test_functions.py`, `tests/test_server.py`),
covering blob round-trips and Content-MD5, table insert/upsert/merge/replace/
query, queue visibility-timeout redelivery and pop-receipt deletion, function
HTTP and queue triggers (including at-least-once behavior on handler failure),
and the full HTTP server for all four services. CI runs the same suite on
Ubuntu, macOS, and Windows across Python 3.10–3.13.

## Roadmap

The following are **not implemented yet** and are tracked as roadmap items
(they are intentionally **not** claimed as working):

- Azure-native SDK / connection-string wire compatibility (current API is a
  clean local REST surface, not the byte-for-byte Azure REST protocol).
- Shared Access Signatures (SAS) and account-key authentication.
- Blob block/append/page blob types, snapshots, and leases.
- Table OData `$filter` query-language parsing (current filtering is exact-match).
- Timer-trigger and blob-trigger functions; a persistent function scheduler.
- Cosmos DB, Service Bus, and Event Hubs emulation.

## License

Cognis Open Collaboration License (COCL) 1.0 — see [LICENSE](LICENSE).
