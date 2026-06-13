# openazure

## Usage — step by step

`openazure` is a local open-source emulator of Azure primitives (blob / queue),
with thin HTTP client subcommands for quick manual checks.

1. **Install** (editable from a clone, or from the wheel):
   ```bash
   pip install -e .
   # provides the `openazure` console script
   ```
2. **Start the local server** (defaults to `127.0.0.1:10000`; it persists to
   `./openazure-data` unless you pass `--in-memory`):
   ```bash
   openazure serve --data-dir ./openazure-data
   # or ephemeral:
   openazure --port 10000 serve --in-memory
   ```
3. **Exercise the data plane** with the built-in HTTP client subcommands (these
   talk to a running `serve` instance — `--host`/`--port` are top-level flags):
   ```bash
   openazure --port 10000 blob ls my-container
   openazure --port 10000 queue put jobs '{"task":"resize"}'
   ```
4. **Read / use the output.** The client subcommands print the server's JSON
   response and exit non-zero on HTTP errors (status ≥ 400), so they double as
   simple health checks. `openazure version` prints the version.
5. **Use it in CI.** Launch the server in the background, run your Azure-SDK
   tests against the local endpoint, then tear it down:
   ```bash
   openazure serve --in-memory &
   # ... run tests pointed at http://127.0.0.1:10000 ...
   openazure --port 10000 queue put smoke '{"ping":1}'
   ```

## What is this?

**openazure** is a small, self-contained program you run **on your own machine**
that imitates the core building blocks of Microsoft Azure's storage and
serverless stack. Instead of creating a real cloud account, paying for usage,
and needing an internet connection, you start `openazure` locally and talk to
it exactly like you would talk to the real services.

It gives a developer six things:

- **Blob Storage** — store and fetch files ("blobs") grouped into containers;
  block-blob staging and commit, metadata, access tiers, server-side copy,
  SAS token stubs, and container lease stubs.
- **Table Storage** — store JSON records keyed by a `PartitionKey` + `RowKey`;
  insert/upsert/merge/replace/query including OData-lite `$filter`/`$top`/`$select`
  and atomic batch transactions.
- **Queue Storage** — push messages onto a queue and pull them off later, with
  a visibility timeout so a message you are working on isn't handed to anyone
  else until you finish (or time out).
- **Functions runner** — register small Python handlers that run on an HTTP
  request or when a queue message arrives, the way Azure Functions do.
- **Cosmos DB** — databases, containers with a partition key, and items; full
  CRUD plus a SQL-subset query engine (`SELECT`/`WHERE`/`ORDER BY`/
  `OFFSET … LIMIT`).
- **File Shares** — shares, directories (hierarchical), and files; upload,
  download, metadata, server-side copy, and directory listing.

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
│   ├── blob.py            # BlobService    (containers / blobs / blocks / metadata)
│   ├── table.py           # TableService   (entities, OData-lite query, batch)
│   ├── queue.py           # QueueService   (visibility-timeout messages)
│   ├── functions.py       # FunctionRunner (http + queue triggers)
│   ├── cosmos.py          # CosmosService  (databases / containers / items / SQL)
│   ├── fileshare.py       # FileShareService (shares / directories / files)
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

| Service      | Module           | Class              | Primitives | Local path prefix |
|--------------|------------------|--------------------|------------|-------------------|
| Blob         | `blob.py`        | `BlobService`      | containers; blobs (bytes, ETag, Content-MD5); block-blob stage/commit; metadata; tier (Hot/Cool/Archive); server-side copy; SAS token stub; container lease stub | `/blob` |
| Table        | `table.py`       | `TableService`     | tables; entities keyed by PartitionKey+RowKey; insert/upsert/merge/replace/query; OData-lite `$filter`/`$top`/`$select`; atomic batch transactions | `/table` |
| Queue        | `queue.py`       | `QueueService`     | queues, messages with visibility timeout + pop receipts | `/queue` |
| Functions    | `functions.py`   | `FunctionRunner`   | HTTP-trigger + queue-trigger Python handlers | `/functions` |
| Cosmos DB    | `cosmos.py`      | `CosmosService`    | databases, containers (partition key), items (CRUD, upsert); SQL-subset query (SELECT/WHERE/ORDER BY/OFFSET LIMIT) | `/cosmos` |
| File Shares  | `fileshare.py`   | `FileShareService` | shares, directories (hierarchical), files (upload/download/copy/metadata/delete), directory listing | `/files` |

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

# Block blob: stage blocks then commit
curl -X POST --data-binary @part1.bin \
     'http://127.0.0.1:10000/blob/photos/big.bin?comp=block&blockid=b1'
curl -X PUT  -d '{"blocks":["b1"],"content_type":"application/octet-stream"}' \
     'http://127.0.0.1:10000/blob/photos/big.bin?comp=blocklist'

# Table: insert and read an entity; OData filter
curl -X PUT  http://127.0.0.1:10000/table/People
curl -X POST http://127.0.0.1:10000/table/People \
     -d '{"PartitionKey":"us","RowKey":"alice","age":30}'
