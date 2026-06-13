"""Azure-Functions-style runner.

Registers Python handlers against two trigger types:

* **HTTP trigger** — a handler ``fn(req: dict) -> dict`` invoked for a named
  function via the server's ``/functions/<name>`` endpoint or directly via
  :meth:`FunctionRunner.invoke_http`. ``req`` carries ``method``, ``body``,
  ``headers`` and ``params``; the return dict may carry ``status`` and
  ``body``.

* **Queue trigger** — a handler ``fn(message: dict) -> None`` bound to a
  queue. :meth:`FunctionRunner.poll_queue` dequeues visible messages, runs
  the handler, and deletes each message that the handler processes without
  raising (mirroring the Functions queue-trigger delete-on-success model).

The runner is deliberately in-process and synchronous: it is meant for
local development and tests, not a distributed scheduler.
"""

from __future__ import annotations

from typing import Callable

from .errors import NotFound
from .queue import QueueService


class FunctionRunner:
    def __init__(self, queue_service: QueueService | None = None):
        self.queue_service = queue_service
        self._http_handlers: dict[str, Callable[[dict], dict]] = {}
        self._queue_handlers: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # HTTP-trigger functions
    # ------------------------------------------------------------------
    def register_http(self, name: str, handler: Callable[[dict], dict]) -> None:
        self._http_handlers[name] = handler

    def http_function(self, name: str):
        """Decorator form of :meth:`register_http`."""
        def deco(fn):
            self.register_http(name, fn)
            return fn
        return deco

    def list_http(self) -> list[str]:
        return sorted(self._http_handlers)

    def invoke_http(self, name: str, req: dict | None = None) -> dict:
        if name not in self._http_handlers:
            raise NotFound(f"http function '{name}' not registered")
        req = dict(req or {})
        req.setdefault("method", "POST")
        req.setdefault("body", None)
        req.setdefault("headers", {})
        req.setdefault("params", {})
        result = self._http_handlers[name](req)
        if result is None:
            result = {}
        if not isinstance(result, dict):
            result = {"body": result}
        result.setdefault("status", 200)
        result.setdefault("body", "")
        return result

    # ------------------------------------------------------------------
    # Queue-trigger functions
    # ------------------------------------------------------------------
    def register_queue(self, name: str, queue: str,
                       handler: Callable[[dict], None]) -> None:
        self._queue_handlers[name] = {"queue": queue, "handler": handler}

    def queue_function(self, name: str, queue: str):
        def deco(fn):
            self.register_queue(name, queue, fn)
            return fn
        return deco

    def list_queue(self) -> list[str]:
        return sorted(self._queue_handlers)

    def poll_queue(self, name: str, max_messages: int = 32,
                   visibility_timeout: float = 30.0) -> int:
        """Drain currently-visible messages for the named queue-trigger
        function. Returns the number of messages successfully processed.

        A handler that raises leaves its message in-flight (it becomes
        visible again after the visibility timeout) — at-least-once.
        """
        if self.queue_service is None:
            raise RuntimeError("FunctionRunner has no QueueService bound")
        if name not in self._queue_handlers:
            raise NotFound(f"queue function '{name}' not registered")
        spec = self._queue_handlers[name]
        queue = spec["queue"]
        handler = spec["handler"]
        processed = 0
        msgs = self.queue_service.dequeue(
            queue, max_messages=max_messages,
            visibility_timeout=visibility_timeout,
        )
        for m in msgs:
            handler(m)  # may raise -> message stays in-flight
            self.queue_service.delete_message(queue, m["id"], m["pop_receipt"])
            processed += 1
        return processed
