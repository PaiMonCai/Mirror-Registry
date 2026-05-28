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
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from sqlalchemy import create_engine, text
except ImportError:  # pragma: no cover - exercised only when external DB deps are absent
    create_engine = None
    text = None

app = FastAPI(title="Mirror Registry Panel")

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/mirrors.yml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/sync-state.json"))
LOG_PATH = Path(os.getenv("LOG_PATH", "/data/sync.log"))
TRIGGER_PATH = Path(os.getenv("TRIGGER_PATH", "/data/.trigger"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/mirror-registry.db")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:5000").rstrip("/")
REGISTRY_STORAGE_PATH = Path(os.getenv("REGISTRY_STORAGE_PATH", "/data/registry"))
PANEL_TOKEN = os.getenv("PANEL_TOKEN", "change-me")
APP_VERSION = os.getenv("APP_VERSION", "v4")
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
    registry: str = Field(default="local", min_length=1, max_length=64)
    group: str = Field(default="default", min_length=1, max_length=64)
    project: str = Field(default="default", min_length=1, max_length=64)
    environment: str = Field(default="local", min_length=1, max_length=64)
    namespace: str = Field(default="library", min_length=1, max_length=128)


class MirrorImportIn(BaseModel):
    mirrors: list[MirrorIn] = Field(default_factory=list, max_length=500)
    replace: bool = False
    registries: list[dict] = Field(default_factory=list, max_length=50)
    mirror_groups: list[dict] = Field(default_factory=list, max_length=100)


class RegistryIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=500)
    copy_host: str | None = Field(default=None, max_length=255)
    storage_path: str | None = Field(default=None, max_length=500)


class MirrorGroupIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    project: str = Field(default="default", min_length=1, max_length=64)
    environment: str = Field(default="local", min_length=1, max_length=64)
    namespace: str = Field(default="library", min_length=1, max_length=128)
    registry: str = Field(default="local", min_length=1, max_length=64)


class SettingsIn(BaseModel):
    check_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    sync_concurrency: int | None = Field(default=None, ge=1, le=16)
    sync_retry_count: int | None = Field(default=None, ge=0, le=10)
    notify_webhook_url: str | None = Field(default=None, max_length=1000)
    database_url: str | None = Field(default=None, max_length=1000)
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
ENGINE = None


SQLITE_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

