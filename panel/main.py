import asyncio
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="Mirror Registry Panel")

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/mirrors.yml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/sync-state.json"))
LOG_PATH = Path(os.getenv("LOG_PATH", "/data/sync.log"))
TRIGGER_PATH = Path(os.getenv("TRIGGER_PATH", "/data/.trigger"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/mirror-registry.db")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:5000").rstrip("/")
PANEL_TOKEN = os.getenv("PANEL_TOKEN", "change-me")

STATIC_DIR = Path(os.getenv("STATIC_DIR", "/panel/static"))
if not STATIC_DIR.exists():
    STATIC_DIR = Path(__file__).parent / "static"
IMAGE_REF_RE = re.compile(
    r"^(?=.{3,255}$)(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:(?:[._-][a-z0-9]+)+)?"
    r"(?:/[a-z0-9]+(?:(?:[._-][a-z0-9]+)+)?)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
)


class MirrorIn(BaseModel):
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)


class SettingsIn(BaseModel):
    check_interval_minutes: int = Field(ge=1, le=1440)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def database_path() -> Path:
    if DATABASE_URL.startswith("sqlite:///"):
        return Path(DATABASE_URL.removeprefix("sqlite:///"))
    return Path(DATABASE_URL)


DB_PATH = database_path()


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mirrors (
            source TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_digest TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            only_source TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_run_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            copy_target TEXT,
            status TEXT NOT NULL,
            old_digest TEXT,
            new_digest TEXT,
            step TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_ms INTEGER,
            FOREIGN KEY(run_id) REFERENCES sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS log_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            run_id INTEGER,
            source TEXT,
            target TEXT,
            message TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def db_rows(sql: str, params: tuple = ()) -> list[dict]:
    try:
        with connect_db() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        raise HTTPException(500, f"数据库读取失败: {exc}") from exc


def db_one(sql: str, params: tuple = ()) -> dict | None:
    rows = db_rows(sql, params)
    return rows[0] if rows else None


def db_execute(sql: str, params: tuple = ()) -> int:
    try:
        with connect_db() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or 0)
    except sqlite3.Error as exc:
        raise HTTPException(500, f"数据库写入失败: {exc}") from exc


def runtime_state() -> dict:
    rows = db_rows("SELECT key, value, updated_at FROM runtime_state")
    return {row["key"]: {"value": row["value"], "updated_at": row["updated_at"]} for row in rows}


def require_write_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = f"Bearer {PANEL_TOKEN}"
    if not PANEL_TOKEN or authorization != expected:
        raise HTTPException(401, "写操作需要有效访问令牌")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    ensure_parent(path)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        newline="\n",
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"mirrors": [], "settings": {"check_interval_minutes": 30}}
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data.setdefault("mirrors", [])
    data.setdefault("settings", {})
    return data


def save_config(config: dict) -> None:
    content = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    atomic_write_text(CONFIG_PATH, content)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"sync-state.json 无法解析: {exc}") from exc


