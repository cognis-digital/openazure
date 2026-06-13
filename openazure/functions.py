"""Azure-Functions-style runner.

Registers Python handlers against multiple trigger types:

* **HTTP trigger** — a handler ``fn(req: dict) -> dict`` invoked for a named
  function via the server's ``/functions/<name>`` endpoint or directly via
  :meth:`FunctionRunner.invoke_http`. ``req`` carries ``method``, ``body``,
  ``headers`` and ``params``; the return dict may carry ``status`` and
  ``body``.

* **Queue trigger** — a handler ``fn(message: dict) -> None`` bound to a
  queue. :meth:`FunctionRunner.poll_queue` dequeues visible messages, runs
  the handler, and deletes each message that the handler processes without
  raising (mirroring the Functions queue-trigger delete-on-success model).

* **Timer trigger** — a handler ``fn(timer_info: dict) -> None`` invoked by
  :meth:`FunctionRunner.fire_timer`. ``timer_info`` carries ``name`` and
  ``fired_at`` (ISO timestamp). Useful for testing scheduled functions.

* **Blob trigger** — a handler ``fn(blob_info: dict) -> None`` called when
  a blob is written. :meth:`FunctionRunner.trigger_blob` invokes the handler
  with ``{"container": ..., "name": ..., "content_type": ..., "size": ...}``.

* **Service Bus trigger** — a handler ``fn(message: dict) -> None`` bound to
  a Service Bus queue or subscription.
  :meth:`FunctionRunner.poll_service_bus` receives messages, calls the
  handler, and completes each message that the handler processes without
  raising.

The runner is deliberately in-process and synchronous: it is meant for
local development and tests, not a distributed scheduler.
"""

from __future__ import annotations

import time
from typing import Callable

from .errors import NotFound
from .queue import QueueService


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FunctionRunner:
    def __init__(self, queue_service: QueueService | None = None,
                 service_bus_service=None):
        self.queue_service = queue_service
        self.service_bus_service = service_bus_service
        self._http_handlers: dict[str, Callable[[dict], dict]] = {}
        self._queue_handlers: dict[str, dict] = {}
        self._timer_handlers: dict[str, Callable[[dict], None]] = {}
        self._blob_handlers: dict[str, dict] = {}
        self._sb_handlers: dict[str, dict] = {}

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

    # ------------------------------------------------------------------
    # Timer-trigger functions
    # ------------------------------------------------------------------
    def register_timer(self, name: str,
                       handler: Callable[[dict], None]) -> None:
        """Register a timer-triggered function handler."""
        self._timer_handlers[name] = handler

    def timer_function(self, name: str):
        """Decorator form of :meth:`register_timer`."""
        def deco(fn):
            self.register_timer(name, fn)
            return fn
        return deco

    def list_timer(self) -> list[str]:
        return sorted(self._timer_handlers)

    def fire_timer(self, name: str) -> None:
        """Manually fire a timer-triggered function (useful in tests)."""
        if name not in self._timer_handlers:
            raise NotFound(f"timer function '{name}' not registered")
        timer_info = {"name": name, "fired_at": _now_iso(), "is_past_due": False}
        self._timer_handlers[name](timer_info)

    # ------------------------------------------------------------------
    # Blob-trigger functions
    # ------------------------------------------------------------------
    def register_blob(self, name: str, container: str,
                      handler: Callable[[dict], None]) -> None:
        """Register a blob-triggered function handler.

        ``container`` is the container to watch. The handler receives a
        ``blob_info`` dict with ``container``, ``name``, ``content_type``,
        and ``size``.
        """
        self._blob_handlers[name] = {"container": container, "handler": handler}

    def blob_function(self, name: str, container: str):
        """Decorator form of :meth:`register_blob`."""
        def deco(fn):
            self.register_blob(name, container, fn)
            return fn
        return deco

    def list_blob(self) -> list[str]:
        return sorted(self._blob_handlers)

    def trigger_blob(self, name: str, blob_info: dict) -> None:
        """Manually trigger a blob-triggered function (useful in tests and
        when the blob service calls back into the runner after a PUT)."""
        if name not in self._blob_handlers:
            raise NotFound(f"blob function '{name}' not registered")
        self._blob_handlers[name]["handler"](blob_info)

    def trigger_blob_for_container(self, container: str,
                                   blob_info: dict) -> int:
        """Fire all blob-trigger handlers registered for ``container``.
        Returns the number of handlers called."""
        called = 0
        for spec in self._blob_handlers.values():
            if spec["container"] == container:
                spec["handler"](blob_info)
                called += 1
        return called

    # ------------------------------------------------------------------
    # Service-Bus-trigger functions
    # ------------------------------------------------------------------
    def register_service_bus(self, name: str, destination: str,
                              handler: Callable[[dict], None]) -> None:
        """Register a Service Bus triggered function.

        ``destination`` is either a queue name or ``"<topic>/<sub>"`` for
        subscriptions.
        """
        self._sb_handlers[name] = {
            "destination": destination,
            "handler": handler,
        }

    def service_bus_function(self, name: str, destination: str):
        """Decorator form of :meth:`register_service_bus`."""
        def deco(fn):
            self.register_service_bus(name, destination, fn)
            return fn
        return deco

    def list_service_bus(self) -> list[str]:
        return sorted(self._sb_handlers)

    def poll_service_bus(self, name: str, max_messages: int = 32,
                         lock_duration: int = 60) -> int:
        """Drain messages from the Service Bus destination bound to ``name``.

        Returns the number of messages successfully processed. A handler
        that raises abandons the message (it becomes visible again after
        the lock expires).
        """
        if self.service_bus_service is None:
            raise RuntimeError(
                "FunctionRunner has no ServiceBusService bound"
            )
        if name not in self._sb_handlers:
            raise NotFound(f"service bus function '{name}' not registered")
        spec = self._sb_handlers[name]
        destination = spec["destination"]
        handler = spec["handler"]
        svc = self.service_bus_service

        # Determine if it's a queue or topic/sub
        if "/" in destination:
            parts = destination.split("/", 1)
            msgs = svc.receive_subscription(
                parts[0], parts[1],
                max_messages=max_messages,
                lock_duration=lock_duration,
            )
        else:
            msgs = svc.receive_queue(
                destination,
                max_messages=max_messages,
                lock_duration=lock_duration,
            )
        processed = 0
        for m in msgs:
            handler(m)   # may raise -> lock expires -> redelivered
            svc.complete_message(destination, m["lock_token"])
            processed += 1
        return processed