POSTGRES_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS mirrors (
        source VARCHAR(255) PRIMARY KEY,
        target VARCHAR(255) NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_digest TEXT,
        updated_at VARCHAR(64) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key VARCHAR(255) PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at VARCHAR(64) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        reason VARCHAR(255) NOT NULL,
        status VARCHAR(64) NOT NULL,
        only_source TEXT,
        started_at VARCHAR(64) NOT NULL,
        ended_at VARCHAR(64),
        total INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_run_items (
        id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        run_id INTEGER NOT NULL,
        source VARCHAR(255) NOT NULL,
        target VARCHAR(255) NOT NULL,
        copy_target TEXT,
        status VARCHAR(64) NOT NULL,
        old_digest TEXT,
        new_digest TEXT,
        step TEXT,
        error TEXT,
        started_at VARCHAR(64) NOT NULL,
        ended_at VARCHAR(64),
        duration_ms INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS log_events (
        id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        created_at VARCHAR(64) NOT NULL,
        level VARCHAR(64) NOT NULL,
        run_id INTEGER,
        source TEXT,
        target TEXT,
        message TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_state (
        key VARCHAR(255) PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at VARCHAR(64) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deletion_marks (
        id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        repo VARCHAR(255) NOT NULL,
        tag VARCHAR(128) NOT NULL,
        reason TEXT,
        created_at VARCHAR(64) NOT NULL,
        UNIQUE(repo, tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        created_at VARCHAR(64) NOT NULL,
        actor VARCHAR(128) NOT NULL,
        action VARCHAR(128) NOT NULL,
        resource_type VARCHAR(128) NOT NULL,
        resource_id VARCHAR(255) NOT NULL,
        detail TEXT NOT NULL
    )
    """,
]

MYSQL_SCHEMA_STATEMENTS = [
    statement.replace("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", "INTEGER PRIMARY KEY AUTO_INCREMENT")
    .replace("key VARCHAR(255) PRIMARY KEY", "`key` VARCHAR(255) PRIMARY KEY")
    for statement in POSTGRES_SCHEMA_STATEMENTS
]


def connect_db() -> sqlite3.Connection:
    if database_backend(DATABASE_URL) != "sqlite":
        raise RuntimeError("connect_db is only used for the default SQLite backend")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SQLITE_SCHEMA)
    conn.commit()


def external_engine():
    global ENGINE
    if ENGINE is not None:
        return ENGINE
    if create_engine is None or text is None:
        raise HTTPException(500, "外部数据库需要安装 SQLAlchemy 和对应 PostgreSQL/MySQL 驱动")
    ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    backend = database_backend(DATABASE_URL)
    statements = MYSQL_SCHEMA_STATEMENTS if backend == "mysql" else POSTGRES_SCHEMA_STATEMENTS
    with ENGINE.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return ENGINE


def bind_sql(sql: str, params: tuple) -> tuple[str, dict]:
    bound = {}
    converted = sql
    for index, value in enumerate(params):
        name = f"p{index}"
        converted = converted.replace("?", f":{name}", 1)
        bound[name] = value
    return converted, bound


def mysql_compatible_sql(sql: str) -> str:
    converted = sql
    converted = converted.replace("settings(key,", "settings(`key`,")
    converted = converted.replace("runtime_state(key,", "runtime_state(`key`,")
    converted = converted.replace("SELECT key,", "SELECT `key`,")
    converted = converted.replace("WHERE key =", "WHERE `key` =")
    converted = converted.replace(
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = VALUES(updated_at)",
    )
    converted = converted.replace(
        "ON CONFLICT(source) DO UPDATE SET\n            target = excluded.target,\n            last_digest = COALESCE(excluded.last_digest, mirrors.last_digest),\n            updated_at = excluded.updated_at",
        "ON DUPLICATE KEY UPDATE target = VALUES(target), last_digest = COALESCE(VALUES(last_digest), last_digest), updated_at = VALUES(updated_at)",
    )
    converted = converted.replace(
        "ON CONFLICT(repo, tag) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at",
        "ON DUPLICATE KEY UPDATE reason = VALUES(reason), created_at = VALUES(created_at)",
    )
    return converted


def db_rows(sql: str, params: tuple = ()) -> list[dict]:
    if database_backend(DATABASE_URL) != "sqlite":
        try:
            engine = external_engine()
            if database_backend(DATABASE_URL) == "mysql":
                sql = mysql_compatible_sql(sql)
            converted, bound = bind_sql(sql, params)
            with engine.begin() as conn:
                result = conn.execute(text(converted), bound)
                return [dict(row._mapping) for row in result.fetchall()]
        except Exception as exc:
            raise HTTPException(500, f"外部数据库读取失败: {exc}") from exc
    try:
        with connect_db() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        raise HTTPException(500, f"数据库读取失败: {exc}") from exc


def db_one(sql: str, params: tuple = ()) -> dict | None:
    rows = db_rows(sql, params)
    return rows[0] if rows else None


def db_execute(sql: str, params: tuple = ()) -> int:
    if database_backend(DATABASE_URL) != "sqlite":
        try:
            engine = external_engine()
            if database_backend(DATABASE_URL) == "mysql":
                sql = mysql_compatible_sql(sql)
            converted, bound = bind_sql(sql, params)
            with engine.begin() as conn:
                result = conn.execute(text(converted), bound)
                lastrowid = int(getattr(result, "lastrowid", 0) or 0)
                if not lastrowid and sql.lstrip().upper().startswith("INSERT"):
                    backend = database_backend(DATABASE_URL)
                    if backend == "postgresql":
                        lastrowid = int(conn.execute(text("SELECT LASTVAL()")).scalar() or 0)
                    elif backend == "mysql":
                        lastrowid = int(conn.execute(text("SELECT LAST_INSERT_ID()")).scalar() or 0)
                return lastrowid
        except Exception as exc:
            raise HTTPException(500, f"外部数据库写入失败: {exc}") from exc
    try:
        with connect_db() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or 0)
    except sqlite3.Error as exc:
        raise HTTPException(500, f"数据库写入失败: {exc}") from exc


def audit_log(action: str, resource_type: str, resource_id: str, detail: dict | None = None, actor: str = "panel") -> None:
    db_execute(
        """
        INSERT INTO audit_logs(created_at, actor, action, resource_type, resource_id, detail)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), actor, action, resource_type, resource_id, json.dumps(detail or {}, ensure_ascii=False)),
    )


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
    data.setdefault("registries", [])
    data.setdefault("mirror_groups", [])
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


def validate_slug(value: str, field_name: str) -> str:
    slug = value.strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", slug):
        raise HTTPException(400, f"{field_name} 只能包含字母、数字、点、下划线和短横线")
    return slug


def validate_registry_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "registry url 必须是 http:// 或 https:// 地址")
    return url


def normalize_registry(item: dict) -> dict:
    registry_id = validate_slug(str(item.get("id") or item.get("name") or "local"), "registry id")
    url = validate_registry_url(str(item.get("url") or REGISTRY_URL))
    return {
        "id": registry_id,
        "name": str(item.get("name") or registry_id).strip() or registry_id,
        "url": url,
        "copy_host": str(item.get("copy_host") or "").strip(),
        "storage_path": str(item.get("storage_path") or "").strip(),
    }


def default_registry() -> dict:
    return {
        "id": "local",
        "name": "Local Registry",
        "url": REGISTRY_URL,
        "copy_host": "registry:5000",
        "storage_path": str(REGISTRY_STORAGE_PATH),
    }


def registry_map(config: dict | None = None) -> dict[str, dict]:
    config = config or load_config()
    registries = [default_registry()]
    for item in config.get("registries", []):
        if isinstance(item, dict):
            try:
                normalized = normalize_registry(item)
            except HTTPException:
                continue
            registries.append(normalized)
    return {item["id"]: item for item in registries}


def normalize_group(item: dict) -> dict:
    group_id = validate_slug(str(item.get("id") or item.get("name") or "default"), "group id")
    return {
        "id": group_id,
        "name": str(item.get("name") or group_id).strip() or group_id,
        "project": validate_slug(str(item.get("project") or "default"), "project"),
        "environment": validate_slug(str(item.get("environment") or "local"), "environment"),
        "namespace": str(item.get("namespace") or "library").strip() or "library",
        "registry": validate_slug(str(item.get("registry") or "local"), "registry"),
    }


def group_map(config: dict | None = None) -> dict[str, dict]:
    config = config or load_config()
    groups = [
        {
            "id": "default",
            "name": "Default",
            "project": "default",
            "environment": "local",
            "namespace": "library",
            "registry": "local",
        }
    ]
    for item in config.get("mirror_groups", []):
        if isinstance(item, dict):
            try:
                groups.append(normalize_group(item))
            except HTTPException:
                continue
    return {item["id"]: item for item in groups}


def normalize_mirror(item: dict, groups: dict[str, dict] | None = None) -> dict:
    source = validate_image_ref(str(item.get("source", "")), "source")
    target = validate_image_ref(str(item.get("target", "")), "target")
    group_id = validate_slug(str(item.get("group") or item.get("group_id") or "default"), "group")
    groups = groups or group_map()
    group = groups.get(group_id, groups["default"])
    registry = validate_slug(str(item.get("registry") or item.get("registry_id") or group.get("registry") or "local"), "registry")
    return {
        "source": source,
        "target": target,
        "registry": registry,
        "group": group_id,
        "project": validate_slug(str(item.get("project") or group.get("project") or "default"), "project"),
        "environment": validate_slug(str(item.get("environment") or group.get("environment") or "local"), "environment"),
        "namespace": str(item.get("namespace") or group.get("namespace") or "library").strip() or "library",
    }


def valid_mirrors(config: dict) -> list[dict]:
    result = []
    groups = group_map(config)
    for item in config.get("mirrors", []):
        if not isinstance(item, dict):
            continue
        try:
            result.append(normalize_mirror(item, groups))
        except HTTPException:
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if source and target:
                result.append(
                    {
                        "source": source,
                        "target": target,
                        "registry": "local",
                        "group": "default",
                        "project": "default",
                        "environment": "local",
                        "namespace": "library",
                    }
                )
    return result


def settings_with_defaults() -> dict:
    settings = load_config().get("settings", {})
    database_url = str(settings.get("database_url") or DATABASE_URL)
    return {
        "check_interval_minutes": int(settings.get("check_interval_minutes", 30)),
        "registry_url": str(settings.get("registry_url") or REGISTRY_URL).rstrip("/"),
        "sync_concurrency": int(settings.get("sync_concurrency", 2)),
        "sync_retry_count": int(settings.get("sync_retry_count", 2)),
        "notify_webhook_configured": bool(str(settings.get("notify_webhook_url") or os.getenv("NOTIFY_WEBHOOK_URL", "")).strip()),
        "notify_webhook_url_masked": mask_url(str(settings.get("notify_webhook_url") or os.getenv("NOTIFY_WEBHOOK_URL", "")).strip()),
        "database_url": database_url,
        "database_backend": database_backend(database_url),
        "external_database_configured": not database_url.startswith("sqlite:"),
    }


def database_backend(database_url: str = DATABASE_URL) -> str:
    if database_url.startswith("sqlite:"):
        return "sqlite"
    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return "postgresql"
    if database_url.startswith("mysql://") or database_url.startswith("mysql+pymysql://"):
        return "mysql"
    return "unknown"


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


def summarize_platform(config: dict) -> dict:
    mirrors = valid_mirrors(config)
    registries = registry_map(config)
    groups = group_map(config)
    projects = sorted({mirror["project"] for mirror in mirrors})
    environments = sorted({mirror["environment"] for mirror in mirrors})
    namespaces = sorted({mirror["namespace"] for mirror in mirrors})
    return {
        "registries": list(registries.values()),
        "mirror_groups": list(groups.values()),
        "projects": projects,
        "environments": environments,
        "namespaces": namespaces,
        "mirror_count": len(mirrors),
        "external_database": external_database_guide(),
        "deployment_modes": deployment_modes(),
    }


def grouped_mirror_summary(config: dict) -> list[dict]:
    mirrors = valid_mirrors(config)
    groups = group_map(config)
    counters: dict[tuple[str, str, str, str, str], dict] = {}
    for mirror in mirrors:
        key = (
            mirror["project"],
            mirror["environment"],
            mirror["namespace"],
            mirror["registry"],
            mirror["group"],
        )
        item = counters.setdefault(
            key,
            {
                "project": mirror["project"],
                "environment": mirror["environment"],
                "namespace": mirror["namespace"],
                "registry": mirror["registry"],
                "group": mirror["group"],
                "group_name": groups.get(mirror["group"], {}).get("name", mirror["group"]),
                "mirror_count": 0,
            },
        )
        item["mirror_count"] += 1
    return sorted(counters.values(), key=lambda item: (item["project"], item["environment"], item["namespace"], item["group"]))


def deployment_modes() -> list[dict]:
    return [
        {
            "id": "single-node",
            "status": "default",
            "description": "默认单机部署：registry、panel、sync 通过 docker compose 在同一台服务器运行。",
        },
        {
            "id": "multi-instance",
            "status": "planned",
            "description": "多实例部署可共享外部数据库和配置卷，但需要避免多个 sync 同时处理同一镜像组。",
        },
        {
            "id": "remote-worker",
            "status": "planned",
            "description": "远程 worker 可按镜像组消费任务，适合跨网络或多环境同步。",
        },
        {
            "id": "queued-sync",
            "status": "planned",
            "description": "队列化同步可将触发、调度和执行解耦，但不作为默认部署路径。",
        },
    ]


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
        "registries": len(registry_map(config)),
        "mirror_groups": len(group_map(config)),
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
        "database_backend": settings["database_backend"],
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
                "registry": mirror["registry"],
                "group": mirror["group"],
                "project": mirror["project"],
                "environment": mirror["environment"],
                "namespace": mirror["namespace"],
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
    return {
        "version": 2,
        "exported_at": now_iso(),
        "registries": list(registry_map(config).values()),
        "mirror_groups": list(group_map(config).values()),
        "mirrors": valid_mirrors(config),
        "settings": safe_settings,
    }


@app.post("/api/mirrors/import", dependencies=[Depends(require_write_token)])
def import_mirrors(body: MirrorImportIn):
    imported: list[dict] = []
    seen_sources: set[str] = set()
    for item in body.mirrors:
        source = validate_image_ref(item.source, "source")
        if source in seen_sources:
            continue
        target = validate_image_ref(item.target, "target")
        seen_sources.add(source)
        imported.append(normalize_mirror(item.model_dump()))
    if not imported:
        raise HTTPException(400, "导入内容没有有效镜像")

    config = load_config()
    if body.registries:
        registry_by_id = registry_map(config)
        for item in body.registries:
            normalized = normalize_registry(item)
            if normalized["id"] != "local":
                registry_by_id[normalized["id"]] = normalized
        config["registries"] = [item for key, item in registry_by_id.items() if key != "local"]
    if body.mirror_groups:
        groups_by_id = group_map(config)
        for item in body.mirror_groups:
            normalized = normalize_group(item)
            if normalized["id"] != "default":
                groups_by_id[normalized["id"]] = normalized
        config["mirror_groups"] = [item for key, item in groups_by_id.items() if key != "default"]
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
    audit_log("import", "mirrors", "bulk", {"imported": len(imported), "replace": body.replace})
    return {"ok": True, "imported": len(imported), "total": len(mirrors), "replace": body.replace}


@app.post("/api/mirrors", dependencies=[Depends(require_write_token)])
def add_mirror(body: MirrorIn):
    mirror = normalize_mirror(body.model_dump())
    source = mirror["source"]
    target = mirror["target"]
    config = load_config()
    mirrors = valid_mirrors(config)
    if any(mirror["source"] == source for mirror in mirrors):
        raise HTTPException(400, "该 source 已存在")
    mirrors.append(mirror)
    config["mirrors"] = mirrors
    save_config(config)
    upsert_mirror_db(source, target)
    audit_log("create", "mirror", source, mirror)
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
    audit_log("delete", "mirror", removed["source"], removed)
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
    audit_log("reset_digest", "mirror", mirrors[index]["source"], mirrors[index])
    return {"ok": True}


@app.post("/api/mirrors/{index}/sync", dependencies=[Depends(require_write_token)])
def trigger_mirror_sync(index: int):
    config = load_config()
    mirrors = valid_mirrors(config)
    if index < 0 or index >= len(mirrors):
        raise HTTPException(404, "镜像不存在")
    write_trigger("manual-single", source=mirrors[index]["source"])
    audit_log("trigger_sync", "mirror", mirrors[index]["source"], mirrors[index])
    return {"ok": True, "message": "单镜像同步任务已触发，请稍后查看任务历史"}


@app.post("/api/sync", dependencies=[Depends(require_write_token)])
def trigger_sync():
    write_trigger("manual")
    audit_log("trigger_sync", "mirrors", "all", {})
    return {"ok": True, "message": "同步任务已触发，请稍后查看日志"}


@app.get("/api/settings")
def get_settings():
    return settings_with_defaults()


def get_registry_url() -> str:
    return settings_with_defaults()["registry_url"]


def config_database_url(config: dict | None = None) -> str:
    config = config or load_config()
    settings = config.get("settings", {})
    return str(settings.get("database_url") or DATABASE_URL)


def external_database_guide() -> dict:
    return {
        "default_backend": "sqlite",
        "supported_backends": ["sqlite", "postgresql", "mysql"],
        "current_backend": database_backend(config_database_url()),
        "current_configured": not config_database_url().startswith("sqlite:"),
        "notes": [
            "默认单机部署继续使用 sqlite:////data/mirror-registry.db。",
            "PostgreSQL/MySQL 通过 DATABASE_URL 或 settings.database_url 预留正式配置入口。",
            "切换外部数据库前先备份 /data/mirror-registry.db 和 config/mirrors.yml。",
            "多实例部署时建议把 panel 和 sync 指向同一个外部数据库，并避免多个 sync 同时执行相同镜像组。",
        ],
        "examples": {
            "postgresql": "postgresql://mirror:password@postgres:5432/mirror_registry",
            "mysql": "mysql://mirror:password@mysql:3306/mirror_registry",
        },
    }


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
    if body.database_url is not None:
        database_url = body.database_url.strip()
        if database_url:
            backend = database_backend(database_url)
            if backend == "unknown":
                raise HTTPException(400, "database_url 仅支持 sqlite/postgresql/mysql")
            settings["database_url"] = database_url
            changed["database_url"] = f"{backend}:***"
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
    audit_log("update", "settings", "sync", changed)
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
    audit_log("retry", "sync_run", str(run_id), {"sources": sources})
    return {"ok": True, "sources": sources, "count": len(sources)}


@app.post("/api/sync-run-items/{item_id}/retry", dependencies=[Depends(require_write_token)])
def retry_sync_run_item(item_id: int):
    item = db_one("SELECT source FROM sync_run_items WHERE id = ?", (item_id,))
    if not item:
        raise HTTPException(404, "同步任务明细不存在")
    write_trigger("retry-item", source=item["source"])
    audit_log("retry", "sync_run_item", str(item_id), {"source": item["source"]})
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

    external_db = external_database_guide()
    checks.append(
        diagnostic_item(
            "数据库后端",
            "ok" if external_db["current_backend"] in {"sqlite", "postgresql", "mysql"} else "warn",
            f"当前后端: {external_db['current_backend']}",
            "默认使用 SQLite；多实例部署可配置 PostgreSQL/MySQL DATABASE_URL",
            {"external_database_configured": external_db["current_configured"]},
        )
    )

    platform = summarize_platform(load_config())
    checks.append(
        diagnostic_item(
            "平台化配置",
            "ok",
            f"{len(platform['registries'])} 个 Registry，{len(platform['mirror_groups'])} 个镜像组",
            "单机部署仍可只使用默认 local/default 配置",
            {"projects": platform["projects"], "environments": platform["environments"]},
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
    audit_log("mark_delete", "image", f"{repo}:{tag}", {"reason": body.reason or ""})
    return {"ok": True, "id": mark_id, "repo": repo, "tag": tag}


@app.delete("/api/storage/delete-mark/{mark_id}", dependencies=[Depends(require_write_token)])
def unmark_image_for_delete(mark_id: int):
    db_execute("DELETE FROM deletion_marks WHERE id = ?", (mark_id,))
    audit_log("unmark_delete", "deletion_mark", str(mark_id), {})
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


@app.get("/api/platform")
def get_platform():
    config = load_config()
    return summarize_platform(config)


@app.get("/api/platform/groups")
def list_grouped_mirrors():
    return grouped_mirror_summary(load_config())


@app.get("/api/registries")
def list_registries():
    return list(registry_map(load_config()).values())


@app.post("/api/registries", dependencies=[Depends(require_write_token)])
def upsert_registry(body: RegistryIn):
    normalized = normalize_registry(body.model_dump())
    if normalized["id"] == "local":
        raise HTTPException(400, "local Registry 是内置默认项，不能覆盖")
    config = load_config()
    registries = registry_map(config)
    registries[normalized["id"]] = normalized
    config["registries"] = [item for key, item in registries.items() if key != "local"]
    save_config(config)
    audit_log("upsert", "registry", normalized["id"], normalized)
    return {"ok": True, "registry": normalized}


@app.delete("/api/registries/{registry_id}", dependencies=[Depends(require_write_token)])
def delete_registry(registry_id: str):
    clean_id = validate_slug(registry_id, "registry")
    if clean_id == "local":
        raise HTTPException(400, "local Registry 是内置默认项，不能删除")
    config = load_config()
    mirrors = valid_mirrors(config)
    if any(mirror["registry"] == clean_id for mirror in mirrors):
        raise HTTPException(400, "该 Registry 仍被镜像引用")
    registries = registry_map(config)
    registries.pop(clean_id, None)
    config["registries"] = [item for key, item in registries.items() if key != "local"]
    save_config(config)
    audit_log("delete", "registry", clean_id, {})
    return {"ok": True}


@app.get("/api/mirror-groups")
def list_mirror_groups():
    return list(group_map(load_config()).values())


@app.post("/api/mirror-groups", dependencies=[Depends(require_write_token)])
def upsert_mirror_group(body: MirrorGroupIn):
    normalized = normalize_group(body.model_dump())
    if normalized["id"] == "default":
        raise HTTPException(400, "default 镜像组是内置默认项，不能覆盖")
    registries = registry_map(load_config())
    if normalized["registry"] not in registries:
        raise HTTPException(400, "镜像组引用的 Registry 不存在")
    config = load_config()
    groups = group_map(config)
    groups[normalized["id"]] = normalized
    config["mirror_groups"] = [item for key, item in groups.items() if key != "default"]
    save_config(config)
    audit_log("upsert", "mirror_group", normalized["id"], normalized)
    return {"ok": True, "group": normalized}


@app.delete("/api/mirror-groups/{group_id}", dependencies=[Depends(require_write_token)])
def delete_mirror_group(group_id: str):
    clean_id = validate_slug(group_id, "group")
    if clean_id == "default":
        raise HTTPException(400, "default 镜像组是内置默认项，不能删除")
    config = load_config()
    mirrors = valid_mirrors(config)
    if any(mirror["group"] == clean_id for mirror in mirrors):
        raise HTTPException(400, "该镜像组仍被镜像引用")
    groups = group_map(config)
    groups.pop(clean_id, None)
    config["mirror_groups"] = [item for key, item in groups.items() if key != "default"]
    save_config(config)
    audit_log("delete", "mirror_group", clean_id, {})
    return {"ok": True}


@app.get("/api/audit-logs")
def list_audit_logs(limit: int = 100):
    bounded_limit = max(1, min(limit, 500))
    rows = db_rows(
        """
        SELECT id, created_at, actor, action, resource_type, resource_id, detail
        FROM audit_logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (bounded_limit,),
    )
    for row in rows:
        try:
            row["detail"] = json.loads(row.get("detail") or "{}")
        except json.JSONDecodeError:
            row["detail"] = {}
    return rows


@app.get("/api/deployment-modes")
def list_deployment_modes():
    return deployment_modes()


@app.get("/api/database-guide")
def get_database_guide():
    return external_database_guide()


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
