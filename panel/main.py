import asyncio
import json
import os
import re
import shutil
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
REGISTRY_STORAGE_PATH = Path(os.getenv("REGISTRY_STORAGE_PATH", "/data/registry"))
PANEL_TOKEN = os.getenv("PANEL_TOKEN", "change-me")
APP_VERSION = os.getenv("APP_VERSION", "v3")
IMAGE_TAG = os.getenv("MIRROR_REGISTRY_IMAGE_TAG", "latest")

STATIC_DIR = Path(os.getenv("STATIC_DIR", "/panel/static"))
if not STATIC_DIR.exists():
    STATIC_DIR = Path(__file__).parent / "static"
IMAGE_REF_RE = re.compile(
    r"^(?=.{3,255}$)(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:(?:[._-][a-z0-9]+)+)?"
    r"(?:/[a-z0-9]+(?:(?:[._-][a-z0-9]+)+)?)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
)
REPO_RE = re.compile(r"^[a-z0-9]+(?:(?:[._-][a-z0-9]+)+)?(?:/[a-z0-9]+(?:(?:[._-][a-z0-9]+)+)?)*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class MirrorIn(BaseModel):
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)


class MirrorImportIn(BaseModel):
    mirrors: list[MirrorIn] = Field(default_factory=list, max_length=500)
    replace: bool = False


class SettingsIn(BaseModel):
    check_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    sync_concurrency: int | None = Field(default=None, ge=1, le=16)
    sync_retry_count: int | None = Field(default=None, ge=0, le=10)
    notify_webhook_url: str | None = Field(default=None, max_length=1000)
    clear_notify_webhook_url: bool = False


class StorageDeleteMarkIn(BaseModel):
    repo: str = Field(min_length=1, max_length=255)
    tag: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default="", max_length=500)


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

        CREATE TABLE IF NOT EXISTS deletion_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            tag TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(repo, tag)
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


def validate_repo_tag(repo: str, tag: str) -> tuple[str, str]:
    clean_repo = repo.strip()
    clean_tag = tag.strip()
    if not REPO_RE.match(clean_repo):
        raise HTTPException(400, "repo 格式不合法")
    if not TAG_RE.match(clean_tag):
        raise HTTPException(400, "tag 格式不合法")
    return clean_repo, clean_tag


def valid_mirrors(config: dict) -> list[dict]:
    result = []
    for item in config.get("mirrors", []):
        source = str(item.get("source", "")).strip() if isinstance(item, dict) else ""
        target = str(item.get("target", "")).strip() if isinstance(item, dict) else ""
        if source and target:
            result.append({"source": source, "target": target})
    return result


def settings_with_defaults() -> dict:
    settings = load_config().get("settings", {})
    return {
        "check_interval_minutes": int(settings.get("check_interval_minutes", 30)),
        "registry_url": str(settings.get("registry_url") or REGISTRY_URL).rstrip("/"),
        "sync_concurrency": int(settings.get("sync_concurrency", 2)),
        "sync_retry_count": int(settings.get("sync_retry_count", 2)),
        "notify_webhook_configured": bool(str(settings.get("notify_webhook_url") or os.getenv("NOTIFY_WEBHOOK_URL", "")).strip()),
        "notify_webhook_url_masked": mask_url(str(settings.get("notify_webhook_url") or os.getenv("NOTIFY_WEBHOOK_URL", "")).strip()),
    }


