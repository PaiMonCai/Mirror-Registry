import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def panel_app(tmp_path, monkeypatch):
    return make_panel_client(tmp_path, monkeypatch)


def make_panel_client(
    tmp_path,
    monkeypatch,
    credentials_secret_key: str | None = "unit-secret-key",
    seed_config: bool = True,
    login: bool = True,
):
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
    if seed_config:
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
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-password")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "604800")
    if credentials_secret_key is None:
        monkeypatch.delenv("CREDENTIALS_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("CREDENTIALS_SECRET_KEY", credentials_secret_key)

    import panel.main as panel_main

    importlib.reload(panel_main)
    panel_main.ensure_admin_user()
    client = TestClient(panel_main.app)
    if login:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
        assert response.status_code == 200
    return client, config_path, state_path, trigger_path


def test_panel_auth_login_logout_and_me(tmp_path, monkeypatch):
    client, _, _, _ = make_panel_client(tmp_path, monkeypatch, login=False)

    anonymous = client.get("/api/auth/me")
    assert anonymous.status_code == 401

    failed = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert failed.status_code == 401

    login_response = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
    assert login_response.status_code == 200
    assert login_response.json()["user"]["username"] == "admin"
    assert client.get("/api/auth/me").json()["authenticated"] is True
    assert client.get("/api/status").status_code == 200

    logout_response = client.post("/api/auth/logout")
    assert logout_response.status_code == 200
    assert client.get("/api/status").status_code == 401


def test_unauthenticated_api_requires_login(tmp_path, monkeypatch):
    client, _, _, _ = make_panel_client(tmp_path, monkeypatch, login=False)

    assert client.get("/api/status").status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
        },
    )
    assert response.status_code == 401


def test_bearer_token_remains_automation_compatible(tmp_path, monkeypatch):
    client, _, _, _ = make_panel_client(tmp_path, monkeypatch, login=False)
    headers = {"Authorization": "Bearer test-token"}

    assert client.get("/api/status", headers=headers).status_code == 200
    response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
        },
        headers=headers,
    )
    assert response.status_code == 200


def test_session_expiry_requires_login_again(tmp_path, monkeypatch):
    client, _, _, _ = make_panel_client(tmp_path, monkeypatch)

    import panel.main as panel_main

    panel_main.db_execute("UPDATE sessions SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))

    assert client.get("/api/status").status_code == 401


def test_login_audit_redacts_secret_values(tmp_path, monkeypatch):
    client, _, _, _ = make_panel_client(tmp_path, monkeypatch, login=False)

    client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
    assert client.post("/api/auth/logout").status_code == 200

    audit = client.get("/api/audit-logs", headers={"Authorization": "Bearer test-token"}).json()
    audit_text = json.dumps(audit, ensure_ascii=False)
    assert "login_failed" in audit_text
    assert "logout" in audit_text
    assert "wrong-password" not in audit_text
    assert "admin-password" not in audit_text
    assert "mirror_registry_session" not in audit_text


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


def test_missing_config_file_bootstraps_default_config(tmp_path, monkeypatch):
    client, config_path, _, _ = make_panel_client(tmp_path, monkeypatch)
    config_path.unlink()

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    content = config_path.read_text(encoding="utf-8")
    assert "docker.io/library/busybox:latest" in content
    assert "registry_url: http://registry:5000" in content


def test_startup_bootstraps_default_config(tmp_path, monkeypatch):
    client, config_path, _, _ = make_panel_client(tmp_path, monkeypatch, seed_config=False)

    with client:
        assert config_path.exists()
        content = config_path.read_text(encoding="utf-8")

    assert "docker.io/library/busybox:latest" in content
    assert "registry_url: http://registry:5000" in content


def test_write_routes_accept_authenticated_session(panel_app):
    client, _, _, _ = panel_app

    response = client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
        },
    )

    assert response.status_code == 200


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


