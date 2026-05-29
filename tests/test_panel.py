import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def panel_app(tmp_path, monkeypatch):
    return make_panel_client(tmp_path, monkeypatch)


def make_panel_client(tmp_path, monkeypatch, credentials_secret_key: str | None = "unit-secret-key"):
    config_path = tmp_path / "config" / "mirrors.yml"
    state_path = tmp_path / "data" / "sync-state.json"
    log_path = tmp_path / "data" / "sync.log"
    trigger_path = tmp_path / "data" / ".trigger"
    db_path = tmp_path / "data" / "mirror-registry.db"
    registry_storage_path = tmp_path / "data" / "registry"
    static_dir = tmp_path / "static"

    static_dir.mkdir(parents=True)
    registry_storage_path.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "mirrors: []\nsettings:\n  check_interval_minutes: 30\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("LOG_PATH", str(log_path))
    monkeypatch.setenv("TRIGGER_PATH", str(trigger_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("REGISTRY_STORAGE_PATH", str(registry_storage_path))
    monkeypatch.setenv("STATIC_DIR", str(static_dir))
    monkeypatch.setenv("PANEL_TOKEN", "test-token")
    if credentials_secret_key is None:
        monkeypatch.delenv("CREDENTIALS_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("CREDENTIALS_SECRET_KEY", credentials_secret_key)

    import panel.main as panel_main

    importlib.reload(panel_main)
    return TestClient(panel_main.app), config_path, state_path, trigger_path


def test_status_and_mirror_crud(panel_app):
    client, config_path, state_path, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    assert client.get("/api/status").json()["total"] == 0
    assert client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
        },
        headers=headers,
    ).status_code == 200

    mirrors = client.get("/api/mirrors").json()
    assert mirrors[0]["source"] == "docker.io/library/busybox:latest"
    assert "docker.io/library/busybox:latest" in config_path.read_text(encoding="utf-8")

    state_path.write_text(json.dumps({"docker.io/library/busybox:latest": "sha256:abc"}), encoding="utf-8")
    assert client.post("/api/mirrors/0/reset", headers=headers).status_code == 200
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}

    assert client.delete("/api/mirrors/0", headers=headers).status_code == 200
    assert client.get("/api/status").json()["total"] == 0


def test_write_routes_require_token(panel_app):
    client, _, _, _ = panel_app

    response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
        },
    )

    assert response.status_code == 401


def test_trigger_sync_creates_trigger_file(panel_app):
    client, _, _, trigger_path = panel_app

    response = client.post("/api/sync", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    assert trigger_path.exists()
    assert "manual" in trigger_path.read_text(encoding="utf-8")


def test_rejects_image_reference_without_tag(panel_app):
    client, _, _, _ = panel_app

    response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox",
            "target": "localhost:5000/library/busybox:latest",
        },
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 400


def test_single_mirror_sync_trigger_writes_source(panel_app):
    client, _, _, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}

    client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
        },
        headers=headers,
    )
    response = client.post("/api/mirrors/0/sync", headers=headers)

    assert response.status_code == 200
    assert "docker.io/library/busybox:latest" in trigger_path.read_text(encoding="utf-8")


def test_diagnostics_and_sync_runs_are_available(panel_app):
    client, _, _, _ = panel_app

    assert client.get("/api/diagnostics").status_code == 200
    assert client.post("/api/diagnostics/run").status_code == 200
    assert client.get("/api/sync-runs").status_code == 200


def test_settings_include_v3_controls(panel_app):
    client, config_path, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    response = client.put(
        "/api/settings",
        json={
            "check_interval_minutes": 15,
            "sync_concurrency": 3,
            "sync_retry_count": 4,
            "notify_webhook_url": "https://example.com/hook",
        },
        headers=headers,
    )

    assert response.status_code == 200
    settings = client.get("/api/settings").json()
    assert settings["check_interval_minutes"] == 15
    assert settings["sync_concurrency"] == 3
    assert settings["sync_retry_count"] == 4
    assert settings["notify_webhook_configured"] is True
    assert "https://example.com/hook" in config_path.read_text(encoding="utf-8")


def test_mirror_export_import(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "mirrors": [
            {
                "source": "docker.io/library/busybox:latest",
                "target": "localhost:5000/library/busybox:latest",
            }
        ],
        "replace": True,
    }

    response = client.post("/api/mirrors/import", json=payload, headers=headers)

    assert response.status_code == 200
    exported = client.get("/api/mirrors/export").json()
    assert exported["mirrors"][0]["source"] == "docker.io/library/busybox:latest"
    assert exported["version"] == 2