def mask_url(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 18:
        return "***"
    return f"{value[:12]}...{value[-6:]}"


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


def write_trigger(reason: str, source: str | None = None, sources: list[str] | None = None) -> None:
    payload: dict = {"reason": reason}
    clean_sources = [item.strip() for item in (sources or []) if item.strip()]
    if clean_sources:
        payload["sources"] = clean_sources
    elif source:
        payload["source"] = source.strip()
    ensure_parent(TRIGGER_PATH)
    atomic_write_text(TRIGGER_PATH, json.dumps(payload, ensure_ascii=False))


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
    settings = settings_with_defaults()
    state = load_state()
    mirrors = valid_mirrors(config)
    synced = sum(1 for mirror in mirrors if state.get(mirror["source"]))
    runtime = runtime_state()
    running = runtime.get("sync_running", {}).get("value") == "true"
    return {
        "total": len(mirrors),
        "synced": synced,
        "pending": len(mirrors) - synced,
        "interval": settings["check_interval_minutes"],
        "sync_concurrency": settings["sync_concurrency"],
        "sync_retry_count": settings["sync_retry_count"],
        "is_syncing": running or TRIGGER_PATH.exists(),
        "auth_required": bool(PANEL_TOKEN),
        "using_default_token": PANEL_TOKEN == "change-me",
        "last_started_at": runtime.get("last_started_at", {}).get("value"),
        "last_finished_at": runtime.get("last_finished_at", {}).get("value"),
        "next_run_at": runtime.get("next_run_at", {}).get("value"),
        "sync_engine": runtime.get("sync_engine", {}).get("value", "skopeo"),
        "disk_free_bytes": runtime.get("disk_free_bytes", {}).get("value"),
        "disk_low": runtime.get("disk_low", {}).get("value") == "true",
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


@app.get("/api/mirrors/export")
def export_mirrors():
    config = load_config()
    settings = settings_with_defaults()
    safe_settings = {
        "check_interval_minutes": settings["check_interval_minutes"],
        "registry_url": settings["registry_url"],
        "sync_concurrency": settings["sync_concurrency"],
        "sync_retry_count": settings["sync_retry_count"],
    }
    return {"version": 1, "exported_at": now_iso(), "mirrors": valid_mirrors(config), "settings": safe_settings}


@app.post("/api/mirrors/import", dependencies=[Depends(require_write_token)])
def import_mirrors(body: MirrorImportIn):
    imported: list[dict] = []
    seen_sources: set[str] = set()
    for item in body.mirrors:
        source = validate_image_ref(item.source, "source")
        target = validate_image_ref(item.target, "target")
        if source in seen_sources:
            continue
        seen_sources.add(source)
        imported.append({"source": source, "target": target})
    if not imported:
        raise HTTPException(400, "导入内容没有有效镜像")

    config = load_config()
    if body.replace:
        mirrors = imported
    else:
        mirrors_by_source = {mirror["source"]: mirror for mirror in valid_mirrors(config)}
        for mirror in imported:
            mirrors_by_source[mirror["source"]] = mirror
        mirrors = list(mirrors_by_source.values())
    config["mirrors"] = mirrors
    save_config(config)
    for mirror in imported:
        upsert_mirror_db(mirror["source"], mirror["target"])
    return {"ok": True, "imported": len(imported), "total": len(mirrors), "replace": body.replace}


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
    write_trigger("manual-single", source=mirrors[index]["source"])
    return {"ok": True, "message": "单镜像同步任务已触发，请稍后查看任务历史"}


@app.post("/api/sync", dependencies=[Depends(require_write_token)])
def trigger_sync():
    write_trigger("manual")
    return {"ok": True, "message": "同步任务已触发，请稍后查看日志"}


@app.get("/api/settings")
def get_settings():
    return settings_with_defaults()


def get_registry_url() -> str:
    return settings_with_defaults()["registry_url"]


def persist_setting(key: str, value: object) -> None:
    db_execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value), now_iso()),
    )


@app.put("/api/settings", dependencies=[Depends(require_write_token)])
def update_settings(body: SettingsIn):
    config = load_config()
    settings = config.setdefault("settings", {})
    changed: dict[str, object] = {}
    for key in ["check_interval_minutes", "sync_concurrency", "sync_retry_count"]:
        value = getattr(body, key)
        if value is not None:
            settings[key] = value
            changed[key] = value
    if body.clear_notify_webhook_url:
        settings.pop("notify_webhook_url", None)
        changed["notify_webhook_url"] = ""
    elif body.notify_webhook_url is not None:
        url = body.notify_webhook_url.strip()
        if url:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(400, "webhook URL 必须以 http:// 或 https:// 开头")
            settings["notify_webhook_url"] = url
            changed["notify_webhook_url"] = mask_url(url)
    save_config(config)
    for key, value in changed.items():
        persist_setting(key, value)
    return {"ok": True, "message": "设置已保存，sync 服务下次读取配置后生效", "changed": changed}


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