def test_mirror_discovery_dry_run_from_compose_does_not_write_config(panel_app):
    client, config_path, _, _ = panel_app
    before = config_path.read_text(encoding="utf-8")
    payload = {
        "source_type": "compose",
        "target_registry": "localhost:5000",
        "content": """
services:
  web:
    image: nginx:1.27
  api:
    image: ghcr.io/example/api:v2
  bad:
    image: redis
""",
    }

    response = client.post("/api/mirrors/discover", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["extracted"] == 3
    assert data["summary"]["importable"] == 2
    assert data["summary"]["invalid"] == 1
    assert data["items"][0]["source"] == "docker.io/library/nginx:1.27"
    assert data["items"][0]["target"] == "localhost:5000/library/nginx:1.27"
    assert data["items"][2]["action"] == "missing_tag"
    assert config_path.read_text(encoding="utf-8") == before


def test_mirror_discovery_imports_kubernetes_images_and_can_trigger_sync(panel_app):
    client, config_path, _, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "source_type": "kubernetes",
        "target_registry": "localhost:5000",
        "mode": "missing_only",
        "trigger_sync": True,
        "content": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  template:
    spec:
      initContainers:
        - name: migrate
          image: ghcr.io/example/migrate:v1
      containers:
        - name: api
          image: ghcr.io/example/api:v2
        - name: sidecar
          image: busybox:1.36
""",
    }

    response = client.post("/api/mirrors/discover/import", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["imported"] == 3
    content = config_path.read_text(encoding="utf-8")
    assert "ghcr.io/example/migrate:v1" in content
    assert "ghcr.io/example/api:v2" in content
    assert "docker.io/library/busybox:1.36" in content
    trigger = json.loads(trigger_path.read_text(encoding="utf-8"))
    assert trigger["reason"] == "discover-import"
    assert sorted(trigger["sources"]) == [
        "docker.io/library/busybox:1.36",
        "ghcr.io/example/api:v2",
        "ghcr.io/example/migrate:v1",
    ]
    audit = client.get("/api/audit-logs").json()
    assert any(item["action"] == "discover_import" for item in audit)


def test_mirror_discovery_text_detects_existing_sources_and_replace_mode(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}
    client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/nginx:1.27",
            "target": "localhost:5000/library/nginx:1.27",
        },
        headers=headers,
    )
    payload = {
        "source_type": "text",
        "target_registry": "localhost:5000",
        "mode": "missing_only",
        "content": "nginx:1.27\npostgres:16\n",
    }

    dry_run = client.post("/api/mirrors/discover", json=payload).json()

    assert dry_run["summary"]["existing_source"] == 1
    assert dry_run["summary"]["new"] == 1
    assert [item["action"] for item in dry_run["items"]] == ["existing_source", "new"]
    replace = client.post(
        "/api/mirrors/discover",
        json={**payload, "mode": "replace"},
        headers=headers,
    ).json()
    assert replace["items"][0]["importable"] is True


def test_mirror_preflight_reports_protection_and_does_not_mutate_state(panel_app):
    client, _, state_path, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}
    state_path.write_text(json.dumps({"docker.io/library/busybox:v1.0.0": "sha256:old"}), encoding="utf-8")
    client.post(
        "/api/tag-protection",
        json={"id": "release-tags", "name": "Release tags", "repo_pattern": "library/*", "tag_pattern": "v*", "environment": "*"},
        headers=headers,
    )

    response = client.post(
        "/api/mirrors/preflight",
        json={
            "source": "docker.io/library/busybox:v1.0.0",
            "target": "localhost:5000/library/busybox:v1.0.0",
            "environment": "prod",
        },
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["status"] == "error"
    assert any(item["name"] == "保护规则" and item["status"] == "error" for item in data["checks"])
    assert any(item["name"] == "远程探测" and item["status"] == "warn" for item in data["checks"])
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"docker.io/library/busybox:v1.0.0": "sha256:old"}
    assert not trigger_path.exists()
    assert client.get("/api/sync-runs").json() == []


def test_mirror_preflight_uses_explicit_credentials_without_secret_leak(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}
    create = client.post(
        "/api/credentials",
        json={
            "id": "dockerhub",
            "name": "Docker Hub",
            "registry_host": "docker.io",
            "username": "alice",
            "secret": "top-secret",
            "scope": "source",
        },
        headers=headers,
    )
    assert create.status_code == 200

    ok = client.post(
        "/api/mirrors/preflight",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:nightly",
            "source_credential_id": "dockerhub",
        },
        headers=headers,
    ).json()

    assert ok["summary"]["status"] == "warn"
    assert any(item["name"] == "source 凭据" and item["status"] == "ok" for item in ok["checks"])
    assert "top-secret" not in json.dumps(ok, ensure_ascii=False)

    bad = client.post(
        "/api/mirrors/preflight",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:nightly",
            "target_credential_id": "dockerhub",
        },
        headers=headers,
    ).json()
    assert bad["summary"]["status"] == "error"
    assert any(item["name"] == "target 凭据" and item["status"] == "error" for item in bad["checks"])
    assert "top-secret" not in json.dumps(client.get("/api/audit-logs").json(), ensure_ascii=False)


def test_mirror_preflight_batch_defaults_to_config_and_remote_probe(panel_app, monkeypatch):
    client, _, state_path, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}
    client.post(
        "/api/mirrors",
        json={
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:nightly",
        },
        headers=headers,
    )

    import panel.main as panel_main

    class FakeResponse:
        def __init__(self, status_code=200, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, auth=None):
            if "/manifests/" in url:
                assert headers["Accept"] == panel_main.MANIFEST_ACCEPT
                return FakeResponse(200, {"Docker-Content-Digest": "sha256:new"})
            assert url.endswith("/v2/")
            return FakeResponse(200)

    monkeypatch.setattr(panel_main.httpx, "AsyncClient", FakeAsyncClient)

    response = client.post("/api/mirrors/preflight/batch", json={"check_remote": True}, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == {"total": 1, "ok": 0, "warn": 1, "error": 0}
    checks = data["items"][0]["checks"]
    assert any(item["name"] == "上游镜像" and item["status"] == "ok" for item in checks)
    assert any(item["name"] == "目标 Registry" and item["status"] == "ok" for item in checks)
    assert not state_path.exists()
    assert not trigger_path.exists()


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
    client, _, _, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}
    guide = client.get("/api/backup-restore-guide").json()
    assert "CREDENTIALS_SECRET_KEY" in guide["required_items"]
    assert any("/v2/" in item for item in guide["tls_entry"].values())
    assert "package_manifest" in guide

    response = client.post(
        "/api/backup-restore/verify",
        json={"require_credentials_secret": True},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    manifest = client.get("/api/backup-restore/package-manifest").json()
    assert manifest["commands"]["drill"].endswith("scripts\\restore-drill.ps1")
    assert any(item["name"] == "credentials_secret" and item["secret"] for item in manifest["required_items"])

    drill = client.post(
        "/api/backup-restore/drill",
        json={"require_credentials_secret": True, "verify_registry_sample": False},
        headers=headers,
    ).json()
    assert drill["ok"] is True
    assert drill["readonly"] is True
    assert drill["summary"]["status"] == "warn"
    assert any(item["name"] == "Registry 样本" and item["status"] == "warn" for item in drill["checks"])
    assert "unit-secret-key" not in json.dumps(drill, ensure_ascii=False)
    assert not trigger_path.exists()
    assert any(item["action"] == "drill" and item["resource_type"] == "backup_restore" for item in client.get("/api/audit-logs").json())


def test_schedules_default_disabled_and_trigger_policy_run(panel_app):
    client, _, _, trigger_path = panel_app
    headers = {"Authorization": "Bearer test-token"}

    disabled_latest = client.post(
        "/api/schedules",
        json={
            "id": "latest-plan",
            "name": "Latest plan",
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
            "cron": "0 18 * * *",
        },
        headers=headers,
    )
    assert disabled_latest.status_code == 200
    assert disabled_latest.json()["schedule"]["enabled"] is False
    blocked_run = client.post("/api/schedules/latest-plan/run", json={}, headers=headers)
    assert blocked_run.status_code == 409

    enabled = client.post(
        "/api/schedules",
        json={
            "id": "nightly-plan",
            "name": "Nightly plan",
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:nightly",
            "cron": "*/30 * * * *",
            "enabled": True,
        },
        headers=headers,
    )
    assert enabled.status_code == 200
    schedule = enabled.json()["schedule"]
    assert schedule["enabled"] is True
    assert schedule["next_run_at"]

    run = client.post("/api/schedules/nightly-plan/run", json={}, headers=headers)
    assert run.status_code == 200
    trigger = json.loads(trigger_path.read_text(encoding="utf-8"))
    assert trigger["reason"] == "scheduled-policy:nightly-plan"
    audit = client.get("/api/audit-logs").json()
    assert any(item["resource_type"] == "scheduled_push_policy" and item["action"] == "run" for item in audit)


def test_manifest_stats_deduplicate_shared_blobs(panel_app):
    import panel.main as panel_main

    child_a = {
        "config": {"digest": "sha256:config-a", "size": 10},
        "layers": [{"digest": "sha256:shared", "size": 100}, {"digest": "sha256:a", "size": 50}],
    }
    child_b = {
        "config": {"digest": "sha256:config-b", "size": 12},
        "layers": [{"digest": "sha256:shared", "size": 100}, {"digest": "sha256:b", "size": 60}],
    }
    manifest_list = {
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": "sha256:linux-amd64", "size": 1, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:linux-arm64", "size": 1, "platform": {"os": "linux", "architecture": "arm64"}},
        ],
    }

    stats = panel_main.compute_manifest_stats(
        manifest_list,
        {"sha256:linux-amd64": child_a, "sha256:linux-arm64": child_b},
    )

    assert stats["logical_size_bytes"] == 332
    assert stats["deduplicated_size_bytes"] == 232
    assert stats["shared_blob_count"] == 1
    assert [item["platform"]["architecture"] for item in stats["platforms"]] == ["amd64", "arm64"]


def test_storage_returns_marks_and_cached_stats_when_registry_unavailable(panel_app, monkeypatch):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    import panel.main as panel_main

    panel_main.upsert_storage_stat(
        "library/busybox",
        "latest",
        "sha256:manifest",
        {
            "logical_size_bytes": 110,
            "deduplicated_size_bytes": 110,
            "shared_blob_count": 0,
            "platforms": [],
            "blobs": [{"digest": "sha256:layer", "size": 110}],
        },
    )
    client.post(
        "/api/storage/delete-mark",
        json={"repo": "library/busybox", "tag": "latest", "reason": "manual"},
        headers=headers,
    )

    async def fake_list_registry_images():
        raise panel_main.HTTPException(502, "registry down")

    monkeypatch.setattr(panel_main, "list_registry_images", fake_list_registry_images)
    storage = client.get("/api/storage").json()

    assert storage["registry_error"] == "registry down"
    assert storage["stats_cached"] is True
    assert storage["deletion_marks"][0]["repo"] == "library/busybox"


def test_ops_summary_explains_recent_failures_and_risk_flags(panel_app):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    import panel.main as panel_main

    run_id = panel_main.db_execute(
        "INSERT INTO sync_runs(reason, status, only_source, started_at, ended_at, failed, total) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("manual", "failed", None, panel_main.now_iso(), panel_main.now_iso(), 1, 1),
    )
    panel_main.db_execute(
        """
        INSERT INTO sync_run_items(run_id, source, target, copy_target, status, step, error, started_at, ended_at, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "docker.io/library/missing:1.0.0",
            "localhost:5000/library/missing:1.0.0",
            "registry:5000/library/missing:1.0.0",
            "failed",
            "inspect",
            "manifest unknown: requested image not found",
            panel_main.now_iso(),
            panel_main.now_iso(),
            1234,
        ),
    )
    client.post(
        "/api/storage/delete-mark",
        json={"repo": "library/busybox", "tag": "old", "reason": "cleanup"},
        headers=headers,
    )

    response = client.get("/api/ops/summary")

    assert response.status_code == 200
    data = response.json()
    assert data["health"] == "error"
    assert "latest_run_failed" in data["reasons"]
    assert "pending_deletion_marks" in data["reasons"]
    assert "default_panel_token" not in data["reasons"]
    assert data["storage"]["deletion_marks"] == 1
    assert data["sync"]["latest_run"]["id"] == run_id
    failure = data["sync"]["recent_failures"][0]
    assert failure["source"] == "docker.io/library/missing:1.0.0"
    assert failure["explanation"]["category"] == "manifest"
    assert "tag" in failure["explanation"]["suggestion"]


