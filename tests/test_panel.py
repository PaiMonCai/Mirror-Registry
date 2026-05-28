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
