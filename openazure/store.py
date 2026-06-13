"""Shared storage backend helpers for openazure services.

Provides a thin sqlite3 connection factory that supports both an on-disk
data directory and a pure in-memory mode (used by the test-suite and the
``--in-memory`` server flag). All services share the same connection so a
single in-memory database is consistent across services within one process.
"""

from __future__ import annotations

import os
import sqlite3
import threading


class Store:
    """A small wrapper around a single sqlite3 connection.

    Parameters
    ----------
    data_dir:
        Directory under which the sqlite database file is created. If
        ``None`` (or when ``in_memory`` is True) an in-memory database is
        used instead.
    in_memory:
        Force a ``:memory:`` database regardless of ``data_dir``.
    """

    def __init__(self, data_dir: str | None = None, in_memory: bool = False):
        self._lock = threading.RLock()
        if in_memory or data_dir is None:
            self.path = ":memory:"
            self.data_dir = None
        else:
            os.makedirs(data_dir, exist_ok=True)
            self.data_dir = data_dir
            self.path = os.path.join(data_dir, "openazure.db")
        # check_same_thread=False because the HTTP server is threaded; we
        # guard all access with our own lock.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: tuple = ()):  # convenience
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def close(self):
        with self._lock:
            self._conn.close()
