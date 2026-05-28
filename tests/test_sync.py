import importlib
import json


def test_state_round_trip_is_atomic(tmp_path, monkeypatch):
    state_path = tmp_path / "data" / "sync-state.json"
    log_path = tmp_path / "data" / "sync.log"
    config_path = tmp_path / "config" / "mirrors.yml"
    trigger_path = tmp_path / "data" / ".trigger"
    db_path = tmp_path / "data" / "mirror-registry.db"

    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("LOG_PATH", str(log_path))
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("TRIGGER_PATH", str(trigger_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    sync_main.save_state({"docker.io/library/nginx:latest": "sha256:abc"})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "docker.io/library/nginx:latest": "sha256:abc"
    }


def test_valid_mirrors_skips_bad_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    mirrors = sync_main.valid_mirrors(
        {
            "mirrors": [
                {"source": "docker.io/library/nginx:latest", "target": "localhost:5000/library/nginx:latest"},
                {"source": "missing-target"},
                "bad",
            ]
        }
    )

    assert mirrors == [
        {
            "source": "docker.io/library/nginx:latest",
            "target": "localhost:5000/library/nginx:latest",
        }
    ]


def test_skopeo_copy_command_rewrites_local_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.setenv("SYNC_TARGET_REGISTRY", "registry:5000")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    copy_target = sync_main.resolve_copy_target("localhost:5000/library/nginx:latest")
    cmd = sync_main.build_skopeo_copy_command("docker.io/library/nginx:latest", copy_target)

    assert copy_target == "registry:5000/library/nginx:latest"
    assert "copy" in cmd
    assert "--all" in cmd
    assert "docker://docker.io/library/nginx:latest" in cmd
    assert "docker://registry:5000/library/nginx:latest" in cmd


def test_sync_run_persists_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    run_id = sync_main.create_run("test")
    item_id = sync_main.create_run_item(
        run_id,
        "docker.io/library/nginx:latest",
        "localhost:5000/library/nginx:latest",
        None,
    )
    sync_main.update_run_item(item_id, "success", new_digest="sha256:abc", step="copy")
    sync_main.update_run(run_id, "completed", 1, 1, 0, 0, "ok")

    with sync_main.connect_db() as conn:
        row = conn.execute("SELECT status, updated FROM sync_runs WHERE id = ?", (run_id,)).fetchone()

    assert row["status"] == "completed"
    assert row["updated"] == 1


def test_parse_trigger_accepts_multiple_sources(tmp_path, monkeypatch):
    trigger_path = tmp_path / "data" / ".trigger"
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("TRIGGER_PATH", str(trigger_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    trigger_path.write_text(
        json.dumps({"reason": "retry-run", "sources": ["docker.io/library/nginx:latest", ""]}),
        encoding="utf-8",
    )

    assert sync_main.parse_trigger() == ("retry-run", ["docker.io/library/nginx:latest"])


def test_copy_image_uses_exponential_retry_and_target_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.setenv("SYNC_TARGET_REGISTRY", "registry:5000")
    monkeypatch.setenv("SYNC_RETRY_BACKOFF_SECONDS", "3")
    calls = []
    sleeps = []

    import sync.sync as sync_main

    importlib.reload(sync_main)

    def fake_run_command(step_name, cmd, timeout=sync_main.COMMAND_TIMEOUT_SECONDS):
        calls.append((step_name, cmd, timeout))
        return (len(calls) >= 3, "" if len(calls) >= 3 else "temporary")

    monkeypatch.setattr(sync_main, "run_command", fake_run_command)
    monkeypatch.setattr(sync_main.time, "sleep", lambda seconds: sleeps.append(seconds))

    ok, copy_target, error = sync_main.copy_image(
        "docker.io/library/nginx:latest",
        "localhost:5000/library/nginx:latest",
        retry_count=2,
    )

    assert ok is True
    assert error == ""
    assert copy_target == "registry:5000/library/nginx:latest"
    assert len(calls) == 3
    assert sleeps == [3, 6]
