"""Console entry point for openazure.

Subcommands::

    openazure serve [--host H] [--port P] [--data-dir DIR] [--in-memory]
    openazure version
    openazure blob ls <container>            (against a running server)
    openazure queue put <queue> <message>    (against a running server)

The data-plane subcommands are thin HTTP clients against a running
``openazure serve`` instance and exist mainly for quick manual checks; the
primary use of the CLI is ``serve``.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error

from . import __version__
from .server import serve


def _http(method: str, url: str, data: bytes | None = None,
          content_type: str = "application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(body or b"{}")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _base(args) -> str:
    return f"http://{args.host}:{args.port}"


def cmd_serve(args) -> int:
    serve(host=args.host, port=args.port,
          data_dir=None if args.in_memory else args.data_dir,
          in_memory=args.in_memory)
    return 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


def cmd_blob_ls(args) -> int:
    status, body = _http("GET", f"{_base(args)}/blob/{args.container}?comp=list")
    print(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body)
    return 0 if status < 400 else 1


def cmd_queue_put(args) -> int:
    payload = json.dumps({"body": args.message}).encode("utf-8")
    status, body = _http("POST", f"{_base(args)}/queue/{args.queue}/messages", payload)
    print(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body)
    return 0 if status < 400 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openazure",
                                description="Local open-source Azure primitives emulator.")
    p.add_argument("--host", default="127.0.0.1", help="server host (client + serve)")
    p.add_argument("--port", default=10000, type=int, help="server port (client + serve)")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="start the local openazure server")
    s.add_argument("--data-dir", default="./openazure-data",
                   help="directory for the sqlite store (default ./openazure-data)")
    s.add_argument("--in-memory", action="store_true",
                   help="use an in-memory store (nothing persisted)")
    s.set_defaults(func=cmd_serve)

    v = sub.add_parser("version", help="print version")
    v.set_defaults(func=cmd_version)

    b = sub.add_parser("blob", help="blob client commands")
    bsub = b.add_subparsers(dest="blob_command")
    bls = bsub.add_parser("ls", help="list blobs in a container")
    bls.add_argument("container")
    bls.set_defaults(func=cmd_blob_ls)

    q = sub.add_parser("queue", help="queue client commands")
    qsub = q.add_subparsers(dest="queue_command")
    qput = qsub.add_parser("put", help="enqueue a message")
    qput.add_argument("queue")
    qput.add_argument("message")
    qput.set_defaults(func=cmd_queue_put)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