def save_state(state: dict) -> None:
    atomic_write_text(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


def validate_image_ref(value: str, field_name: str) -> str:
    image = value.strip()
    if not IMAGE_REF_RE.match(image):
        raise HTTPException(400, f"{field_name} 必须是包含 tag 的镜像地址")
    return image


def valid_mirrors(config: dict) -> list[dict]:
    result = []
    for item in config.get("mirrors", []):
        source = str(item.get("source", "")).strip() if isinstance(item, dict) else ""
        target = str(item.get("target", "")).strip() if isinstance(item, dict) else ""
        if source and target:
            result.append({"source": source, "target": target})
    return result


def upsert_mirror_db(source: str, target: str, digest: str | None = None) -> None:
    db_execute(
        """
        INSERT INTO mirrors(source, target, enabled, last_digest, updated_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            target = excluded.target,
            last_digest = COALESCE(excluded.last_digest, mirrors.last_digest),
            updated_at = excluded.updated_at
        """,
        (source, target, digest, now_iso()),
    )


def delete_mirror_db(source: str) -> None:
    db_execute("DELETE FROM mirrors WHERE source = ?", (source,))


def write_trigger(reason: str, source: str | None = None) -> None:
    ensure_parent(TRIGGER_PATH)
    atomic_write_text(TRIGGER_PATH, json.dumps({"reason": reason, "source": source}, ensure_ascii=False))


def latest_run() -> dict | None:
    return db_one(
        """
        SELECT id, reason, status, started_at, ended_at, total, updated, skipped, failed, message
        FROM sync_runs
        ORDER BY id DESC
        LIMIT 1
        """
    )


@app.get("/api/status")
def get_status():
    config = load_config()
    state = load_state()
    mirrors = valid_mirrors(config)
    synced = sum(1 for mirror in mirrors if state.get(mirror["source"]))
    runtime = runtime_state()
    running = runtime.get("sync_running", {}).get("value") == "true"
    return {
        "total": len(mirrors),
        "synced": synced,
        "pending": len(mirrors) - synced,
        "interval": config.get("settings", {}).get("check_interval_minutes", 30),
        "is_syncing": running or TRIGGER_PATH.exists(),
        "auth_required": bool(PANEL_TOKEN),
        "using_default_token": PANEL_TOKEN == "change-me",
        "last_started_at": runtime.get("last_started_at", {}).get("value"),
        "last_finished_at": runtime.get("last_finished_at", {}).get("value"),
        "next_run_at": runtime.get("next_run_at", {}).get("value"),
        "sync_engine": runtime.get("sync_engine", {}).get("value", "skopeo"),
        "latest_run": latest_run(),
    }


@app.get("/api/mirrors")
def list_mirrors():
    config = load_config()
    state = load_state()
    result = []
    for index, mirror in enumerate(valid_mirrors(config)):
        digest = state.get(mirror["source"], "")
        db_mirror = db_one("SELECT last_digest FROM mirrors WHERE source = ?", (mirror["source"],))
        if not digest and db_mirror:
            digest = db_mirror.get("last_digest") or ""
        last_item = db_one(
            """
            SELECT status, step, error, ended_at
            FROM sync_run_items
            WHERE source = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (mirror["source"],),
        )
        short = (digest[7:19] + "...") if len(digest) > 19 and digest.startswith("sha256:") else digest
        result.append(
            {
                "index": index,
                "source": mirror["source"],
                "target": mirror["target"],
                "digest": short,
                "synced": bool(digest),
                "last_status": last_item.get("status") if last_item else "",
                "last_error": last_item.get("error") if last_item else "",
                "last_step": last_item.get("step") if last_item else "",
                "last_finished_at": last_item.get("ended_at") if last_item else "",
            }
        )
    return result


@app.post("/api/mirrors", dependencies=[Depends(require_write_token)])
def add_mirror(body: MirrorIn):
    source = validate_image_ref(body.source, "source")
    target = validate_image_ref(body.target, "target")
    config = load_config()
    mirrors = valid_mirrors(config)
    if any(mirror["source"] == source for mirror in mirrors):
        raise HTTPException(400, "该 source 已存在")
    mirrors.append({"source": source, "target": target})
    config["mirrors"] = mirrors
    save_config(config)
    upsert_mirror_db(source, target)
    return {"ok": True}


@app.delete("/api/mirrors/{index}", dependencies=[Depends(require_write_token)])
def delete_mirror(index: int):
    config = load_config()
    mirrors = valid_mirrors(config)
    if index < 0 or index >= len(mirrors):
        raise HTTPException(404, "镜像不存在")
    removed = mirrors.pop(index)
    config["mirrors"] = mirrors
    save_config(config)
    state = load_state()
    state.pop(removed["source"], None)
    save_state(state)
    delete_mirror_db(removed["source"])
    return {"ok": True}


@app.post("/api/mirrors/{index}/reset", dependencies=[Depends(require_write_token)])
def reset_mirror_digest(index: int):
    config = load_config()
    mirrors = valid_mirrors(config)
    if index < 0 or index >= len(mirrors):
        raise HTTPException(404, "镜像不存在")
    state = load_state()
    state.pop(mirrors[index]["source"], None)
    save_state(state)
    db_execute("UPDATE mirrors SET last_digest = NULL, updated_at = ? WHERE source = ?", (now_iso(), mirrors[index]["source"]))
    return {"ok": True}


@app.post("/api/mirrors/{index}/sync", dependencies=[Depends(require_write_token)])
def trigger_mirror_sync(index: int):
    config = load_config()
    mirrors = valid_mirrors(config)
    if index < 0 or index >= len(mirrors):
        raise HTTPException(404, "镜像不存在")
    write_trigger("manual-single", mirrors[index]["source"])
    return {"ok": True, "message": "单镜像同步任务已触发，请稍后查看任务历史"}


@app.post("/api/sync", dependencies=[Depends(require_write_token)])
def trigger_sync():
    write_trigger("manual")
    return {"ok": True, "message": "同步任务已触发，请稍后查看日志"}


@app.get("/api/settings")
def get_settings():
    config = load_config()
    settings = config.get("settings", {})
    return {"check_interval_minutes": settings.get("check_interval_minutes", 30)}


def get_registry_url() -> str:
    settings = load_config().get("settings", {})
    return str(settings.get("registry_url") or REGISTRY_URL).rstrip("/")


@app.put("/api/settings", dependencies=[Depends(require_write_token)])
def update_settings(body: SettingsIn):
    config = load_config()
    config.setdefault("settings", {})["check_interval_minutes"] = body.check_interval_minutes
    save_config(config)
    db_execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("check_interval_minutes", str(body.check_interval_minutes), now_iso()),
    )
    return {"ok": True, "message": "设置已保存，sync 服务下次读取配置后生效"}


@app.get("/api/logs")
def get_logs(lines: int = 150):
    bounded_lines = max(1, min(lines, 1000))
    if not LOG_PATH.exists():
        return {"lines": ["（暂无日志）"]}
    all_lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": all_lines[-bounded_lines:]}


@app.get("/api/events")
def list_events(limit: int = 100):
    bounded_limit = max(1, min(limit, 500))
    return db_rows(
        """
        SELECT id, created_at, level, run_id, source, target, message
        FROM log_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (bounded_limit,),
    )


@app.get("/api/sync-runs")
def list_sync_runs(limit: int = 30):
    bounded_limit = max(1, min(limit, 100))
    return db_rows(
        """
        SELECT id, reason, status, only_source, started_at, ended_at, total, updated, skipped, failed, message
        FROM sync_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (bounded_limit,),
    )


@app.get("/api/sync-runs/{run_id}")
def get_sync_run(run_id: int):
    run = db_one(
        """
        SELECT id, reason, status, only_source, started_at, ended_at, total, updated, skipped, failed, message
        FROM sync_runs
        WHERE id = ?
        """,
        (run_id,),
    )
    if not run:
        raise HTTPException(404, "同步任务不存在")
    items = db_rows(
        """
        SELECT id, source, target, copy_target, status, old_digest, new_digest, step, error, started_at, ended_at, duration_ms
        FROM sync_run_items
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    )
    run["items"] = items
    return run


@app.get("/api/diagnostics")
async def get_diagnostics():
    return await run_diagnostics()


@app.post("/api/diagnostics/run")
async def post_diagnostics():
    return await run_diagnostics()


def diagnostic_item(name: str, status: str, message: str, suggestion: str = "", details: dict | None = None) -> dict:
    return {
        "name": name,
        "status": status,
        "message": message,
        "suggestion": suggestion,
        "details": details or {},
    }


async def run_diagnostics() -> dict:
    checks = []
    registry_url = get_registry_url()

    started = datetime.now(timezone.utc)
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{registry_url}/v2/", timeout=5)
            if response.status_code in {200, 401}:
                checks.append(diagnostic_item("Registry API", "ok", f"{registry_url}/v2/ 可访问"))
            else:
                checks.append(
                    diagnostic_item(
                        "Registry API",
                        "error",
                        f"Registry 返回 HTTP {response.status_code}",
                        "检查 registry 服务和 config/mirrors.yml 中的 registry_url",
                    )
                )
        except httpx.HTTPError as exc:
            checks.append(
                diagnostic_item(
                    "Registry API",
                    "error",
                    f"无法连接 Registry: {exc}",
                    "确认 registry 容器正在运行，且 panel 能访问 registry:5000",
                )
            )

    try:
        config = load_config()
        checks.append(
            diagnostic_item(
                "配置文件",
                "ok",
                f"已加载 {len(valid_mirrors(config))} 条镜像配置",
                details={"path": str(CONFIG_PATH)},
            )
        )
    except Exception as exc:
        checks.append(diagnostic_item("配置文件", "error", f"读取配置失败: {exc}", "检查 config/mirrors.yml 格式"))

    config_parent = CONFIG_PATH.parent
    checks.append(
        diagnostic_item(
            "配置目录写入",
            "ok" if config_parent.exists() and os.access(config_parent, os.W_OK) else "error",
            f"{config_parent} {'可写' if config_parent.exists() and os.access(config_parent, os.W_OK) else '不可写'}",
            "确认 panel 容器挂载了 ./config:/config",
        )
    )

    data_parent = LOG_PATH.parent
    checks.append(
        diagnostic_item(
            "数据目录写入",
            "ok" if data_parent.exists() and os.access(data_parent, os.W_OK) else "error",
            f"{data_parent} {'可写' if data_parent.exists() and os.access(data_parent, os.W_OK) else '不可写'}",
            "确认 panel 和 sync 容器都挂载了 ./data:/data",
        )
    )

    try:
        with connect_db() as conn:
            conn.execute("SELECT 1")
        checks.append(diagnostic_item("SQLite", "ok", f"数据库可用: {DB_PATH}", details={"path": str(DB_PATH)}))
    except sqlite3.Error as exc:
        checks.append(diagnostic_item("SQLite", "error", f"数据库不可用: {exc}", "检查 /data 是否可写"))

    runtime = runtime_state()
    skopeo_available = runtime.get("skopeo_available", {}).get("value")
    last_heartbeat = runtime.get("last_heartbeat", {}).get("value")
    if skopeo_available == "true":
        checks.append(
            diagnostic_item(
                "Sync 依赖",
                "ok",
                f"sync 已上报 skopeo: {runtime.get('skopeo_path', {}).get('value', '')}",
                details={"last_heartbeat": last_heartbeat},
            )
        )
    elif last_heartbeat:
        checks.append(diagnostic_item("Sync 依赖", "error", "sync 已运行但未找到 skopeo", "重新构建并拉取最新 sync 镜像"))
    else:
        checks.append(diagnostic_item("Sync 依赖", "warn", "尚未收到 sync 心跳", "检查 sync 容器是否启动"))

    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    summary_status = "ok"
    if any(item["status"] == "error" for item in checks):
        summary_status = "error"
    elif any(item["status"] == "warn" for item in checks):
        summary_status = "warn"

    return {
        "status": summary_status,
        "checked_at": now_iso(),
        "duration_ms": elapsed_ms,
        "checks": checks,
        "runtime": {key: value["value"] for key, value in runtime.items()},
    }


@app.get("/api/images")
async def list_images():
    registry_url = get_registry_url()
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{registry_url}/v2/_catalog", timeout=5)
            response.raise_for_status()
            repos = response.json().get("repositories", [])
            tasks = [_get_tags(client, registry_url, repo) for repo in repos]
            return await asyncio.gather(*tasks)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"无法连接到 Registry: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(502, f"Registry 返回了无效 JSON: {exc}") from exc


async def _get_tags(client: httpx.AsyncClient, registry_url: str, repo: str) -> dict:
    try:
        response = await client.get(f"{registry_url}/v2/{repo}/tags/list", timeout=5)
        response.raise_for_status()
        tags = response.json().get("tags") or []
    except (httpx.HTTPError, ValueError):
        tags = []
    return {"repo": repo, "tags": tags}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