curl 'http://127.0.0.1:10000/table/People?pk=us&rk=alice'
curl 'http://127.0.0.1:10000/table/People?pk=us&$filter=age%20gt%2025&$top=5'

# Table batch transaction
curl -X POST 'http://127.0.0.1:10000/table/People?comp=batch' \
     -d '[{"op":"insert","entity":{"PartitionKey":"eu","RowKey":"bob","age":25}}]'

# Queue: create, enqueue, dequeue
curl -X PUT  http://127.0.0.1:10000/queue/jobs
curl -X POST http://127.0.0.1:10000/queue/jobs/messages -d '{"body":"hello"}'
curl 'http://127.0.0.1:10000/queue/jobs/messages?num=1&vt=30'

# Cosmos DB: create database, container, item, query
curl -X PUT  http://127.0.0.1:10000/cosmos/mydb
curl -X PUT  http://127.0.0.1:10000/cosmos/mydb/users \
     -d '{"partitionKey":"/country"}'
curl -X POST http://127.0.0.1:10000/cosmos/mydb/users/items \
     -d '{"id":"u1","country":"us","name":"Alice"}'
curl -X POST http://127.0.0.1:10000/cosmos/mydb/users/query \
     -d '{"query":"SELECT * FROM c WHERE c.country = '\''us'\''"}'

# File Shares: create share, directory, upload and download a file
curl -X PUT  http://127.0.0.1:10000/files/myshare -d '{"quota_gb":100}'
curl -X PUT  'http://127.0.0.1:10000/files/myshare/docs?comp=dir' -d '{}'
curl -X PUT  --data-binary @readme.txt \
     http://127.0.0.1:10000/files/myshare/docs/readme.txt
curl         http://127.0.0.1:10000/files/myshare/docs/readme.txt
curl         'http://127.0.0.1:10000/files/myshare/docs?comp=dir'
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

Cosmos DB:

```python
from openazure.store import Store
from openazure.cosmos import CosmosService

store = Store(in_memory=True)
cosmos = CosmosService(store)
cosmos.create_database("mydb")
cosmos.create_container("mydb", "items", "/category")
cosmos.create_item("mydb", "items", {"id": "1", "category": "A", "val": 42})
results = cosmos.query_items("mydb", "items",
                             "SELECT * FROM c WHERE c.category = 'A'")
print(results[0]["val"])  # 42
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
`table-storage` · `queue-storage` · `serverless-functions` · `cosmos-db` ·
`file-shares` · `testing` · `offline-development` · `developer-tools`

## Verification

The test suite is a real end-to-end pytest suite under `tests/` that exercises
every service both directly (calling the service classes) and over the live
HTTP server (started in-process on an OS-assigned port and driven with
`urllib`). On the development machine:

```
$ python -m pytest -q
170 passed
```

**170 tests pass** (`tests/test_blob.py`, `tests/test_blob_extended.py`,
`tests/test_table.py`, `tests/test_table_extended.py`, `tests/test_queue.py`,
`tests/test_functions.py`, `tests/test_server.py`, `tests/test_server_extended.py`,
`tests/test_cosmos.py`, `tests/test_fileshare.py`), covering:

- Blob round-trips, Content-MD5, block-blob staging and commit, metadata,
  access tiers (Hot/Cool/Archive), server-side copy, SAS token stubs,
  container lease acquire/release.
- Table insert/upsert/merge/replace/query, OData-lite `$filter` (eq/ne/gt/lt/
  ge/le/and, string/number/bool values), `$top`, `$select`, atomic batch
  transactions with rollback on failure.
- Queue visibility-timeout redelivery and pop-receipt deletion.
- Function HTTP and queue triggers (including at-least-once behavior on handler
  failure).
- Cosmos DB: databases, containers, items (CRUD + upsert), partition key
  scoping, SQL-subset queries (SELECT/WHERE/ORDER BY/OFFSET LIMIT).
- File Shares: shares, directories (hierarchical, empty-check enforcement),
  files (upload/download/copy/metadata/delete), directory listing.
- Full HTTP server for all six services.

CI runs the same suite on Ubuntu, macOS, and Windows across Python 3.10–3.13.

## Roadmap

The following are **not implemented yet** and are tracked as roadmap items
(they are intentionally **not** claimed as working):

- Azure-native SDK / connection-string wire compatibility (current API is a
  clean local REST surface, not the byte-for-byte Azure REST protocol).
- Shared Access Signatures (SAS) with actual enforcement (current stub
  generates signed URLs but does not validate them on GET/PUT).
- Blob snapshots, page blobs, and append blobs.
- Full OData `$filter` language (nested parens, `or`, `not`, `startswith`,
  `contains`; current subset supports `and`, six comparison operators,
  string/number/bool literals).
- Cosmos DB stored procedures, triggers, change feed, and cross-partition
  aggregation queries.
- File Share SMB/NFS protocol access (current API is HTTP-only).
- Timer-trigger and blob-trigger functions; a persistent function scheduler.
- Service Bus and Event Hubs emulation.

## License

Cognis Open Collaboration License (COCL) 1.0 — see [LICENSE](LICENSE).