def test_diagnostic_bundle_redacts_secrets_and_includes_ops_context(panel_app, monkeypatch):
    client, _, _, _ = panel_app
    headers = {"Authorization": "Bearer test-token"}

    import panel.main as panel_main

    async def fake_run_diagnostics():
        return {
            "status": "warn",
            "checks": [
                {
                    "name": "Auth probe",
                    "status": "warn",
                    "message": "Bearer test-token password=plain secret=plain",
                    "suggestion": "check postgresql://mirror:top-secret@db:5432/mirror",
                }
            ],
        }

    monkeypatch.setattr(panel_main, "run_diagnostics", fake_run_diagnostics)
    assert client.post(
        "/api/credentials",
        json={
            "id": "dockerhub",
            "name": "Docker Hub",
            "registry_host": "docker.io",
            "username": "alice",
            "secret": "top-secret",
            "scope": "both",
        },
        headers=headers,
    ).status_code == 200
    panel_main.audit_log(
        "probe",
        "release",
        "v1.2.3",
        {"Authorization": "Bearer test-token", "url": "postgresql://mirror:top-secret@db:5432/mirror"},
    )

    response = client.get("/api/ops/diagnostic-bundle")

    assert response.status_code == 200
    bundle = response.json()
    assert "summary" in bundle
    assert "config_summary" in bundle
    assert "diagnostics" in bundle
    assert "recent_runs" in bundle
    assert "events" in bundle
    assert "upgrade_guide" in bundle
    bundle_text = json.dumps(bundle, ensure_ascii=False)
    assert "top-secret" not in bundle_text
    assert "unit-secret-key" not in bundle_text
    assert "test-token" not in bundle_text
    assert "encrypted_secret" not in bundle_text
    assert "Authorization" not in bundle_text
    assert "Bearer <redacted>" in bundle_text
    assert "postgresql://<redacted>@db:5432/mirror" in bundle_text


def test_upgrade_guide_and_release_check_script_are_available(panel_app):
    client, _, _, _ = panel_app

    guide = client.get("/api/ops/upgrade-guide").json()

    assert "CREDENTIALS_SECRET_KEY" in guide["environment_variables"]
    assert "mirror-registry-storage:/var/lib/registry" in guide["volumes"]
    assert "python scripts\\verify.py" in guide["commands"]
    release_script = (Path(__file__).resolve().parents[1] / "scripts" / "release-check.ps1").read_text(encoding="utf-8")
    for snippet in ["Version", "ImageTag", "SmokeResultPath", "CHANGELOG.md", "latest", "Release checklist failed"]:
        assert snippet in release_script
