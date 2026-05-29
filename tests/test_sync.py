import importlib
import json
from pathlib import Path


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
    sync_main.save_state({"docker.io/library/busybox:latest": "sha256:abc"})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "docker.io/library/busybox:latest": "sha256:abc"
    }


def test_valid_mirrors_skips_bad_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "data" / "sync-state.json"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    mirrors = sync_main.valid_mirrors(
        {
            "mirrors": [
                {"source": "docker.io/library/busybox:latest", "target": "localhost:5000/library/busybox:latest"},
                {"source": "missing-target"},
                "bad",
            ]
        }
    )

    assert mirrors == [
        {
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
            "registry": "local",
            "group": "default",
            "project": "default",
            "environment": "local",
            "namespace": "library",
            "source_credential_id": "",
            "target_credential_id": "",
        }
    ]


def test_missing_sync_config_uses_default_busybox(tmp_path, monkeypatch):
    config_path = tmp_path / "config" / "mirrors.yml"
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    config = sync_main.load_config()

    assert config["mirrors"][0]["source"] == "docker.io/library/busybox:latest"
    assert config["settings"]["registry_url"] == "http://registry:5000"
    assert not config_path.exists()


def test_valid_mirrors_keeps_v4_group_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    mirrors = sync_main.valid_mirrors(
        {
            "mirror_groups": [
                {
                    "id": "prod-app",
                    "project": "app",
                    "environment": "prod",
                    "namespace": "library",
                    "registry": "prod",
                }
            ],
            "mirrors": [
                {
                    "source": "docker.io/library/busybox:latest",
                    "target": "registry.example.com/library/busybox:latest",
                    "group": "prod-app",
                }
            ],
        }
    )

    assert mirrors[0]["registry"] == "prod"
    assert mirrors[0]["group"] == "prod-app"
    assert mirrors[0]["project"] == "app"
    assert mirrors[0]["environment"] == "prod"
    assert mirrors[0]["source_credential_id"] == ""


def test_skopeo_copy_command_rewrites_local_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.setenv("SYNC_TARGET_REGISTRY", "registry:5000")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    copy_target = sync_main.resolve_copy_target("localhost:5000/library/busybox:latest")
    cmd = sync_main.build_skopeo_copy_command("docker.io/library/busybox:latest", copy_target)

    assert copy_target == "registry:5000/library/busybox:latest"
    assert "copy" in cmd
    assert "--all" in cmd
    assert "docker://docker.io/library/busybox:latest" in cmd
    assert "docker://registry:5000/library/busybox:latest" in cmd


def test_sync_run_persists_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    run_id = sync_main.create_run("test")
    item_id = sync_main.create_run_item(
        run_id,
        "docker.io/library/busybox:latest",
        "localhost:5000/library/busybox:latest",
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
        json.dumps({"reason": "retry-run", "sources": ["docker.io/library/busybox:latest", ""]}),
        encoding="utf-8",
    )

    assert sync_main.parse_trigger() == ("retry-run", ["docker.io/library/busybox:latest"])


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
        "docker.io/library/busybox:latest",
        "localhost:5000/library/busybox:latest",
        retry_count=2,
    )

    assert ok is True
    assert error == ""
    assert copy_target == "registry:5000/library/busybox:latest"
    assert len(calls) == 3
    assert sleeps == [3, 6]


def test_credentials_match_authfile_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.setenv("CREDENTIALS_SECRET_KEY", "unit-secret-key")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    encrypted = sync_main.credential_fernet().encrypt(b"top-secret").decode("ascii")
    credentials = [
        {
            "id": "dockerhub",
            "registry_host": "docker.io",
            "username": "alice",
            "encrypted_secret": encrypted,
            "scope": "source",
        },
        {
            "id": "local-push",
            "registry_host": "registry:5000",
            "username": "publisher",
            "encrypted_secret": encrypted,
            "scope": "target",
        },
    ]

    host_default = sync_main.find_credential("docker.io/library/busybox:latest", "source", "", credentials)
    target_override = sync_main.find_credential("registry:5000/library/busybox:latest", "target", "local-push", credentials)
    assert host_default["id"] == "dockerhub"
    assert target_override["id"] == "local-push"

    authfile = sync_main.write_temp_authfile(host_default, target_override)
    payload = json.loads(Path(authfile).read_text(encoding="utf-8"))
    assert sorted(payload["auths"]) == ["docker.io", "registry:5000"]
    assert payload["auths"]["docker.io"]["password"] == "top-secret"
    assert "--authfile <authfile>" in sync_main.redact_command(["skopeo", "copy", "--authfile", authfile])
    sync_main.remove_temp_authfile(authfile)
    assert not Path(authfile).exists()


