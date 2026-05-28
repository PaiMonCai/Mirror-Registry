import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def panel_app(tmp_path, monkeypatch):
    config_path = tmp_path / "config" / "mirrors.yml"
    state_path = tmp_path / "data" / "sync-state.json"
    log_path = tmp_path / "data" / "sync.log"
    trigger_path = tmp_path / "data" / ".trigger"
    db_path = tmp_path / "data" / "mirror-registry.db"
    static_dir = tmp_path / "static"

    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
    config_path.parent.mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    config_path.write_text(
        "mirrors: []\nsettings:\n  check_interval_minutes: 30\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("LOG_PATH", str(log_path))
    monkeypatch.setenv("TRIGGER_PATH", str(trigger_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("STATIC_DIR", str(static_dir))
    monkeypatch.setenv("PANEL_TOKEN", "test-token")

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
            "source": "docker.io/library/nginx:latest",
            "target": "localhost:5000/library/nginx:latest",
        },
        headers=headers,
    ).status_code == 200

    mirrors = client.get("/api/mirrors").json()
    assert mirrors[0]["source"] == "docker.io/library/nginx:latest"
    assert "docker.io/library/nginx:latest" in config_path.read_text(encoding="utf-8")

    state_path.write_text(json.dumps({"docker.io/library/nginx:latest": "sha256:abc"}), encoding="utf-8")
    assert client.post("/api/mirrors/0/reset", headers=headers).status_code == 200
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}

    assert client.delete("/api/mirrors/0", headers=headers).status_code == 200
    assert client.get("/api/status").json()["total"] == 0


def test_write_routes_require_token(panel_app):
    client, _, _, _ = panel_app

    response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/nginx:latest",
            "target": "localhost:5000/library/nginx:latest",
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
            "source": "docker.io/library/nginx",
            "target": "localhost:5000/library/nginx:latest",
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
            "source": "docker.io/library/nginx:latest",
            "target": "localhost:5000/library/nginx:latest",
        },
        headers=headers,
    )
    response = client.post("/api/mirrors/0/sync", headers=headers)

    assert response.status_code == 200
    assert "docker.io/library/nginx:latest" in trigger_path.read_text(encoding="utf-8")


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
                "source": "docker.io/library/nginx:latest",
                "target": "localhost:5000/library/nginx:latest",
            }
        ],
        "replace": True,
    }

    response = client.post("/api/mirrors/import", json=payload, headers=headers)

    assert response.status_code == 200
    exported = client.get("/api/mirrors/export").json()
    assert exported["mirrors"][0]["source"] == "docker.io/library/nginx:latest"
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
            "docker.io/library/nginx:latest",
            "localhost:5000/library/nginx:latest",
            "failed",
            panel_main.now_iso(),
        ),
    )

    response = client.post(f"/api/sync-runs/{run_id}/retry", headers=headers)

    assert response.status_code == 200
    trigger = json.loads(trigger_path.read_text(encoding="utf-8"))
    assert trigger["reason"] == "retry-run"
    assert trigger["sources"] == ["docker.io/library/nginx:latest"]


def test_storage_delete_mark_and_security_guide(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    response = client.post(
        "/api/storage/delete-mark",
        json={"repo": "library/nginx", "tag": "latest", "reason": "cleanup"},
        headers=headers,
    )

    assert response.status_code == 200
    storage = client.get("/api/storage").json()
    assert storage["deletion_marks"][0]["repo"] == "library/nginx"
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
            "source": "docker.io/library/nginx:latest",
            "target": "registry.example.com/library/nginx:latest",
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
