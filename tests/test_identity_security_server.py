"""End-to-end HTTP server tests for Key Vault, Managed Identity,
App Configuration, Azure Monitor, and Notification Hubs.

Each test class starts an ephemeral in-process server on an OS-assigned port
and talks to it with urllib, matching the pattern used in the existing
test_server.py / test_messaging_server.py files.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.request
import urllib.error
import pytest

from openazure.server import make_server, OpenAzure


# ---------------------------------------------------------------------------
# Shared test-server fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server():
    httpd, app = make_server(host="127.0.0.1", port=0, in_memory=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", app
    httpd.shutdown()
    app.close()


def _get(base: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{base}{path}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _method(method: str, base: str, path: str,
            body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


_put = lambda base, path, body=None: _method("PUT", base, path, body)
_post = lambda base, path, body=None: _method("POST", base, path, body)
_delete = lambda base, path, body=None: _method("DELETE", base, path, body)


# ===========================================================================
# Key Vault — HTTP tests
# ===========================================================================

class TestKeyVaultHTTP:

    def test_health_includes_keyvault(self, server):
        base, _ = server
        status, body = _get(base, "/")
        assert "keyvault" in body["services"]

    def test_set_and_get_secret(self, server):
        base, _ = server
        status, body = _put(base, "/keyvault/v1/secrets/mypass",
                            {"value": "s3cr3t"})
        assert status == 201
        assert body["name"] == "mypass"

        status, got = _get(base, "/keyvault/v1/secrets/mypass")
        assert status == 200
        assert got["value"] == "s3cr3t"

    def test_list_secrets(self, server):
        base, _ = server
        _put(base, "/keyvault/v1/secrets/ls1", {"value": "a"})
        _put(base, "/keyvault/v1/secrets/ls2", {"value": "b"})
        status, body = _get(base, "/keyvault/v1/secrets")
        assert status == 200
        names = [s["name"] for s in body["secrets"]]
        assert "ls1" in names

    def test_delete_and_recover_secret(self, server):
        base, _ = server
        _put(base, "/keyvault/v1/secrets/drs", {"value": "x"})
        status, body = _delete(base, "/keyvault/v1/secrets/drs")
        assert status == 200
        assert body["deleted"] is True

        # Recover
        status, rec = _post(base, "/keyvault/v1/secrets/drs?comp=recover")
        assert status == 200
        assert rec["name"] == "drs"

    def test_create_key(self, server):
        base, _ = server
        status, body = _put(base, "/keyvault/v1/keys/rsa-key",
                            {"key_type": "RSA", "key_size": 2048})
        assert status == 201
        assert body["key_type"] == "RSA"

    def test_encrypt_decrypt_key(self, server):
        base, _ = server
        _put(base, "/keyvault/v1/keys/enc-k", {"key_type": "RSA"})
        pt = base64.b64encode(b"openazure secret").decode()
        status, enc = _post(base, "/keyvault/v1/keys/enc-k?comp=encrypt",
                            {"plaintext": pt})
        assert status == 200
        assert "ciphertext" in enc

        status, dec = _post(base, "/keyvault/v1/keys/enc-k?comp=decrypt",
                            {"ciphertext": enc["ciphertext"]})
        assert status == 200
        recovered = base64.b64decode(dec["plaintext"])
        assert recovered == b"openazure secret"

    def test_wrap_unwrap_key(self, server):
        base, _ = server
        _put(base, "/keyvault/v1/keys/wrap-key2", {})
        sym = base64.b64encode(b"\xca\xfe" * 8).decode()
        status, wrapped = _post(base, "/keyvault/v1/keys/wrap-key2?comp=wrap",
                                {"key": sym})
        assert status == 200
        status, unwrapped = _post(base,
                                  "/keyvault/v1/keys/wrap-key2?comp=unwrap",
                                  {"wrapped_key": wrapped["wrapped_key"]})
        assert status == 200
        assert unwrapped["unwrapped_key"] == sym

    def test_create_certificate(self, server):
        base, _ = server
        status, body = _put(base, "/keyvault/v1/certificates/mycert",
                            {"subject": "CN=www.example.com"})
        assert status == 201
        assert body["subject"] == "CN=www.example.com"
        assert body["thumbprint"]

    def test_list_deleted_secrets(self, server):
        base, _ = server
        _put(base, "/keyvault/v1/secrets/goner", {"value": "x"})
        _delete(base, "/keyvault/v1/secrets/goner")
        status, body = _get(base, "/keyvault/v1/deleted/secrets")
        assert status == 200
        names = [d["name"] for d in body["deleted"]]
        assert "goner" in names

    def test_purge_secret(self, server):
        base, _ = server
        _put(base, "/keyvault/v1/secrets/purgeme", {"value": "x"})
        _delete(base, "/keyvault/v1/secrets/purgeme")
        status, body = _post(base, "/keyvault/v1/secrets/purgeme?comp=purge")
        assert status == 200
        assert body["purged"] is True


# ===========================================================================
# Managed Identity — HTTP tests
# ===========================================================================

class TestManagedIdentityHTTP:

    def test_health_includes_identity(self, server):
        base, _ = server
        status, body = _get(base, "/")
        assert "identity" in body["services"]

    def test_register_list_get_identity(self, server):
        base, _ = server
        status, body = _put(base, "/identity/identities/svc-a",
                            {"type": "UserAssigned"})
        assert status == 201
        assert body["name"] == "svc-a"

        status, lst = _get(base, "/identity/identities")
        assert status == 200
        names = [i["name"] for i in lst["identities"]]
        assert "svc-a" in names

        status, got = _get(base, "/identity/identities/svc-a")
        assert status == 200
        assert got["client_id"]

    def test_delete_identity(self, server):
        base, _ = server
        _put(base, "/identity/identities/temp-id", {})
        status, body = _delete(base, "/identity/identities/temp-id")
        assert status == 200
        assert body["deleted"] is True

    def test_assign_list_roles(self, server):
        base, _ = server
        _put(base, "/identity/identities/role-svc", {})
        status, body = _put(base, "/identity/identities/role-svc/roles",
                            {"role": "Storage.Reader", "scope": "/"})
        assert status == 201
        assert body["role"] == "Storage.Reader"

        status, lst = _get(base, "/identity/identities/role-svc/roles")
        assert status == 200
        assert any(r["role"] == "Storage.Reader" for r in lst["roles"])

    def test_issue_token(self, server):
        base, _ = server
        _put(base, "/identity/identities/token-svc", {})
        status, body = _get(base,
                            "/identity/token?identity=token-svc&scope=openid")
        assert status == 200
        assert body["access_token"]
        assert body["token_type"] == "Bearer"

    def test_validate_token_round_trip(self, server):
        base, _ = server
        _put(base, "/identity/identities/val-svc", {})
        status, tok = _get(base, "/identity/token?identity=val-svc")
        assert status == 200
        token = tok["access_token"]

        status, val = _post(base, "/identity/validate", {"token": token})
        assert status == 200
        assert val["valid"] is True
        assert val["claims"]["name"] == "val-svc"

    def test_validate_bad_token(self, server):
        base, _ = server
        status, val = _post(base, "/identity/validate",
                            {"token": "garbage.token.here"})
        assert status == 401
        assert val["valid"] is False

    def test_revoke_token(self, server):
        base, _ = server
        _put(base, "/identity/identities/rev-svc", {})
        status, tok = _get(base, "/identity/token?identity=rev-svc")
        token = tok["access_token"]
        # Extract jti
        parts = token.split(".")
        pad = 4 - len(parts[1]) % 4
        if pad != 4:
            parts[1] += "=" * pad
        import base64 as _b64
        claims = json.loads(_b64.urlsafe_b64decode(parts[1]))
        _post(base, "/identity/revoke", {"jti": claims["jti"]})
        status, val = _post(base, "/identity/validate", {"token": token})
        assert val["valid"] is False


# ===========================================================================
# App Configuration — HTTP tests
# ===========================================================================

class TestAppConfigHTTP:

    AC = "test-appconfig"

    def test_health_includes_appconfig(self, server):
        base, _ = server
        status, body = _get(base, "/")
        assert "appconfig" in body["services"]

    def test_set_and_get_keyvalue(self, server):
        base, _ = server
        status, body = _put(base, f"/appconfig/{self.AC}/kv/app:host",
                            {"value": "localhost"})
        assert status == 200
        assert body["value"] == "localhost"

        status, got = _get(base, f"/appconfig/{self.AC}/kv/app:host")
        assert status == 200
        assert got["value"] == "localhost"

    def test_list_keyvalues(self, server):
        base, _ = server
        _put(base, f"/appconfig/{self.AC}/kv/list1", {"value": "a"})
        _put(base, f"/appconfig/{self.AC}/kv/list2", {"value": "b"})
        status, body = _get(base, f"/appconfig/{self.AC}/kv")
        assert status == 200
        keys = [i["key"] for i in body["items"]]
        assert "list1" in keys

    def test_delete_keyvalue(self, server):
        base, _ = server
        _put(base, f"/appconfig/{self.AC}/kv/delme", {"value": "x"})
        status, body = _delete(base, f"/appconfig/{self.AC}/kv/delme")
        assert status == 200
        assert body["deleted"] is True

    def test_feature_flag_set_get(self, server):
        base, _ = server
        status, body = _put(base, f"/appconfig/{self.AC}/featureflags/dark-mode",
                            {"enabled": True, "description": "Dark UI"})
        assert status == 200

        status, ff = _get(base, f"/appconfig/{self.AC}/featureflags/dark-mode")
        assert status == 200
        assert ff["feature"]["enabled"] is True

    def test_feature_flag_toggle(self, server):
        base, _ = server
        _put(base, f"/appconfig/{self.AC}/featureflags/toggle-me",
             {"enabled": False})
        status, body = _post(
            base,
            f"/appconfig/{self.AC}/featureflags/toggle-me?comp=toggle",
            {"enabled": True},
        )
        assert status == 200
        status, ff = _get(base,
                          f"/appconfig/{self.AC}/featureflags/toggle-me")
        assert ff["feature"]["enabled"] is True

    def test_list_feature_flags(self, server):
        base, _ = server
        _put(base, f"/appconfig/{self.AC}/featureflags/ff1", {"enabled": False})
        _put(base, f"/appconfig/{self.AC}/featureflags/ff2", {"enabled": True})
        status, body = _get(base, f"/appconfig/{self.AC}/featureflags")
        assert status == 200
        ids = [f["feature"].get("id") for f in body["flags"]]
        assert "ff1" in ids

    def test_create_and_get_snapshot(self, server):
        base, _ = server
        _put(base, f"/appconfig/{self.AC}/kv/snap-key", {"value": "v"})
        status, snap = _put(base,
                            f"/appconfig/{self.AC}/snapshots/snap1",
                            {})
        assert status == 201
        assert snap["name"] == "snap1"

        status, got = _get(base, f"/appconfig/{self.AC}/snapshots/snap1")
        assert status == 200
        assert got["item_count"] >= 1

    def test_delete_snapshot(self, server):
        base, _ = server
        _put(base, f"/appconfig/{self.AC}/kv/snap2-key", {"value": "v"})
        _put(base, f"/appconfig/{self.AC}/snapshots/snap2", {})
        status, body = _delete(base, f"/appconfig/{self.AC}/snapshots/snap2")
        assert status == 200
        assert body["deleted"] is True


# ===========================================================================
# Azure Monitor — HTTP tests
# ===========================================================================

class TestMonitorHTTP:

    def test_health_includes_monitor(self, server):
        base, _ = server
        status, body = _get(base, "/")
        assert "monitor" in body["services"]

    def test_ingest_and_query_metric(self, server):
        base, _ = server
        status, body = _post(base, "/monitor/metrics/app/cpu",
                             {"value": 75.0})
        assert status == 201
        assert body["value"] == 75.0

        status, result = _get(base, "/monitor/metrics/app/cpu"
                              "?aggregation=avg&interval=3600")
        assert status == 200
        assert len(result["points"]) > 0

    def test_ingest_batch_metrics(self, server):
        base, _ = server
        records = [{"name": "latency", "value": 10},
                   {"name": "latency", "value": 20}]
        status, body = _post(base, "/monitor/metrics/net", records)
        assert status == 201
        assert body["ingested"] == 2

    def test_list_metrics(self, server):
        base, _ = server
        _post(base, "/monitor/metrics/myns/mtr", {"value": 1.0})
        status, body = _get(base, "/monitor/metrics")
        assert status == 200
        namespaces = {m["namespace"] for m in body["metrics"]}
        assert "myns" in namespaces

    def test_create_get_workspace(self, server):
        base, _ = server
        status, body = _put(base, "/monitor/workspaces/myws")
        assert status == 201
        assert body["name"] == "myws"

        status, got = _get(base, "/monitor/workspaces/myws")
        assert status == 200

    def test_list_workspaces(self, server):
        base, _ = server
        _put(base, "/monitor/workspaces/ws-list")
        status, body = _get(base, "/monitor/workspaces")
        assert status == 200
        names = [w["name"] for w in body["workspaces"]]
        assert "ws-list" in names

    def test_ingest_and_query_logs(self, server):
        base, _ = server
        _put(base, "/monitor/workspaces/log-ws")
        records = [{"level": "info", "msg": "start"},
                   {"level": "error", "msg": "fail"}]
        status, ingest = _post(base,
                               "/monitor/workspaces/log-ws/logs/AppLog",
                               records)
        assert status == 201
        assert ingest["ingested"] == 2

        status, result = _post(base,
                               "/monitor/workspaces/log-ws/query",
                               {"query": "SELECT * FROM AppLog"})
        assert status == 200
        assert result["count"] == 2

    def test_query_logs_where(self, server):
        base, _ = server
        _put(base, "/monitor/workspaces/filter-ws")
        _post(base, "/monitor/workspaces/filter-ws/logs/Events",
              [{"level": "info", "code": 200},
               {"level": "error", "code": 500}])
        status, result = _post(
            base,
            "/monitor/workspaces/filter-ws/query",
            {"query": "SELECT * FROM Events WHERE level = 'error'"},
        )
        assert status == 200
        assert result["count"] == 1
        assert result["rows"][0]["level"] == "error"

    def test_delete_workspace(self, server):
        base, _ = server
        _put(base, "/monitor/workspaces/del-ws")
        status, body = _delete(base, "/monitor/workspaces/del-ws")
        assert status == 200
        assert body["deleted"] is True

    def test_create_and_evaluate_alert_rule(self, server):
        base, _ = server
        status, body = _put(base, "/monitor/alerts/high-cpu",
                            {"namespace": "srv", "metric": "cpu",
                             "operator": "gt", "threshold": 50.0,
                             "window_seconds": 300})
        assert status == 201

        _post(base, "/monitor/metrics/srv/cpu", {"value": 90.0})
        status, result = _get(base, "/monitor/alerts/high-cpu?comp=evaluate")
        assert status == 200
        assert result["firing"] is True

    def test_list_and_delete_alert_rule(self, server):
        base, _ = server
        _put(base, "/monitor/alerts/temp-rule",
             {"namespace": "x", "metric": "y"})
        status, lst = _get(base, "/monitor/alerts")
        assert status == 200
        names = [r["name"] for r in lst["rules"]]
        assert "temp-rule" in names

        status, body = _delete(base, "/monitor/alerts/temp-rule")
        assert status == 200


# ===========================================================================
# Notification Hubs — HTTP tests
# ===========================================================================

class TestNotificationHubsHTTP:

    HUB = "http-hub"

    def test_health_includes_notificationhubs(self, server):
        base, _ = server
        status, body = _get(base, "/")
        assert "notificationhubs" in body["services"]

    def test_create_list_hub(self, server):
        base, _ = server
        status, body = _put(base, f"/notificationhubs/{self.HUB}")
        assert status == 201

        status, lst = _get(base, "/notificationhubs")
        assert status == 200
        assert self.HUB in lst["hubs"]

    def test_create_and_get_registration(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        status, body = _post(
            base,
            f"/notificationhubs/{self.HUB}/registrations",
            {"handle": "device-abc", "platform": "fcm", "tags": ["news"]},
        )
        assert status == 201
        reg_id = body["id"]

        status, got = _get(base,
                           f"/notificationhubs/{self.HUB}/registrations/{reg_id}")
        assert status == 200
        assert got["handle"] == "device-abc"

    def test_list_registrations(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        _post(base, f"/notificationhubs/{self.HUB}/registrations",
              {"handle": "h-list-1"})
        status, body = _get(
            base, f"/notificationhubs/{self.HUB}/registrations"
        )
        assert status == 200
        handles = [r["handle"] for r in body["registrations"]]
        assert "h-list-1" in handles

    def test_delete_registration(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        status, reg = _post(base,
                            f"/notificationhubs/{self.HUB}/registrations",
                            {"handle": "del-handle"})
        reg_id = reg["id"]
        status, body = _delete(
            base, f"/notificationhubs/{self.HUB}/registrations/{reg_id}"
        )
        assert status == 200
        assert body["deleted"] is True

    def test_upsert_and_get_installation(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        status, body = _put(
            base,
            f"/notificationhubs/{self.HUB}/installations/inst-http-1",
            {"handle": "tok", "platform": "apns", "tags": ["vip"]},
        )
        assert status == 200
        assert body["installation_id"] == "inst-http-1"

        status, got = _get(
            base,
            f"/notificationhubs/{self.HUB}/installations/inst-http-1",
        )
        assert status == 200
        assert "vip" in got["tags"]

    def test_delete_installation(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        _put(base,
             f"/notificationhubs/{self.HUB}/installations/del-inst",
             {"handle": "x"})
        status, body = _delete(
            base,
            f"/notificationhubs/{self.HUB}/installations/del-inst",
        )
        assert status == 200
        assert body["deleted"] is True

    def test_send_broadcast_and_list_notifications(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        _post(base, f"/notificationhubs/{self.HUB}/registrations",
              {"handle": "rcpt-1"})

        status, result = _post(
            base,
            f"/notificationhubs/{self.HUB}/send",
            {"payload": {"title": "Test", "body": "Hello"}},
        )
        assert status == 201
        notif_id = result["notification_id"]
        assert result["recipient_count"] >= 1

        # List
        status, lst = _get(base,
                           f"/notificationhubs/{self.HUB}/notifications")
        assert status == 200
        ids = [n["notification_id"] for n in lst["notifications"]]
        assert notif_id in ids

        # Get detail
        status, detail = _get(
            base,
            f"/notificationhubs/{self.HUB}/notifications/{notif_id}",
        )
        assert status == 200
        assert detail["notification_id"] == notif_id

    def test_send_with_tag_expression(self, server):
        base, _ = server
        _put(base, f"/notificationhubs/{self.HUB}")
        _post(base, f"/notificationhubs/{self.HUB}/registrations",
              {"handle": "h1", "tags": ["sports"]})
        _post(base, f"/notificationhubs/{self.HUB}/registrations",
              {"handle": "h2", "tags": ["news"]})
        status, result = _post(
            base,
            f"/notificationhubs/{self.HUB}/send",
            {"payload": "alert", "tag_expression": "sports"},
        )
        assert status == 201
        # At least one recipient with sports tag
        assert result["recipient_count"] >= 1

    def test_delete_hub(self, server):
        base, _ = server
        _put(base, "/notificationhubs/del-hub-http")
        status, body = _delete(base, "/notificationhubs/del-hub-http")
        assert status == 200
        assert body["deleted"] is True
