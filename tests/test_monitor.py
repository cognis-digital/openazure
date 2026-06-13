"""Tests for the Azure Monitor emulation."""

import time
import pytest

from openazure.store import Store
from openazure.monitor import MonitorService, _parse_log_query, _eval_condition
from openazure.errors import NotFound, Conflict, BadRequest


@pytest.fixture
def store():
    s = Store(in_memory=True)
    yield s
    s.close()


@pytest.fixture
def mon(store):
    return MonitorService(store)


# ---------------------------------------------------------------------------
# Metrics — ingestion
# ---------------------------------------------------------------------------

def test_ingest_single_metric(mon):
    r = mon.ingest_metric("app/cpu", "usage_pct", 42.5)
    assert r["value"] == 42.5
    assert r["namespace"] == "app/cpu"
    assert r["name"] == "usage_pct"
    assert r["id"]


def test_ingest_metric_with_dimensions(mon):
    r = mon.ingest_metric("custom", "requests", 100,
                          dimensions={"region": "westus", "tier": "web"})
    assert r["dimensions"]["region"] == "westus"


def test_ingest_metrics_batch(mon):
    records = [
        {"name": "cpu", "value": 10.0},
        {"name": "mem", "value": 20.0},
        {"name": "cpu", "value": 30.0},
    ]
    results = mon.ingest_metrics_batch("ns", records)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# Metrics — listing
# ---------------------------------------------------------------------------

def test_list_metrics(mon):
    mon.ingest_metric("ns/a", "cpu", 1)
    mon.ingest_metric("ns/a", "mem", 2)
    mon.ingest_metric("ns/b", "disk", 3)
    metrics = mon.list_metrics()
    namespaces = {m["namespace"] for m in metrics}
    assert "ns/a" in namespaces
    assert "ns/b" in namespaces


def test_list_metrics_scoped_to_namespace(mon):
    mon.ingest_metric("ns1", "cpu", 1)
    mon.ingest_metric("ns2", "cpu", 2)
    metrics = mon.list_metrics("ns1")
    assert all(m["namespace"] == "ns1" for m in metrics)


# ---------------------------------------------------------------------------
# Metrics — query
# ---------------------------------------------------------------------------

def test_query_metrics_avg(mon):
    for v in [10.0, 20.0, 30.0]:
        mon.ingest_metric("app", "latency", v)
    result = mon.query_metrics("app", "latency", aggregation="avg",
                               interval_seconds=3600)
    assert result["aggregation"] == "avg"
    assert len(result["points"]) > 0
    # avg of 10+20+30 = 20
    assert abs(result["points"][0]["value"] - 20.0) < 0.01


def test_query_metrics_min_max_sum_count(mon):
    for v in [5.0, 10.0, 15.0]:
        mon.ingest_metric("ns", "m", v)
    for agg, expected in [("min", 5.0), ("max", 15.0),
                          ("sum", 30.0), ("count", 3.0)]:
        r = mon.query_metrics("ns", "m", aggregation=agg,
                              interval_seconds=3600)
        assert abs(r["points"][0]["value"] - expected) < 0.01


def test_query_metrics_no_data_returns_empty(mon):
    result = mon.query_metrics("empty", "nothing")
    assert result["points"] == []


def test_query_metrics_invalid_aggregation(mon):
    mon.ingest_metric("ns", "x", 1)
    with pytest.raises(BadRequest):
        mon.query_metrics("ns", "x", aggregation="median")


def test_query_metrics_with_dimension_filter(mon):
    mon.ingest_metric("ns", "req", 100, dimensions={"region": "eastus"})
    mon.ingest_metric("ns", "req", 200, dimensions={"region": "westus"})
    result = mon.query_metrics("ns", "req", aggregation="sum",
                               interval_seconds=3600,
                               dimension_filter={"region": "eastus"})
    assert abs(result["points"][0]["value"] - 100.0) < 0.01


def test_query_metrics_time_filter(mon):
    past = time.time() - 7200  # 2 hours ago
    now = time.time()
    mon.ingest_metric("ns", "ts", 1.0, timestamp=past)
    mon.ingest_metric("ns", "ts", 2.0, timestamp=now)
    # Only the recent point
    result = mon.query_metrics("ns", "ts", aggregation="avg",
                               start_time=now - 300,
                               interval_seconds=3600)
    assert len(result["points"]) == 1
    assert abs(result["points"][0]["value"] - 2.0) < 0.01


# ---------------------------------------------------------------------------
# Log workspaces
# ---------------------------------------------------------------------------

def test_create_list_get_workspace(mon):
    r = mon.create_workspace("myws")
    assert r["name"] == "myws"
    assert r["id"]

    workspaces = mon.list_workspaces()
    assert any(w["name"] == "myws" for w in workspaces)

    got = mon.get_workspace("myws")
    assert got["id"] == r["id"]


def test_duplicate_workspace_raises(mon):
    mon.create_workspace("ws1")
    with pytest.raises(Conflict):
        mon.create_workspace("ws1")


def test_delete_workspace(mon):
    mon.create_workspace("temp")
    mon.delete_workspace("temp")
    with pytest.raises(NotFound):
        mon.get_workspace("temp")


def test_workspace_lists_tables_after_ingest(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "CustomLogs", [{"msg": "hi"}])
    mon.ingest_logs("ws", "AppMetrics", [{"val": 1}])
    ws = mon.get_workspace("ws")
    assert "CustomLogs" in ws["tables"]
    assert "AppMetrics" in ws["tables"]


# ---------------------------------------------------------------------------
# Log ingestion
# ---------------------------------------------------------------------------

def test_ingest_logs(mon):
    mon.create_workspace("ws")
    result = mon.ingest_logs("ws", "Events", [
        {"msg": "a", "level": "info"},
        {"msg": "b", "level": "warn"},
    ])
    assert result["ingested"] == 2


