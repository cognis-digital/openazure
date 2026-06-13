"""Azure Monitor emulation.

Supports:
* Metric ingestion — ingest named metrics with timestamp, value, dimensions,
  and namespace.
* Metric queries — query metrics by namespace + name, with time range,
  aggregation (avg/min/max/sum/count), and optional dimension filter.
* Log workspaces — create and delete workspaces; each workspace has its own
  tables.
* Log ingestion — ingest log records (arbitrary JSON objects) into named
  tables within a workspace.
* Log queries — query a workspace table using a simple SQL-subset syntax
  (SELECT ... WHERE ... ORDER BY ... LIMIT); a minimal in-process evaluator is
  provided (no KQL parsing — this is a local testing emulator).
* Alert rules — define an alert on a metric threshold; list and delete rules.

All data is persisted in the shared sqlite Store.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from .errors import NotFound, Conflict, BadRequest
from .store import Store


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MonitorService:
    """Local emulation of Azure Monitor (metrics + logs)."""

    def __init__(self, store: Store):
        self.store = store
        self._init_schema()

    def _init_schema(self):
        # Metrics
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS am_metrics (
                id        TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                name      TEXT NOT NULL,
                value     REAL NOT NULL,
                timestamp REAL NOT NULL,
                dimensions TEXT NOT NULL DEFAULT '{}'
            )
        """)
        # Log workspaces
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS am_workspaces (
                id      TEXT PRIMARY KEY,
                name    TEXT NOT NULL UNIQUE,
                created REAL NOT NULL
            )
        """)
        # Log records (one table per (workspace, table_name) — stored as JSON)
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS am_logs (
                id        TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                table_name TEXT NOT NULL,
                timestamp REAL NOT NULL,
                record    TEXT NOT NULL
            )
        """)
        # Alert rules
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS am_alert_rules (
                id        TEXT PRIMARY KEY,
                name      TEXT NOT NULL UNIQUE,
                namespace TEXT NOT NULL,
                metric    TEXT NOT NULL,
                operator  TEXT NOT NULL,
                threshold REAL NOT NULL,
                window_seconds INTEGER NOT NULL DEFAULT 300,
                severity  INTEGER NOT NULL DEFAULT 3,
                enabled   INTEGER NOT NULL DEFAULT 1,
                created   REAL NOT NULL
            )
        """)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def ingest_metric(self, namespace: str, name: str, value: float, *,
                      timestamp: float | None = None,
                      dimensions: dict | None = None) -> dict:
        """Ingest a single metric data point."""
        mid = uuid.uuid4().hex
        ts = timestamp or _now()
        self.store.execute(
            "INSERT INTO am_metrics VALUES (?,?,?,?,?,?)",
            (mid, namespace, name, value, ts,
             json.dumps(dimensions or {})),
        )
        return {"id": mid, "namespace": namespace, "name": name,
                "value": value, "timestamp": ts,
                "dimensions": dimensions or {}}

    def ingest_metrics_batch(self, namespace: str,
                             records: list[dict]) -> list[dict]:
        """Ingest multiple metric data points."""
        return [
            self.ingest_metric(
                namespace,
                r.get("name", ""),
                float(r.get("value", 0)),
                timestamp=r.get("timestamp"),
                dimensions=r.get("dimensions"),
            )
            for r in records
        ]

    def query_metrics(self, namespace: str, name: str, *,
                      start_time: float | None = None,
                      end_time: float | None = None,
                      aggregation: str = "avg",
                      interval_seconds: int = 60,
                      dimension_filter: dict | None = None) -> dict:
        """
        Query metric values with optional time range and aggregation.

        Aggregations: avg, min, max, sum, count.
        Returns bucketed time-series points.
        """
        agg = aggregation.lower()
        if agg not in ("avg", "min", "max", "sum", "count"):
            raise BadRequest(f"Unsupported aggregation: {aggregation}")

        rows = self.store.query(
            "SELECT value, timestamp, dimensions FROM am_metrics "
            "WHERE namespace=? AND name=? ORDER BY timestamp",
            (namespace, name),
        )

        # Apply time filters and dimension filters
        filtered = []
        for r in rows:
            if start_time and r["timestamp"] < start_time:
                continue
            if end_time and r["timestamp"] > end_time:
                continue
            if dimension_filter:
                dims = json.loads(r["dimensions"] or "{}")
                if not all(dims.get(k) == v
                           for k, v in dimension_filter.items()):
                    continue
            filtered.append(r)

        if not filtered:
            return {"namespace": namespace, "name": name,
                    "aggregation": aggregation, "points": []}

        # Bucket by interval
        if filtered:
            first_ts = filtered[0]["timestamp"]
        else:
            first_ts = start_time or _now()

        buckets: dict[int, list[float]] = {}
        for r in filtered:
            bucket = int((r["timestamp"] - first_ts) / interval_seconds)
            buckets.setdefault(bucket, []).append(r["value"])

        points = []
        for bucket, values in sorted(buckets.items()):
            ts = first_ts + bucket * interval_seconds
            if agg == "avg":
                v = sum(values) / len(values)
            elif agg == "min":
                v = min(values)
            elif agg == "max":
                v = max(values)
            elif agg == "sum":
                v = sum(values)
            else:  # count
                v = float(len(values))
            points.append({"timestamp": ts, "value": v, "count": len(values)})

        return {"namespace": namespace, "name": name,
                "aggregation": aggregation,
                "interval_seconds": interval_seconds,
                "points": points}

    def list_metrics(self, namespace: str | None = None) -> list[dict]:
        """List distinct metric names (optionally scoped to a namespace)."""
        if namespace:
            rows = self.store.query(
                "SELECT DISTINCT namespace, name FROM am_metrics "
                "WHERE namespace=? ORDER BY name",
                (namespace,),
            )
        else:
            rows = self.store.query(
                "SELECT DISTINCT namespace, name FROM am_metrics ORDER BY namespace, name"
            )
        return [{"namespace": r["namespace"], "name": r["name"]} for r in rows]

    # ------------------------------------------------------------------
    # Log workspaces
    # ------------------------------------------------------------------

    def create_workspace(self, name: str) -> dict:
        if self.store.query(
            "SELECT id FROM am_workspaces WHERE name=?", (name,)
        ):
            raise Conflict(f"Workspace '{name}' already exists")
        wid = uuid.uuid4().hex
        now = _now()
        self.store.execute(
            "INSERT INTO am_workspaces VALUES (?,?,?)", (wid, name, now)
        )
        return {"id": wid, "name": name, "created": now}

    def delete_workspace(self, name: str) -> None:
        rows = self.store.query(
            "SELECT id FROM am_workspaces WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"Workspace '{name}' not found")
        self.store.execute(
            "DELETE FROM am_logs WHERE workspace=?", (name,)
        )
        self.store.execute(
            "DELETE FROM am_workspaces WHERE name=?", (name,)
        )

    def list_workspaces(self) -> list[dict]:
        rows = self.store.query(
            "SELECT id, name, created FROM am_workspaces ORDER BY name"
        )
        return [{"id": r["id"], "name": r["name"],
                 "created": r["created"]} for r in rows]

    def get_workspace(self, name: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM am_workspaces WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"Workspace '{name}' not found")
        r = rows[0]
        # List tables
        tables = self.store.query(
            "SELECT DISTINCT table_name FROM am_logs WHERE workspace=? ORDER BY table_name",
            (name,),
        )
        return {"id": r["id"], "name": r["name"], "created": r["created"],
                "tables": [t["table_name"] for t in tables]}

    # ------------------------------------------------------------------
    # Log ingestion
    # ------------------------------------------------------------------

    def ingest_logs(self, workspace: str, table_name: str,
                    records: list[dict]) -> dict:
        """Ingest a list of JSON log records into a workspace table."""
        if not self.store.query(
            "SELECT id FROM am_workspaces WHERE name=?", (workspace,)
        ):
            raise NotFound(f"Workspace '{workspace}' not found")
        now = _now()
        count = 0
        for rec in records:
            rid = uuid.uuid4().hex
            ts = float(rec.get("TimeGenerated", rec.get("timestamp", now)))
            self.store.execute(
                "INSERT INTO am_logs VALUES (?,?,?,?,?)",
                (rid, workspace, table_name, ts, json.dumps(rec)),
            )
            count += 1
        return {"workspace": workspace, "table": table_name,
                "ingested": count}

    # ------------------------------------------------------------------
    # Log queries
    # ------------------------------------------------------------------

    def query_logs(self, workspace: str, query: str, *,
                   start_time: float | None = None,
                   end_time: float | None = None,
                   limit: int = 500) -> dict:
        """
        Execute a simple SQL-like query against workspace logs.

        Syntax supported:
            SELECT <col1>, <col2>, ... | *
            FROM <table>
            [WHERE <col> <op> <val> [AND ...]]
            [ORDER BY <col> [ASC|DESC]]
            [LIMIT <n>]

        Values are matched against the JSON record fields.
        """
        if not self.store.query(
            "SELECT id FROM am_workspaces WHERE name=?", (workspace,)
        ):
            raise NotFound(f"Workspace '{workspace}' not found")

        parsed = _parse_log_query(query)
        table = parsed["table"]

        rows = self.store.query(
            "SELECT record, timestamp FROM am_logs "
            "WHERE workspace=? AND table_name=? ORDER BY timestamp",
            (workspace, table),
        )

        # Decode and time-filter
        records = []
        for r in rows:
            if start_time and r["timestamp"] < start_time:
                continue
            if end_time and r["timestamp"] > end_time:
                continue
            try:
                rec = json.loads(r["record"])
            except json.JSONDecodeError:
                continue
            records.append(rec)

        # WHERE filtering
        for cond in parsed.get("where", []):
            records = [r for r in records if _eval_condition(r, cond)]

        # ORDER BY
        order_col, order_desc = parsed.get("order_by", (None, False))
        if order_col:
            records.sort(key=lambda r: r.get(order_col, ""),
                         reverse=order_desc)

        # SELECT projection
        select_cols = parsed.get("select", ["*"])
        if select_cols != ["*"]:
            records = [{c: r.get(c) for c in select_cols} for r in records]

        # LIMIT
        q_limit = parsed.get("limit", limit)
        records = records[:q_limit]

        return {"workspace": workspace, "table": table,
                "query": query, "count": len(records), "rows": records}

    # ------------------------------------------------------------------
    # Alert rules
    # ------------------------------------------------------------------

    def create_alert_rule(self, name: str, namespace: str, metric: str, *,
                          operator: str = "gt",
                          threshold: float = 0.0,
                          window_seconds: int = 300,
                          severity: int = 3,
                          enabled: bool = True) -> dict:
        if operator not in ("gt", "lt", "gte", "lte", "eq", "ne"):
            raise BadRequest(f"Unsupported operator: {operator}")
        if self.store.query(
            "SELECT id FROM am_alert_rules WHERE name=?", (name,)
        ):
            raise Conflict(f"Alert rule '{name}' already exists")
        rid = uuid.uuid4().hex
        now = _now()
        self.store.execute(
            "INSERT INTO am_alert_rules VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, name, namespace, metric, operator, threshold,
             window_seconds, severity, 1 if enabled else 0, now),
        )
        return self._rule_dict(rid)

    def get_alert_rule(self, name: str) -> dict:
        rows = self.store.query(
            "SELECT id FROM am_alert_rules WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"Alert rule '{name}' not found")
        return self._rule_dict(rows[0]["id"])

    def list_alert_rules(self) -> list[dict]:
        rows = self.store.query(
            "SELECT id FROM am_alert_rules ORDER BY name"
        )
        return [self._rule_dict(r["id"]) for r in rows]

    def delete_alert_rule(self, name: str) -> None:
        if not self.store.query(
            "SELECT id FROM am_alert_rules WHERE name=?", (name,)
        ):
            raise NotFound(f"Alert rule '{name}' not found")
        self.store.execute(
            "DELETE FROM am_alert_rules WHERE name=?", (name,)
        )

    def evaluate_alert_rule(self, name: str) -> dict:
        """Evaluate a rule against current metric data; returns firing state."""
        rows = self.store.query(
            "SELECT * FROM am_alert_rules WHERE name=?", (name,)
        )
        if not rows:
            raise NotFound(f"Alert rule '{name}' not found")
        r = rows[0]
        if not r["enabled"]:
            return {"name": name, "firing": False, "reason": "disabled"}

        window_start = _now() - r["window_seconds"]
        metric_rows = self.store.query(
            "SELECT value FROM am_metrics "
            "WHERE namespace=? AND name=? AND timestamp>=?",
            (r["namespace"], r["metric"], window_start),
        )
        if not metric_rows:
            return {"name": name, "firing": False,
                    "reason": "no data in window"}

        values = [row["value"] for row in metric_rows]
        avg_val = sum(values) / len(values)
        threshold = r["threshold"]
        op = r["operator"]
        OPS = {
            "gt": avg_val > threshold,
            "lt": avg_val < threshold,
            "gte": avg_val >= threshold,
            "lte": avg_val <= threshold,
            "eq": avg_val == threshold,
            "ne": avg_val != threshold,
        }
        firing = OPS[op]
        return {"name": name, "firing": firing,
                "avg_value": avg_val,
                "threshold": threshold,
                "operator": op,
                "data_points": len(values)}

    def _rule_dict(self, rule_id: str) -> dict:
        rows = self.store.query(
            "SELECT * FROM am_alert_rules WHERE id=?", (rule_id,)
        )
        r = rows[0]
        return {
            "id": r["id"],
            "name": r["name"],
            "namespace": r["namespace"],
            "metric": r["metric"],
            "operator": r["operator"],
            "threshold": r["threshold"],
            "window_seconds": r["window_seconds"],
            "severity": r["severity"],
            "enabled": bool(r["enabled"]),
            "created": r["created"],
        }


# ---------------------------------------------------------------------------
# Minimal log-query parser
# ---------------------------------------------------------------------------

def _parse_log_query(query: str) -> dict:
    """Parse a minimal SQL-like log query into a structure."""
    q = query.strip()
    result: dict = {"select": ["*"], "table": "", "where": [],
                    "order_by": (None, False), "limit": 500}

    # SELECT
    m = re.match(r"(?i)SELECT\s+(.+?)\s+FROM\s+", q)
    if m:
        cols_str = m.group(1).strip()
        if cols_str != "*":
            result["select"] = [c.strip() for c in cols_str.split(",")]

    # FROM
    m = re.match(r"(?i).*?\bFROM\s+(\S+)", q)
    if m:
        result["table"] = m.group(1).strip()

    # WHERE
    where_m = re.search(r"(?i)\bWHERE\b(.+?)(?:\bORDER BY\b|\bLIMIT\b|$)", q)
    if where_m:
        where_clause = where_m.group(1).strip()
        conds = re.split(r"(?i)\bAND\b", where_clause)
        for cond in conds:
            parsed_cond = _parse_condition(cond.strip())
            if parsed_cond:
                result["where"].append(parsed_cond)

    # ORDER BY
    order_m = re.search(r"(?i)\bORDER BY\s+(\S+)(?:\s+(ASC|DESC))?", q)
    if order_m:
        col = order_m.group(1)
        desc = (order_m.group(2) or "ASC").upper() == "DESC"
        result["order_by"] = (col, desc)

    # LIMIT
    limit_m = re.search(r"(?i)\bLIMIT\s+(\d+)", q)
    if limit_m:
        result["limit"] = int(limit_m.group(1))

    return result


def _parse_condition(cond: str):
    """Parse a single WHERE condition like ``col op value``."""
    m = re.match(r"(\w+)\s*(=|!=|<>|>=|<=|>|<)\s*(.+)$", cond.strip())
    if not m:
        return None
    col, op, val_str = m.group(1), m.group(2), m.group(3).strip()
    # Normalise op
    if op == "<>":
        op = "!="
    # Parse value
    if (val_str.startswith("'") and val_str.endswith("'")):
        val: object = val_str[1:-1]
    elif val_str.lower() == "true":
        val = True
    elif val_str.lower() == "false":
        val = False
    else:
        try:
            val = float(val_str) if "." in val_str else int(val_str)
        except ValueError:
            val = val_str
    return {"col": col, "op": op, "val": val}


def _eval_condition(record: dict, cond: dict) -> bool:
    """Evaluate a parsed condition against a log record dict."""
    col_val = record.get(cond["col"])
    op = cond["op"]
    ref = cond["val"]
    if col_val is None:
        return False
    try:
        if op == "=":
            return col_val == ref
        if op == "!=":
            return col_val != ref
        if op == ">":
            return col_val > ref  # type: ignore[operator]
        if op == "<":
            return col_val < ref  # type: ignore[operator]
        if op == ">=":
            return col_val >= ref  # type: ignore[operator]
        if op == "<=":
            return col_val <= ref  # type: ignore[operator]
    except TypeError:
        return False
    return False