def test_retry_failed_run_writes_sources_trigger(panel_app):
    client, _, _, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}

    import panel.main as panel_main

    run_id = panel_main.db_execute(
        "INSERT INTO sync_runs(reason, status, only_source, started_at, failed) VALUES (?, ?, ?, ?, ?)",
        ("manual", "failed", None, panel_main.now_iso(), 1),
    )
    panel_main.db_execute(
        """
        INSERT INTO sync_run_items(run_id, source, target, status, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "docker.io/library/busybox:latest",
            "localhost:5000/library/busybox:latest",
            "failed",
            panel_main.now_iso(),
        ),
    )

    response = client.post(f"/api/sync-runs/{run_id}/retry", headers=headers)

    assert response.status_code == 200
    trigger = json.loads(trigger_path.read_text(encoding="utf-8"))
    assert trigger["reason"] == "retry-run"
    assert trigger["sources"] == ["docker.io/library/busybox:latest"]


def test_storage_delete_mark_and_security_guide(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    response = client.post(
        "/api/storage/delete-mark",
        json={"repo": "library/busybox", "tag": "latest", "reason": "cleanup"},
        headers=headers,
    )

    assert response.status_code == 200
    storage = client.get("/api/storage").json()
    assert storage["deletion_marks"][0]["repo"] == "library/busybox"
    assert "garbage-collect" in "\n".join(storage["garbage_collection"]["commands"])
    assert client.get("/api/security-guide").json()["recommended"]


def test_v4_registry_group_platform_and_audit(panel_app):
    client, config_path, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    registry_response = client.post(
        "/api/registries",
        json={"id": "prod", "name": "Production", "url": "https://registry.example.com", "copy_host": "registry.example.com"},
        headers=headers,
    )
    group_response = client.post(
        "/api/mirror-groups",
        json={
            "id": "prod-app",
            "name": "Prod App",
            "project": "app",
            "environment": "prod",
            "namespace": "library",
            "registry": "prod",
        },
        headers=headers,
    )
    mirror_response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "registry.example.com/library/busybox:latest",
            "registry": "prod",
            "group": "prod-app",
            "project": "app",
            "environment": "prod",
            "namespace": "library",
        },
        headers=headers,
    )

    assert registry_response.status_code == 200
    assert group_response.status_code == 200
    assert mirror_response.status_code == 200
    platform = client.get("/api/platform").json()
    grouped = client.get("/api/platform/groups").json()
    audit = client.get("/api/audit-logs").json()
    assert any(item["id"] == "prod" for item in platform["registries"])
    assert grouped[0]["project"] == "app"
    assert grouped[0]["environment"] == "prod"
    assert any(item["resource_type"] == "mirror" and item["action"] == "create" for item in audit)
    assert "mirror_groups" in config_path.read_text(encoding="utf-8")


def test_v4_database_configuration_guide(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    response = client.put(
        "/api/settings",
        json={"database_url": "postgresql://mirror:password@postgres:5432/mirror_registry"},
        headers=headers,
    )

    assert response.status_code == 200
    settings = client.get("/api/settings").json()
    guide = client.get("/api/database-guide").json()
    diagnostics = client.post("/api/diagnostics/run").json()
    assert settings["database_backend"] == "postgresql"
    assert guide["supported_backends"] == ["sqlite", "postgresql", "mysql"]
    assert any(item["name"] == "数据库后端" for item in diagnostics["checks"])


def test_credentials_crud_test_and_secret_redaction(panel_app, monkeypatch):
    client, config_path, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    create = client.post(
        "/api/credentials",
        json={
            "id": "dockerhub",
            "name": "Docker Hub",
            "registry_host": "https://index.docker.io",
            "username": "alice",
            "secret": "top-secret",
            "scope": "both",
        },
        headers=headers,
    )

    assert create.status_code == 200
    listed = client.get("/api/credentials").json()
    assert listed[0]["id"] == "dockerhub"
    assert listed[0]["registry_host"] == "index.docker.io"
    assert listed[0]["configured"] is True
    assert "secret" not in json.dumps(listed)

    import panel.main as panel_main

    status_holder = {"code": 200}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, auth):
            assert url == "https://index.docker.io/v2/"
            assert auth == ("alice", "top-secret")
            return type("Response", (), {"status_code": status_holder["code"]})()

    monkeypatch.setattr(panel_main.httpx, "AsyncClient", FakeAsyncClient)
    test_response = client.post("/api/credentials/dockerhub/test", json={}, headers=headers)
    assert test_response.status_code == 200
    assert test_response.json()["status"] == "ok"
    status_holder["code"] = 401
    assert client.post("/api/credentials/dockerhub/test", json={}, headers=headers).json()["status"] == "authentication_failed"
    status_holder["code"] = 403
    assert client.post("/api/credentials/dockerhub/test", json={}, headers=headers).json()["status"] == "permission_denied"

    update = client.put(
        "/api/credentials/dockerhub",
        json={
            "name": "Docker Hub Read",
            "registry_host": "index.docker.io",
            "username": "alice",
            "scope": "source",
        },
        headers=headers,
    )
    assert update.status_code == 200
    assert update.json()["credential"]["scope"] == "source"

    audit = client.get("/api/audit-logs").json()
    assert "top-secret" not in json.dumps(audit)
    assert "alice" not in json.dumps(audit)

    client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
            "source_credential_id": "dockerhub",
        },
        headers=headers,
    )
    assert "source_credential_id: dockerhub" in config_path.read_text(encoding="utf-8")
    assert client.delete("/api/credentials/dockerhub", headers=headers).status_code == 400


def test_credentials_require_secret_key(tmp_path, monkeypatch):
    client, _, _, _ = make_panel_client(tmp_path, monkeypatch, credentials_secret_key=None)
    response = client.post(
        "/api/credentials",
        json={
            "id": "missing-key",
            "name": "Missing Key",
            "registry_host": "ghcr.io",
            "username": "alice",
            "secret": "top-secret",
        },
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 400
    diagnostics = client.post("/api/diagnostics/run").json()
    assert any(item["name"] == "仓库凭据密钥" and item["status"] == "warn" for item in diagnostics["checks"])


def test_governance_blocks_protected_delete_and_retention_marks(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    rule_response = client.post(
        "/api/tag-protection",
        json={"id": "release-tags", "name": "Release tags", "repo_pattern": "library/*", "tag_pattern": "v*", "environment": "*"},
        headers=headers,
    )
    assert rule_response.status_code == 200
    check_response = client.get("/api/tag-protection/check", params={"repo": "library/busybox", "tag": "v1.0.0"}).json()
    assert check_response["protected"] is True

    protected = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:v1.0.0",
            "target": "localhost:5000/library/busybox:v1.0.0",
            "environment": "prod",
        },
        headers=headers,
    )
    assert protected.status_code == 200
    blocked = client.post(
        "/api/storage/delete-mark",
        json={"repo": "library/busybox", "tag": "v1.0.0", "reason": "cleanup"},
        headers=headers,
    )
    assert blocked.status_code == 409

    import panel.main as panel_main

    run_id = panel_main.db_execute(
        "INSERT INTO sync_runs(reason, status, only_source, started_at, ended_at, total) VALUES (?, ?, ?, ?, ?, ?)",
        ("manual", "completed", None, "2024-01-04T00:00:00+00:00", "2024-01-04T00:00:00+00:00", 3),
    )
    for tag, ended_at in [
        ("3", "2024-01-04T00:00:00+00:00"),
        ("2", "2024-01-03T00:00:00+00:00"),
        ("v1.0.0", "2024-01-02T00:00:00+00:00"),
    ]:
        panel_main.db_execute(
            """
            INSERT INTO sync_run_items(run_id, source, target, status, new_digest, started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                f"docker.io/library/busybox:{tag}",
                f"localhost:5000/library/busybox:{tag}",
                "success",
                f"sha256:{tag}",
                ended_at,
                ended_at,
            ),
        )

    policy_response = client.post(
        "/api/retention-policies",
        json={"id": "keep-one", "name": "Keep one", "repo_pattern": "library/busybox", "keep_last": 1},
        headers=headers,
    )
    assert policy_response.status_code == 200
    dry_run = client.post("/api/retention-policies/keep-one/dry-run", json={}, headers=headers).json()
    assert [item["tag"] for item in dry_run["candidates"]] == ["2"]
    assert [item["tag"] for item in dry_run["skipped_protected"]] == ["v1.0.0"]

    applied = client.post("/api/retention-policies/keep-one/apply", json={}, headers=headers).json()
    assert applied["marked"] == ["library/busybox:2"]
    storage = client.get("/api/storage").json()
    assert storage["deletion_marks"][0]["tag"] == "2"


def test_backup_restore_guide_and_verify(panel_app):
    client, _, _, _ = panel_app
    guide = client.get("/api/backup-restore-guide").json()
    assert "CREDENTIALS_SECRET_KEY" in guide["required_items"]
    assert any("/v2/" in item for item in guide["tls_entry"].values())

    response = client.post(
        "/api/backup-restore/verify",
        json={"require_credentials_secret": True},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