def test_ingest_logs_missing_workspace(mon):
    with pytest.raises(NotFound):
        mon.ingest_logs("ghost", "table", [{"x": 1}])


# ---------------------------------------------------------------------------
# Log queries
# ---------------------------------------------------------------------------

def test_query_logs_select_all(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "Logs", [
        {"app": "api", "level": "info", "msg": "started"},
        {"app": "db", "level": "error", "msg": "failed"},
    ])
    result = mon.query_logs("ws", "SELECT * FROM Logs")
    assert result["count"] == 2


def test_query_logs_where_filter(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "AppLog", [
        {"level": "info", "code": 200},
        {"level": "error", "code": 500},
        {"level": "info", "code": 201},
    ])
    result = mon.query_logs("ws", "SELECT * FROM AppLog WHERE level = 'info'")
    assert all(r["level"] == "info" for r in result["rows"])
    assert result["count"] == 2


def test_query_logs_numeric_where(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "T", [
        {"val": 10}, {"val": 20}, {"val": 30}
    ])
    result = mon.query_logs("ws", "SELECT * FROM T WHERE val > 15")
    assert result["count"] == 2


def test_query_logs_select_columns(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "T", [{"a": 1, "b": 2, "c": 3}])
    result = mon.query_logs("ws", "SELECT a, b FROM T")
    row = result["rows"][0]
    assert "a" in row
    assert "b" in row
    assert "c" not in row


def test_query_logs_order_by_desc(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "T", [
        {"val": 3}, {"val": 1}, {"val": 2}
    ])
    result = mon.query_logs("ws", "SELECT * FROM T ORDER BY val DESC")
    vals = [r["val"] for r in result["rows"]]
    assert vals == [3, 2, 1]


def test_query_logs_limit(mon):
    mon.create_workspace("ws")
    mon.ingest_logs("ws", "T", [{"i": i} for i in range(20)])
    result = mon.query_logs("ws", "SELECT * FROM T LIMIT 5")
    assert result["count"] == 5


def test_query_logs_missing_workspace(mon):
    with pytest.raises(NotFound):
        mon.query_logs("ghost", "SELECT * FROM T")


# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------

def test_create_list_get_alert_rule(mon):
    r = mon.create_alert_rule("high-cpu", "app", "cpu_pct",
                              operator="gt", threshold=80.0)
    assert r["name"] == "high-cpu"
    assert r["threshold"] == 80.0

    rules = mon.list_alert_rules()
    assert any(rule["name"] == "high-cpu" for rule in rules)

    got = mon.get_alert_rule("high-cpu")
    assert got["id"] == r["id"]


def test_duplicate_alert_rule_raises(mon):
    mon.create_alert_rule("dup", "ns", "m")
    with pytest.raises(Conflict):
        mon.create_alert_rule("dup", "ns", "m")


def test_delete_alert_rule(mon):
    mon.create_alert_rule("temp", "ns", "m")
    mon.delete_alert_rule("temp")
    with pytest.raises(NotFound):
        mon.get_alert_rule("temp")


def test_alert_rule_invalid_operator(mon):
    with pytest.raises(BadRequest):
        mon.create_alert_rule("bad", "ns", "m", operator="between")


def test_evaluate_alert_rule_firing(mon):
    mon.create_alert_rule("fire", "app", "cpu", operator="gt", threshold=50.0,
                          window_seconds=300)
    mon.ingest_metric("app", "cpu", 90.0)
    result = mon.evaluate_alert_rule("fire")
    assert result["firing"] is True


def test_evaluate_alert_rule_not_firing(mon):
    mon.create_alert_rule("quiet", "app", "cpu2", operator="gt", threshold=90.0,
                          window_seconds=300)
    mon.ingest_metric("app", "cpu2", 10.0)
    result = mon.evaluate_alert_rule("quiet")
    assert result["firing"] is False


def test_evaluate_alert_rule_no_data(mon):
    mon.create_alert_rule("nodata", "app", "ghost_metric")
    result = mon.evaluate_alert_rule("nodata")
    assert result["firing"] is False


def test_evaluate_disabled_rule(mon):
    mon.create_alert_rule("off", "app", "x", enabled=False)
    result = mon.evaluate_alert_rule("off")
    assert result["firing"] is False
    assert result["reason"] == "disabled"


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------

def test_parse_query_select_all():
    q = _parse_log_query("SELECT * FROM MyTable")
    assert q["table"] == "MyTable"
    assert q["select"] == ["*"]


def test_parse_query_select_cols():
    q = _parse_log_query("SELECT a, b FROM T")
    assert q["select"] == ["a", "b"]


def test_parse_query_where():
    q = _parse_log_query("SELECT * FROM T WHERE level = 'error'")
    assert len(q["where"]) == 1
    assert q["where"][0]["col"] == "level"
    assert q["where"][0]["val"] == "error"


def test_parse_query_order_by_desc():
    q = _parse_log_query("SELECT * FROM T ORDER BY ts DESC")
    assert q["order_by"] == ("ts", True)


def test_parse_query_limit():
    q = _parse_log_query("SELECT * FROM T LIMIT 42")
    assert q["limit"] == 42


def test_eval_condition_eq():
    assert _eval_condition({"level": "info"}, {"col": "level", "op": "=", "val": "info"})
    assert not _eval_condition({"level": "error"}, {"col": "level", "op": "=", "val": "info"})


def test_eval_condition_numeric():
    assert _eval_condition({"code": 200}, {"col": "code", "op": "=", "val": 200})
    assert _eval_condition({"code": 500}, {"col": "code", "op": ">", "val": 400})
    assert not _eval_condition({"code": 200}, {"col": "code", "op": ">", "val": 400})