def test_credentials_missing_key_fails_without_secret_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.delenv("CREDENTIALS_SECRET_KEY", raising=False)

    import sync.sync as sync_main

    importlib.reload(sync_main)
    try:
        sync_main.decrypt_credential_secret("not-a-valid-token")
    except ValueError as exc:
        assert "CREDENTIALS_SECRET_KEY" in str(exc)
        assert "not-a-valid-token" not in str(exc)
    else:
        raise AssertionError("missing CREDENTIALS_SECRET_KEY should fail")


def test_sync_blocks_protected_release_tag_before_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    run_id = sync_main.create_run("scheduled")
    result = sync_main.process_mirror(
        run_id,
        {
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:v1.0.0",
            "environment": "local",
        },
        {},
        retry_count=0,
    )

    assert result == "failed"
    with sync_main.connect_db() as conn:
        row = conn.execute("SELECT step, error FROM sync_run_items ORDER BY id DESC LIMIT 1").fetchone()
    assert row["step"] == "protection"
    assert "release_tag" in row["error"]


def test_sync_records_tag_written_audit_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "data" / "sync-state.json"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.setenv("SYNC_TARGET_REGISTRY", "registry:5000")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    monkeypatch.setattr(sync_main, "load_credentials", lambda: [])
    monkeypatch.setattr(sync_main, "inspect_remote_digest", lambda image, authfile="": ("sha256:new", ""))
    monkeypatch.setattr(sync_main, "copy_image", lambda source, target, retry_count=0, authfile="": (True, "registry:5000/library/busybox:latest", ""))
    run_id = sync_main.create_run("scheduled")
    state = {}

    result = sync_main.process_mirror(
        run_id,
        {
            "source": "docker.io/library/busybox:latest",
            "target": "localhost:5000/library/busybox:latest",
            "environment": "local",
        },
        state,
        retry_count=0,
    )

    assert result == "updated"
    assert state["docker.io/library/busybox:latest"] == "sha256:new"
    with sync_main.connect_db() as conn:
        row = conn.execute("SELECT action, resource_id, detail FROM audit_logs WHERE action = 'tag_written'").fetchone()
    assert row["resource_id"] == "library/busybox:latest"
    assert "sha256:new" in row["detail"]


def test_scheduled_policy_runs_due_push_and_updates_status(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "data" / "sync-state.json"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'mirror-registry.db'}")
    monkeypatch.setenv("SYNC_TARGET_REGISTRY", "registry:5000")

    import sync.sync as sync_main

    importlib.reload(sync_main)
    sync_main.db_write(
        """
        INSERT INTO scheduled_push_policies(
            id, name, source, target, cron, enabled, allow_latest, next_run_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "nightly-plan",
            "Nightly plan",
            "docker.io/library/busybox:latest",
            "localhost:5000/library/busybox:nightly",
            "*/30 * * * *",
            1,
            0,
            "2024-01-01T00:00:00+00:00",
            sync_main.now_iso(),
            sync_main.now_iso(),
        ),
    )
    monkeypatch.setattr(sync_main, "load_credentials", lambda: [])
    monkeypatch.setattr(sync_main, "inspect_remote_digest", lambda image, authfile="": ("sha256:nightly", ""))
    monkeypatch.setattr(sync_main, "copy_image", lambda source, target, retry_count=0, authfile="": (True, "registry:5000/library/busybox:nightly", ""))

    sync_main.check_scheduled_policies()

    with sync_main.connect_db() as conn:
        policy = conn.execute("SELECT last_run_at, next_run_at, last_error FROM scheduled_push_policies WHERE id = ?", ("nightly-plan",)).fetchone()
        run = conn.execute("SELECT reason, status, updated FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert policy["last_run_at"]
    assert policy["next_run_at"]
    assert policy["last_error"] == ""
    assert run["reason"] == "scheduled-policy:nightly-plan"
    assert run["status"] == "completed"
    assert run["updated"] == 1