@app.post("/api/sync-runs/{run_id}/retry", dependencies=[Depends(require_write_token)])
def retry_sync_run(run_id: int):
    run = db_one("SELECT id FROM sync_runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(404, "同步任务不存在")
    rows = db_rows(
        """
        SELECT source
        FROM sync_run_items
        WHERE run_id = ? AND status = 'failed'
        ORDER BY id ASC
        """,
        (run_id,),
    )
    sources = list(dict.fromkeys(row["source"] for row in rows))
    if not sources:
        raise HTTPException(400, "该任务没有失败镜像可重试")
    write_trigger("retry-run", sources=sources)
    return {"ok": True, "sources": sources, "count": len(sources)}


@app.post("/api/sync-run-items/{item_id}/retry", dependencies=[Depends(require_write_token)])
def retry_sync_run_item(item_id: int):
    item = db_one("SELECT source FROM sync_run_items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404, "同步任务明细不存在")
    write_trigger("retry-item", source=item["source"])
    return {"ok": True, "source": item["source"]}


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
        usage = shutil.disk_usage(data_parent)
        checks.append(
            diagnostic_item(
                "磁盘空间",
                "warn" if usage.free < 2 * 1024 * 1024 * 1024 else "ok",
                f"剩余 {usage.free} / 总计 {usage.total} bytes",
                "磁盘不足时先清理删除标记镜像并执行 Registry garbage-collect",
                {"free_bytes": usage.free, "total_bytes": usage.total},
            )
        )
    except OSError as exc:
        checks.append(diagnostic_item("磁盘空间", "warn", f"无法读取磁盘空间: {exc}"))

    try:
        with connect_db() as conn:
            conn.execute("SELECT 1")
        checks.append(diagnostic_item("SQLite", "ok", f"数据库可用: {DB_PATH}", details={"path": str(DB_PATH)}))
    except sqlite3.Error as exc:
        checks.append(diagnostic_item("SQLite", "error", f"数据库不可用: {exc}", "检查 /data 是否可写"))

    settings = settings_with_defaults()
    checks.append(
        diagnostic_item(
            "同步策略",
            "ok",
            f"并发 {settings['sync_concurrency']}，最大重试 {settings['sync_retry_count']}，webhook {'已配置' if settings['notify_webhook_configured'] else '未配置'}",
            details={"notify_webhook_configured": settings["notify_webhook_configured"]},
        )
    )

    checks.append(
        diagnostic_item(
            "版本信息",
            "ok",
            f"app={APP_VERSION}, image_tag={IMAGE_TAG}",
            "如果这里不是预期版本，执行 docker compose pull 后重新创建服务",
            {"app_version": APP_VERSION, "image_tag": IMAGE_TAG},
        )
    )

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


async def list_registry_images() -> list[dict]:
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


@app.get("/api/images")
async def list_images():
    return await list_registry_images()


async def _get_tags(client: httpx.AsyncClient, registry_url: str, repo: str) -> dict:
    try:
        response = await client.get(f"{registry_url}/v2/{repo}/tags/list", timeout=5)
        response.raise_for_status()
        tags = response.json().get("tags") or []
    except (httpx.HTTPError, ValueError):
        tags = []
    return {"repo": repo, "tags": tags}


def directory_size(path: Path) -> int | None:
    if not path.exists():
        return None
    total = 0
    try:
        for file_path in path.rglob("*"):
            if file_path.is_file():
                total += file_path.stat().st_size
    except OSError:
        return None
    return total


def estimate_repo_size(repo: str) -> int | None:
    return directory_size(REGISTRY_STORAGE_PATH / "docker" / "registry" / "v2" / "repositories" / repo)


def gc_guide() -> dict:
    return {
        "summary": "删除标记只记录清理意图。真正释放空间需要先删除 Registry manifest，再停 registry 执行 garbage-collect。",
        "commands": [
            "docker compose pull",
            "docker compose stop registry",
            "docker compose run --rm registry registry garbage-collect /etc/docker/registry/config.yml",
            "docker compose up -d registry",
        ],
    }


@app.get("/api/storage")
async def get_storage():
    registry_error = ""
    try:
        images = await list_registry_images()
    except HTTPException as exc:
        registry_error = str(exc.detail)
        images = []
    marks = db_rows(
        """
        SELECT id, repo, tag, reason, created_at
        FROM deletion_marks
        ORDER BY id DESC
        """
    )
    mark_by_ref = {f"{mark['repo']}:{mark['tag']}": mark for mark in marks}
    enriched = []
    for image in images:
        repo = image["repo"]
        repo_size = estimate_repo_size(repo)
        enriched.append(
            {
                "repo": repo,
                "estimated_size_bytes": repo_size,
                "tags": [
                    {
                        "name": tag,
                        "marked_for_deletion": f"{repo}:{tag}" in mark_by_ref,
                        "deletion_mark": mark_by_ref.get(f"{repo}:{tag}"),
                    }
                    for tag in image.get("tags", [])
                ],
            }
        )
    total_storage = directory_size(REGISTRY_STORAGE_PATH)
    return {
        "images": enriched,
        "deletion_marks": marks,
        "registry_storage_path": str(REGISTRY_STORAGE_PATH),
        "estimated_total_bytes": total_storage,
        "registry_error": registry_error,
        "garbage_collection": gc_guide(),
    }


@app.post("/api/storage/delete-mark", dependencies=[Depends(require_write_token)])
def mark_image_for_delete(body: StorageDeleteMarkIn):
    repo, tag = validate_repo_tag(body.repo, body.tag)
    mark_id = db_execute(
        """
        INSERT INTO deletion_marks(repo, tag, reason, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(repo, tag) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at
        """,
        (repo, tag, body.reason or "", now_iso()),
    )
    return {"ok": True, "id": mark_id, "repo": repo, "tag": tag}


@app.delete("/api/storage/delete-mark/{mark_id}", dependencies=[Depends(require_write_token)])
def unmark_image_for_delete(mark_id: int):
    db_execute("DELETE FROM deletion_marks WHERE id = ?", (mark_id,))
    return {"ok": True}


@app.get("/api/security-guide")
def get_security_guide():
    return {
        "public_exposure_boundary": "不要把 8080 面板端口直接暴露到公网。PANEL_TOKEN 只保护写操作，不等于完整登录系统。",
        "recommended": [
            "通过 Nginx、Caddy 或 Traefik 放在内网入口后面。",
            "在反向代理层启用 Basic Auth 或单点登录，只允许可信 IP 访问。",
            "仍然保留强随机 PANEL_TOKEN，作为写接口的第二层保护。",
        ],
        "nginx_basic_auth": [
            "location / {",
            "  auth_basic \"Mirror Registry\";",
            "  auth_basic_user_file /etc/nginx/.htpasswd;",
            "  proxy_pass http://127.0.0.1:8080;",
            "}",
        ],
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
