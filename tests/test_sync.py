import importlib
import json


def test_state_round_trip_is_atomic(tmp_path, monkeypatch):
    state_path = tmp_path / "data" / "sync-state.json"
    log_path = tmp_path / "data" / "sync.log"
    config_path = tmp_path / "config" / "mirrors.yml"
    trigger_path = tmp_path / "data" / ".trigger"

    monkeypatch.setenv("STATE_PATH", str(state_path))
    monkeypatch.setenv("LOG_PATH", str(log_path))
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("TRIGGER_PATH", str(trigger_path))

    import sync.sync as sync_main

    importlib.reload(sync_main)
    sync_main.save_state({"docker.io/library/nginx:latest": "sha256:abc"})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "docker.io/library/nginx:latest": "sha256:abc"
    }


def test_valid_mirrors_skips_bad_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "data" / "sync.log"))

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
