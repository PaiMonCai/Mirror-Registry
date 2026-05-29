import json
import base64
import fnmatch
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from cryptography.fernet import Fernet, InvalidToken

from mirror_registry_core.config import default_config

try:
    from sqlalchemy import create_engine, text
except ImportError:  # pragma: no cover - exercised only when external DB deps are absent
    create_engine = None
    text = None

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/mirrors.yml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/sync-state.json"))
LOG_PATH = Path(os.getenv("LOG_PATH", "/data/sync.log"))
TRIGGER_PATH = Path(os.getenv("TRIGGER_PATH", "/data/.trigger"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/mirror-registry.db")
COMMAND_TIMEOUT_SECONDS = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "900"))
SYNC_ENGINE = os.getenv("SYNC_ENGINE", "skopeo")
APP_VERSION = os.getenv("APP_VERSION", "v4")
IMAGE_TAG = os.getenv("MIRROR_REGISTRY_IMAGE_TAG", "latest")
SYNC_RETRY_COUNT = int(os.getenv("SYNC_RETRY_COUNT", "2"))
SYNC_CONCURRENCY = int(os.getenv("SYNC_CONCURRENCY", "2"))
SYNC_RETRY_BACKOFF_SECONDS = int(os.getenv("SYNC_RETRY_BACKOFF_SECONDS", "2"))
DISK_LOW_BYTES = int(os.getenv("DISK_LOW_BYTES", str(2 * 1024 * 1024 * 1024)))
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
NOTIFY_DEDUPE_SECONDS = int(os.getenv("NOTIFY_DEDUPE_SECONDS", "1800"))
SKOPEO_COPY_ALL = os.getenv("SKOPEO_COPY_ALL", "1") != "0"
SKOPEO_SRC_TLS_VERIFY = os.getenv("SKOPEO_SRC_TLS_VERIFY", "true").lower()
SKOPEO_DEST_TLS_VERIFY = os.getenv("SKOPEO_DEST_TLS_VERIFY", "false").lower()
SKOPEO_AUTHFILE = os.getenv("SKOPEO_AUTHFILE", "").strip()
SYNC_TARGET_REGISTRY = os.getenv("SYNC_TARGET_REGISTRY", "registry:5000").strip()
CREDENTIALS_SECRET_KEY = os.getenv("CREDENTIALS_SECRET_KEY", "")
LOCAL_REGISTRY_ALIASES = [
    item.strip()
    for item in os.getenv("LOCAL_REGISTRY_ALIASES", "localhost:5000,127.0.0.1:5000").split(",")
    if item.strip()
]


def database_backend(database_url: str = DATABASE_URL) -> str:
    if database_url.startswith("sqlite:"):
        return "sqlite"
    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return "postgresql"
    if database_url.startswith("mysql://") or database_url.startswith("mysql+pymysql://"):
        return "mysql"
    return "unknown"


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

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    sources TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER,
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
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

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    registry_host TEXT NOT NULL,
    username TEXT NOT NULL,
    encrypted_secret TEXT NOT NULL,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tag_protection_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_pattern TEXT NOT NULL,
    tag_pattern TEXT NOT NULL,
    environment TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_push_policies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    allow_latest INTEGER NOT NULL DEFAULT 0,
    source_credential_id TEXT,
    target_credential_id TEXT,
    last_run_at TEXT,
    next_run_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    CREATE TABLE IF NOT EXISTS sync_queue (
        id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        reason VARCHAR(255) NOT NULL,
        sources TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        status VARCHAR(64) NOT NULL,
        dedupe_key VARCHAR(255) NOT NULL,
        scheduled_at VARCHAR(64) NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        run_id INTEGER,
        message TEXT,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL,
        started_at VARCHAR(64),
        finished_at VARCHAR(64)
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
    """
    CREATE TABLE IF NOT EXISTS credentials (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        registry_host VARCHAR(255) NOT NULL,
        username VARCHAR(255) NOT NULL,
        encrypted_secret TEXT NOT NULL,
        scope VARCHAR(16) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tag_protection_rules (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        repo_pattern VARCHAR(255) NOT NULL,
        tag_pattern VARCHAR(128) NOT NULL,
        environment VARCHAR(64) NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        reason TEXT,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_push_policies (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        source VARCHAR(255) NOT NULL,
        target VARCHAR(255) NOT NULL,
        cron VARCHAR(120) NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        allow_latest INTEGER NOT NULL DEFAULT 0,
        source_credential_id VARCHAR(64),
        target_credential_id VARCHAR(64),
        last_run_at VARCHAR(64),
        next_run_at VARCHAR(64),
        last_error TEXT,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL
    )
    """,
]
MYSQL_SCHEMA_STATEMENTS = [
    statement.replace("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", "INTEGER PRIMARY KEY AUTO_INCREMENT")
    .replace("key VARCHAR(255) PRIMARY KEY", "`key` VARCHAR(255) PRIMARY KEY")
    for statement in POSTGRES_SCHEMA_STATEMENTS
]

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("sync")
logger.setLevel(logging.INFO)
logger.handlers.clear()

fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(fmt)
logger.addHandler(stream_handler)

file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
file_handler.setFormatter(fmt)
logger.addHandler(file_handler)

sync_lock = threading.Lock()
queue_lock = threading.Lock()
state_lock = threading.Lock()
target_locks_guard = threading.Lock()
target_locks: dict[str, threading.Lock] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def database_path() -> Path:
    if DATABASE_URL.startswith("sqlite:///"):
        return Path(DATABASE_URL.removeprefix("sqlite:///"))
    return Path(DATABASE_URL)


DB_PATH = database_path()
ENGINE = None


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
        raise RuntimeError("外部数据库需要安装 SQLAlchemy 和对应 PostgreSQL/MySQL 驱动")
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


def db_write(sql: str, params: tuple = ()) -> int:
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
            logger.warning("外部数据库写入失败: %s", exc)
            return 0
    try:
        with connect_db() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or 0)
    except sqlite3.Error as exc:
        logger.warning("SQLite 写入失败: %s", exc)
        return 0


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
            logger.warning("外部数据库读取失败: %s", exc)
            return []
    try:
        with connect_db() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error as exc:
        logger.warning("SQLite 读取失败: %s", exc)
        return []


def db_one(sql: str, params: tuple = ()) -> dict | None:
    rows = db_rows(sql, params)
    return rows[0] if rows else None


def runtime_value(key: str, default: str = "") -> str:
    if database_backend(DATABASE_URL) != "sqlite":
        try:
            engine = external_engine()
            sql = "SELECT value FROM runtime_state WHERE key = :key"
            if database_backend(DATABASE_URL) == "mysql":
                sql = "SELECT value FROM runtime_state WHERE `key` = :key"
            with engine.begin() as conn:
                row = conn.execute(text(sql), {"key": key}).fetchone()
                return str(row._mapping["value"]) if row else default
        except Exception as exc:
            logger.warning("外部数据库读取运行状态失败: %s", exc)
            return default
    try:
        with connect_db() as conn:
            row = conn.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default
    except sqlite3.Error as exc:
        logger.warning("SQLite 读取运行状态失败: %s", exc)
        return default


def set_runtime_state(key: str, value: str) -> None:
    db_write(
        """
        INSERT INTO runtime_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def audit_log(action: str, resource_type: str, resource_id: str, detail: dict | None = None) -> None:
    db_write(
        """
        INSERT INTO audit_logs(created_at, actor, action, resource_type, resource_id, detail)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), "sync", action, resource_type, resource_id, json.dumps(detail or {}, ensure_ascii=False)),
    )


def record_event(level: str, message: str, run_id: int | None = None, source: str = "", target: str = "") -> None:
    db_write(
        "INSERT INTO log_events(created_at, level, run_id, source, target, message) VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), level, run_id, source, target, message),
    )


QUEUE_ACTIVE_STATUSES = {"queued", "running", "paused", "cancel_requested"}
QUEUE_TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def clean_queue_sources(source: str | None = None, sources: list[str] | None = None) -> list[str]:
    clean_sources = [str(item).strip() for item in (sources or []) if str(item).strip()]
    if not clean_sources and source and source.strip():
        clean_sources = [source.strip()]
    return list(dict.fromkeys(clean_sources))


def parse_queue_sources(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item).strip() for item in decoded if str(item).strip()]


def queue_dedupe_key(reason: str, sources: list[str]) -> str:
    raw = json.dumps({"reason": reason, "sources": sorted(sources)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_queue_row(queue_id: int) -> dict | None:
    return db_one(
        """
        SELECT id, reason, sources, priority, status, dedupe_key, scheduled_at, attempts, run_id,
               message, created_at, updated_at, started_at, finished_at
        FROM sync_queue
        WHERE id = ?
        """,
        (queue_id,),
    )


def enqueue_sync_queue_task(
    reason: str,
    source: str | None = None,
    sources: list[str] | None = None,
    priority: int = 100,
    scheduled_at: str | None = None,
    force: bool = False,
) -> dict | None:
    clean_reason = reason.strip() or "manual"
    clean_sources = clean_queue_sources(source=source, sources=sources)
    dedupe_key = queue_dedupe_key(clean_reason, clean_sources)
    if not force:
        existing = db_one(
            """
            SELECT id, reason, sources, priority, status, dedupe_key, scheduled_at, attempts, run_id,
                   message, created_at, updated_at, started_at, finished_at
            FROM sync_queue
            WHERE dedupe_key = ? AND status IN ('queued', 'running', 'paused', 'cancel_requested')
            ORDER BY id DESC
            LIMIT 1
            """,
            (dedupe_key,),
        )
        if existing:
            existing["duplicate"] = True
            return existing
    now = now_iso()
    queue_id = db_write(
        """
        INSERT INTO sync_queue(reason, sources, priority, status, dedupe_key, scheduled_at, attempts, created_at, updated_at, message)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            clean_reason,
            json.dumps(clean_sources, ensure_ascii=False),
            max(0, min(int(priority), 1000)),
            "queued",
            dedupe_key,
            scheduled_at or now,
            now,
            now,
            "queued",
        ),
    )
    audit_log("enqueue", "sync_queue", str(queue_id), {"reason": clean_reason, "sources": clean_sources, "priority": priority})
    row = sync_queue_row(queue_id) if queue_id else None
    if row:
        row["duplicate"] = False
    return row


def mark_sync_queue_task(
    queue_id: int,
    status: str,
    message: str = "",
    run_id: int | None = None,
    started: bool = False,
    finished: bool = False,
    increment_attempts: bool = False,
) -> None:
    now = now_iso()
    db_write(
        """
        UPDATE sync_queue
        SET status = ?,
            message = ?,
            updated_at = ?,
            run_id = COALESCE(?, run_id),
            attempts = attempts + ?,
            started_at = CASE WHEN ? = 1 THEN ? ELSE started_at END,
            finished_at = CASE WHEN ? = 1 THEN ? ELSE finished_at END
        WHERE id = ?
        """,
        (
            status,
            message,
            now,
            run_id,
            1 if increment_attempts else 0,
            1 if started else 0,
            now,
            1 if finished else 0,
            now,
            queue_id,
        ),
    )


def attach_sync_queue_run(queue_id: int | None, run_id: int) -> None:
    if queue_id is None:
        return
    db_write(
        "UPDATE sync_queue SET run_id = ?, updated_at = ? WHERE id = ?",
        (run_id, now_iso(), queue_id),
    )


def sync_queue_cancel_requested(queue_id: int) -> bool:
    row = sync_queue_row(queue_id)
    return bool(row and row.get("status") == "cancel_requested")


def next_sync_queue_task() -> dict | None:
    return db_one(
        """
        SELECT id, reason, sources, priority, status, dedupe_key, scheduled_at, attempts, run_id,
               message, created_at, updated_at, started_at, finished_at
        FROM sync_queue
        WHERE status = 'queued' AND scheduled_at <= ?
        ORDER BY priority ASC, id ASC
        LIMIT 1
        """,
        (now_iso(),),
    )


def recover_stale_queue_tasks() -> None:
    db_write(
        """
        UPDATE sync_queue
        SET status = 'queued', message = 'recovered after worker restart', updated_at = ?, started_at = NULL
        WHERE status IN ('running', 'cancel_requested')
        """,
        (now_iso(),),
    )


def create_run(reason: str, only_source: str | None = None) -> int:
    return db_write(
        "INSERT INTO sync_runs(reason, status, only_source, started_at) VALUES (?, ?, ?, ?)",
        (reason, "running", only_source, now_iso()),
    )


def update_run(run_id: int, status: str, total: int, updated: int, skipped: int, failed: int, message: str = "") -> None:
    db_write(
        """
        UPDATE sync_runs
        SET status = ?, ended_at = ?, total = ?, updated = ?, skipped = ?, failed = ?, message = ?
        WHERE id = ?
        """,
        (status, now_iso(), total, updated, skipped, failed, message, run_id),
    )


def create_run_item(run_id: int, source: str, target: str, old_digest: str | None) -> int:
    return db_write(
        """
        INSERT INTO sync_run_items(run_id, source, target, status, old_digest, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, source, target, "running", old_digest, now_iso()),
    )


def update_run_item(
    item_id: int,
    status: str,
    new_digest: str | None = None,
    step: str = "",
    error: str = "",
    copy_target: str = "",
    started_at_monotonic: float | None = None,
) -> None:
    duration_ms = None
    if started_at_monotonic is not None:
        duration_ms = int((time.monotonic() - started_at_monotonic) * 1000)
    db_write(
        """
        UPDATE sync_run_items
        SET status = ?, new_digest = ?, step = ?, error = ?, copy_target = ?, ended_at = ?, duration_ms = ?
        WHERE id = ?
        """,
        (status, new_digest, step, error, copy_target, now_iso(), duration_ms, item_id),
    )


def upsert_mirror(source: str, target: str, digest: str | None = None) -> None:
    db_write(
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


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        logger.warning("配置文件不存在: %s", CONFIG_PATH)
        return default_config()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.error("读取配置失败: %s", exc)
        return {"mirrors": [], "settings": {"check_interval_minutes": 30}}
    config.setdefault("mirrors", [])
    config.setdefault("settings", {})
    config.setdefault("registries", [])
    config.setdefault("mirror_groups", [])
    return config


def group_map(config: dict) -> dict[str, dict]:
    groups = {
        "default": {
            "id": "default",
            "name": "Default",
            "project": "default",
            "environment": "local",
            "namespace": "library",
            "registry": "local",
        }
    }
    for item in config.get("mirror_groups", []):
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or item.get("name") or "").strip()
        if not group_id:
            continue
        groups[group_id] = {
            "id": group_id,
            "name": str(item.get("name") or group_id).strip() or group_id,
            "project": str(item.get("project") or "default").strip() or "default",
            "environment": str(item.get("environment") or "local").strip() or "local",
            "namespace": str(item.get("namespace") or "library").strip() or "library",
            "registry": str(item.get("registry") or "local").strip() or "local",
        }
    return groups


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError as exc:
        backup = STATE_PATH.with_suffix(f".invalid-{int(time.time())}.json")
        try:
            STATE_PATH.replace(backup)
            logger.error("状态文件损坏，已备份到 %s: %s", backup, exc)
        except OSError:
            logger.error("状态文件损坏且备份失败: %s", exc)
        return {}


def save_state(state: dict) -> None:
    atomic_write_text(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


def valid_mirrors(config: dict) -> list[dict]:
    result = []
    groups = group_map(config)
    for index, item in enumerate(config.get("mirrors", []), start=1):
        if not isinstance(item, dict):
            logger.error("第 %d 条镜像配置不是对象，已跳过", index)
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if not source or not target:
            logger.error("第 %d 条镜像配置缺少 source 或 target，已跳过", index)
            continue
        group_id = str(item.get("group") or item.get("group_id") or "default").strip() or "default"
        group = groups.get(group_id, groups["default"])
        result.append(
            {
                "source": source,
                "target": target,
                "registry": str(item.get("registry") or item.get("registry_id") or group.get("registry") or "local").strip() or "local",
                "group": group_id,
                "project": str(item.get("project") or group.get("project") or "default").strip() or "default",
                "environment": str(item.get("environment") or group.get("environment") or "local").strip() or "local",
                "namespace": str(item.get("namespace") or group.get("namespace") or "library").strip() or "library",
                "source_credential_id": str(item.get("source_credential_id") or "").strip(),
                "target_credential_id": str(item.get("target_credential_id") or "").strip(),
            }
        )
        upsert_mirror(source, target)
    return result


def image_registry_host(value: str) -> str:
    first = value.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first.lower()
    return "docker.io"


def image_repo_tag(image: str) -> tuple[str, str]:
    value = image.strip()
    first, rest = (value.split("/", 1) + [""])[:2]
    without_registry = rest if rest and ("." in first or ":" in first or first == "localhost") else value
    if ":" not in without_registry:
        return without_registry, ""
    repo, tag = without_registry.rsplit(":", 1)
    return repo, tag


def pattern_matches(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value.lower(), (pattern or "*").lower())


def load_tag_protection_rules() -> list[dict]:
    return db_rows(
        """
        SELECT id, name, repo_pattern, tag_pattern, environment, enabled, reason
        FROM tag_protection_rules
        WHERE enabled = 1
        ORDER BY id
        """
    )


def tag_protection_reasons(repo: str, tag: str, environment: str = "", rules: list[dict] | None = None) -> list[str]:
    reasons = []
    env = (environment or "").lower()
    if env in {"prod", "production"}:
        reasons.append("protected_environment")
    if re.match(r"^v\d", tag or ""):
        reasons.append("release_tag")
    for row in rules if rules is not None else load_tag_protection_rules():
        if not pattern_matches(str(row.get("repo_pattern") or "*"), repo):
            continue
        if not pattern_matches(str(row.get("tag_pattern") or "*"), tag):
            continue
        rule_env = str(row.get("environment") or "*")
        if rule_env != "*" and (not environment or not pattern_matches(rule_env, environment)):
            continue
        reasons.append(str(row.get("reason") or row.get("name") or row.get("id") or "protected_rule"))
    return reasons


def parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def next_run_from_cron(cron: str, base: datetime | None = None) -> str:
    now = (base or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    parts = cron.strip().split()
    if len(parts) != 5:
        return (now + timedelta(hours=24)).isoformat()
    minute, hour, day, month, weekday = parts
    if day == month == weekday == "*" and minute.startswith("*/") and hour == "*":
        try:
            interval = max(1, min(int(minute[2:]), 1440))
        except ValueError:
            interval = 1440
        return (now + timedelta(minutes=interval)).isoformat()
    if day == month == weekday == "*":
        try:
            target_minute = int(minute)
            target_hour = int(hour)
        except ValueError:
            return (now + timedelta(hours=24)).isoformat()
        candidate = now.replace(hour=target_hour, minute=target_minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.isoformat()
    return (now + timedelta(hours=24)).isoformat()


def load_due_scheduled_policies(force: bool = False) -> list[dict]:
    rows = db_rows(
        """
        SELECT id, name, source, target, cron, enabled, allow_latest, source_credential_id, target_credential_id,
               last_run_at, next_run_at, last_error
        FROM scheduled_push_policies
        WHERE enabled = 1
        ORDER BY id
        """
    )
    now = datetime.now(timezone.utc)
    due = []
    for row in rows:
        next_run_at = str(row.get("next_run_at") or "")
        if force or not next_run_at or parse_iso(next_run_at) <= now:
            due.append(row)
    return due


def load_scheduled_policy(policy_id: str) -> dict | None:
    rows = db_rows(
        """
        SELECT id, name, source, target, cron, enabled, allow_latest, source_credential_id, target_credential_id,
               last_run_at, next_run_at, last_error
        FROM scheduled_push_policies
        WHERE id = ?
        """,
        (policy_id,),
    )
    return rows[0] if rows else None


def update_scheduled_policy_result(policy_id: str, cron: str, error: str = "") -> None:
    db_write(
        """
        UPDATE scheduled_push_policies
        SET last_run_at = ?, next_run_at = ?, last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            next_run_from_cron(cron),
            error,
            now_iso(),
            policy_id,
        ),
    )


def credential_fernet() -> Fernet | None:
    secret = CREDENTIALS_SECRET_KEY.strip()
    if not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def decrypt_credential_secret(encrypted_secret: str) -> str:
    fernet = credential_fernet()
    if not fernet:
        raise ValueError("CREDENTIALS_SECRET_KEY 未配置，无法解密仓库凭据")
    try:
        return fernet.decrypt(encrypted_secret.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("仓库凭据无法解密，请检查 CREDENTIALS_SECRET_KEY") from exc


def load_credentials() -> list[dict]:
    return db_rows(
        """
        SELECT id, registry_host, username, encrypted_secret, scope
        FROM credentials
        ORDER BY registry_host, id
        """
    )


def credential_allows(row: dict, purpose: str) -> bool:
    scope = str(row.get("scope") or "both").lower()
    return scope == "both" or scope == purpose


def find_credential(image: str, purpose: str, explicit_id: str = "", credentials: list[dict] | None = None) -> dict | None:
    rows = credentials if credentials is not None else load_credentials()
    if explicit_id:
        for row in rows:
            if row.get("id") == explicit_id and credential_allows(row, purpose):
                return row
        raise ValueError(f"{purpose} 凭据不存在或 scope 不匹配: {explicit_id}")
    host = image_registry_host(image)
    for row in rows:
        if str(row.get("registry_host") or "").lower() == host and credential_allows(row, purpose):
            return row
    return None


def auth_entry(row: dict) -> tuple[str, dict]:
    secret = decrypt_credential_secret(str(row.get("encrypted_secret") or ""))
    username = str(row.get("username") or "")
    auth = base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")
    return str(row.get("registry_host") or ""), {"username": username, "password": secret, "auth": auth}


def write_temp_authfile(source_credential: dict | None, target_credential: dict | None) -> str:
    auths = {}
    for row in [source_credential, target_credential]:
        if not row:
            continue
        host, entry = auth_entry(row)
        auths[host] = entry
    if not auths:
        return ""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".auth.json") as handle:
        json.dump({"auths": auths}, handle)
        handle.flush()
        os.fsync(handle.fileno())
        return handle.name


def remove_temp_authfile(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError as exc:
        logger.warning("临时 authfile 清理失败: %s", exc)


def redact_command(cmd: list[str]) -> str:
    redacted = []
    skip_next = False
    for item in cmd:
        if skip_next:
            redacted.append("<authfile>")
            skip_next = False
            continue
        redacted.append(item)
        if item == "--authfile":
            skip_next = True
    return " ".join(redacted)


def run_command(step_name: str, cmd: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        message = f"{step_name} 超时（{timeout} 秒）: {redact_command(cmd)}"
        logger.error(message)
        return False, message
    except OSError as exc:
        message = f"{step_name} 启动失败: {exc}"
        logger.error(message)
        return False, message

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        message = f"{step_name} 失败 [{redact_command(cmd)}]: {stderr}"
        logger.error(message)
        return False, message
    return True, ""


def build_skopeo_inspect_command(image: str, authfile: str = "") -> list[str]:
    cmd = ["skopeo", "inspect", "--format", "{{.Digest}}"]
    if authfile:
        cmd.extend(["--authfile", authfile])
    elif SKOPEO_AUTHFILE:
        cmd.extend(["--authfile", SKOPEO_AUTHFILE])
    cmd.append(f"docker://{image}")
    return cmd


def inspect_remote_digest(image: str, authfile: str = "") -> tuple[str | None, str]:
    cmd = build_skopeo_inspect_command(image, authfile)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        message = f"inspect 超时（60 秒）: {image}"
        logger.error(message)
        return None, message
    except OSError as exc:
        message = f"inspect 启动失败: {exc}"
        logger.error(message)
        return None, message
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        logger.warning("skopeo inspect 返回错误 %s: %s", image, message)
        return None, message
    digest = result.stdout.strip()
    return (digest or None), ""


def get_remote_digest(image: str) -> str | None:
    digest, _ = inspect_remote_digest(image)
    return digest


def resolve_copy_target(target: str) -> str:
    if not SYNC_TARGET_REGISTRY:
        return target
    for alias in LOCAL_REGISTRY_ALIASES:
        prefix = f"{alias}/"
        if target.startswith(prefix):
            return f"{SYNC_TARGET_REGISTRY}/{target[len(prefix):]}"
    return target


def build_skopeo_copy_command(source: str, copy_target: str, authfile: str = "") -> list[str]:
    cmd = [
        "skopeo",
        "copy",
        f"--src-tls-verify={SKOPEO_SRC_TLS_VERIFY}",
        f"--dest-tls-verify={SKOPEO_DEST_TLS_VERIFY}",
    ]
    if SKOPEO_COPY_ALL:
        cmd.append("--all")
    if authfile:
        cmd.extend(["--authfile", authfile])
    elif SKOPEO_AUTHFILE:
        cmd.extend(["--authfile", SKOPEO_AUTHFILE])
    cmd.extend([f"docker://{source}", f"docker://{copy_target}"])
    return cmd


def get_target_lock(copy_target: str) -> threading.Lock:
    with target_locks_guard:
        if copy_target not in target_locks:
            target_locks[copy_target] = threading.Lock()
        return target_locks[copy_target]


def copy_image(source: str, target: str, retry_count: int | None = None, authfile: str = "") -> tuple[bool, str, str]:
    copy_target = resolve_copy_target(target)
    cmd = build_skopeo_copy_command(source, copy_target, authfile=authfile)
    attempts = bounded_int(retry_count if retry_count is not None else SYNC_RETRY_COUNT, SYNC_RETRY_COUNT, 0, 10) + 1
    with get_target_lock(copy_target):
        last_error = ""
        for attempt in range(1, attempts + 1):
            logger.info("skopeo copy 尝试 %d/%d: %s -> %s", attempt, attempts, source, copy_target)
            ok, error = run_command("copy", cmd)
            if ok:
                return True, copy_target, ""
            last_error = error
            if attempt < attempts:
                delay = min(SYNC_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)), 60)
                logger.warning("copy 失败，将在 %d 秒后重试: %s", delay, error)
                time.sleep(delay)
    return False, copy_target, last_error


def pull_and_push(source: str, target: str) -> bool:
    ok, _, _ = copy_image(source, target)
    return ok


def cleanup_local_tags(source: str, target: str) -> None:
    logger.info("skopeo copy 不产生本地 Docker tag，跳过本地镜像清理: %s -> %s", source, target)


def setting_int(config: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    settings = config.get("settings", {}) if isinstance(config.get("settings", {}), dict) else {}
    return bounded_int(settings.get(name, default), default, minimum, maximum)


def effective_webhook_url(config: dict) -> str:
    settings = config.get("settings", {}) if isinstance(config.get("settings", {}), dict) else {}
    return str(settings.get("notify_webhook_url") or NOTIFY_WEBHOOK_URL).strip()


def webhook_dedupe_key(event_type: str) -> str:
    return hashlib.sha256(event_type.encode("utf-8")).hexdigest()[:16]


def should_send_webhook_event(event_type: str) -> bool:
    if NOTIFY_DEDUPE_SECONDS <= 0:
        return True
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fingerprint = webhook_dedupe_key(event_type)
    state_key = f"notify_last_{fingerprint}"
    try:
        last_value = runtime_value(state_key, "")
        last_sent_at = parse_iso(last_value) if last_value else datetime.min.replace(tzinfo=timezone.utc)
        if last_sent_at != datetime.min.replace(tzinfo=timezone.utc) and (now - last_sent_at).total_seconds() < NOTIFY_DEDUPE_SECONDS:
            set_runtime_state("notify_last_suppressed_at", now.isoformat())
            set_runtime_state("notify_last_suppressed_event", event_type)
            logger.info("webhook 通知已去重: %s", event_type)
            return False
        set_runtime_state(state_key, now.isoformat())
    except Exception as exc:  # pragma: no cover - notification must not break sync execution
        logger.warning("webhook 去重状态写入失败: %s", exc)
    return True


def notify_webhook(event_type: str, payload: dict, webhook_url: str = "") -> None:
    url = webhook_url or NOTIFY_WEBHOOK_URL
    if not url:
        return
    if not should_send_webhook_event(event_type):
        return
    body = json.dumps(
        {
            "event": event_type,
            "app": "mirror-registry",
            "app_version": APP_VERSION,
            "image_tag": IMAGE_TAG,
            "created_at": now_iso(),
            "payload": payload,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "mirror-registry-sync"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            logger.info("webhook 通知已发送: %s HTTP %s", event_type, response.status)
        set_runtime_state("notify_last_sent_at", now_iso())
        set_runtime_state("notify_last_sent_event", event_type)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("webhook 通知失败: %s", exc)


def check_disk_space(run_id: int | None = None, webhook_url: str = "") -> dict:
    try:
        usage = shutil.disk_usage(LOG_PATH.parent)
    except OSError as exc:
        logger.warning("读取磁盘空间失败: %s", exc)
        return {"ok": False, "error": str(exc)}

    free_bytes = int(usage.free)
    total_bytes = int(usage.total)
    set_runtime_state("disk_free_bytes", str(free_bytes))
    set_runtime_state("disk_total_bytes", str(total_bytes))
    set_runtime_state("disk_low_threshold_bytes", str(DISK_LOW_BYTES))
    low = DISK_LOW_BYTES > 0 and free_bytes < DISK_LOW_BYTES
    set_runtime_state("disk_low", "true" if low else "false")
    result = {"ok": True, "free_bytes": free_bytes, "total_bytes": total_bytes, "low": low}
    if low:
        message = f"磁盘剩余空间低于阈值: free={free_bytes}, threshold={DISK_LOW_BYTES}"
        logger.warning(message)
        record_event("WARNING", message, run_id)
        notify_webhook("disk_low", result, webhook_url)
    return result


def update_heartbeat(interval: int | None = None, concurrency: int | None = None, retry_count: int | None = None) -> None:
    skopeo_path = shutil.which("skopeo") or ""
    set_runtime_state("sync_engine", SYNC_ENGINE)
    set_runtime_state("app_version", APP_VERSION)
    set_runtime_state("image_tag", IMAGE_TAG)
    set_runtime_state("database_backend", database_backend(DATABASE_URL))
    set_runtime_state("skopeo_available", "true" if skopeo_path else "false")
    set_runtime_state("skopeo_path", skopeo_path)
    set_runtime_state("last_heartbeat", now_iso())
    if concurrency is not None:
        set_runtime_state("sync_concurrency", str(concurrency))
    if retry_count is not None:
        set_runtime_state("sync_retry_count", str(retry_count))
    if interval is not None:
        set_runtime_state("check_interval_minutes", str(interval))
        set_runtime_state("next_run_at", (datetime.now(timezone.utc) + timedelta(minutes=interval)).replace(microsecond=0).isoformat())


def process_mirror(run_id: int, mirror: dict, state: dict, retry_count: int) -> str:
    started_at = time.monotonic()
    source = mirror["source"]
    target = mirror["target"]
    with state_lock:
        cached = state.get(source)
    item_id = create_run_item(run_id, source, target, cached)

    logger.info("检查镜像: %s", source)
    record_event("INFO", f"检查镜像: {source}", run_id, source, target)
    audit_log(
        "check",
        "mirror",
        source,
        {
            "target": target,
            "registry": mirror.get("registry", "local"),
            "group": mirror.get("group", "default"),
            "project": mirror.get("project", "default"),
            "environment": mirror.get("environment", "local"),
            "namespace": mirror.get("namespace", "library"),
        },
    )

    target_repo, target_tag = image_repo_tag(resolve_copy_target(target))
    protection_reasons = tag_protection_reasons(target_repo, target_tag, str(mirror.get("environment") or ""))
    if protection_reasons:
        message = f"受保护 tag 不允许自动覆盖: {target_repo}:{target_tag} ({', '.join(protection_reasons)})"
        logger.warning(message)
        record_event("ERROR", message, run_id, source, target)
        audit_log("copy_blocked", "image", f"{target_repo}:{target_tag}", {"source": source, "target": target, "reasons": protection_reasons})
        update_run_item(item_id, "failed", step="protection", error=message, started_at_monotonic=started_at)
        return "failed"

    authfile = ""
    try:
        credentials = load_credentials()
        source_credential = find_credential(source, "source", mirror.get("source_credential_id", ""), credentials)
        target_credential = find_credential(resolve_copy_target(target), "target", mirror.get("target_credential_id", ""), credentials)
        authfile = write_temp_authfile(source_credential, target_credential)
    except ValueError as exc:
        message = str(exc)
        logger.warning("仓库凭据不可用: %s", message)
        record_event("ERROR", f"仓库凭据不可用: {message}", run_id, source, target)
        update_run_item(item_id, "failed", step="credentials", error=message, started_at_monotonic=started_at)
        return "failed"

    try:
        remote, error = inspect_remote_digest(source, authfile=authfile)
        if not remote:
            logger.warning("跳过（无法获取 digest）: %s", source)
            record_event("WARNING", f"无法获取 digest: {error}", run_id, source, target)
            update_run_item(item_id, "failed", step="inspect", error=error, started_at_monotonic=started_at)
            return "failed"

        if remote == cached:
            logger.info("无更新: %s", source)
            record_event("INFO", "digest 未变化，跳过同步", run_id, source, target)
            update_run_item(item_id, "skipped", new_digest=remote, step="inspect", started_at_monotonic=started_at)
            upsert_mirror(source, target, remote)
            return "skipped"

        short_old = (cached[:19] + "...") if cached else "新镜像"
        short_new = remote[:19] + "..."
        logger.info("发现更新: %s  %s -> %s", source, short_old, short_new)
        record_event("INFO", f"发现更新: {short_old} -> {short_new}", run_id, source, target)

        ok, copy_target, copy_error = copy_image(source, target, retry_count=retry_count, authfile=authfile)
        if ok:
            with state_lock:
                state[source] = remote
                save_state(state)
            upsert_mirror(source, target, remote)
            logger.info("同步完成: %s", target)
            record_event("INFO", "同步完成", run_id, source, target)
            audit_log("copy_success", "mirror", source, {"target": target, "copy_target": copy_target, "digest": remote})
            audit_log("tag_written", "image", f"{target_repo}:{target_tag}", {"source": source, "target": target, "copy_target": copy_target, "digest": remote, "run_id": run_id})
            update_run_item(
                item_id,
                "success",
                new_digest=remote,
                step="copy",
                copy_target=copy_target,
                started_at_monotonic=started_at,
            )
            return "updated"

        logger.error("同步失败: %s -> %s，失败步骤: copy", source, target)
        record_event("ERROR", f"同步失败: {copy_error}", run_id, source, target)
        audit_log("copy_failed", "mirror", source, {"target": target, "error": copy_error})
        update_run_item(
            item_id,
            "failed",
            new_digest=remote,
            step="copy",
            error=copy_error,
            copy_target=copy_target,
            started_at_monotonic=started_at,
        )
        return "failed"
    finally:
        remove_temp_authfile(authfile)


def process_scheduled_policy(policy: dict, retry_count: int, queue_id: int | None = None) -> dict:
    source = policy["source"]
    target = policy["target"]
    allow_latest = bool(policy.get("allow_latest"))
    if image_repo_tag(resolve_copy_target(target))[1] == "latest" and not allow_latest:
        message = f"计划推送默认不允许覆盖 latest: {target}"
        logger.warning(message)
        audit_log("copy_blocked", "scheduled_push_policy", policy["id"], {"source": source, "target": target, "reason": "latest"})
        update_scheduled_policy_result(policy["id"], policy["cron"], message)
        return {"status": "failed", "result": "failed", "run_id": None, "message": message}
    mirror = {
        "source": source,
        "target": target,
        "environment": policy.get("environment") or "local",
        "source_credential_id": policy.get("source_credential_id") or "",
        "target_credential_id": policy.get("target_credential_id") or "",
    }
    run_id = create_run(f"scheduled-policy:{policy['id']}", source)
    attach_sync_queue_run(queue_id, run_id)
    state = load_state()
    result = process_mirror(run_id, mirror, state, retry_count)
    status = "completed" if result in {"updated", "skipped"} else "failed"
    update_run(
        run_id,
        status,
        1,
        1 if result == "updated" else 0,
        1 if result == "skipped" else 0,
        1 if result == "failed" else 0,
        result,
    )
    error = "" if status == "completed" else "scheduled push failed"
    update_scheduled_policy_result(policy["id"], policy["cron"], error)
    audit_log("run", "scheduled_push_policy", policy["id"], {"source": source, "target": target, "result": result})
    return {"status": status, "result": result, "run_id": run_id, "message": result}


def normalize_sources(only_source: str | None = None, only_sources: list[str] | None = None) -> set[str]:
    selected = {str(item).strip() for item in (only_sources or []) if str(item).strip()}
    if only_source:
        selected.add(str(only_source).strip())
    return selected


def sync_all(
    reason: str = "scheduled",
    only_source: str | None = None,
    only_sources: list[str] | None = None,
    queue_id: int | None = None,
) -> dict:
    if sync_lock.locked():
        logger.warning("已有同步任务正在运行，本次触发将排队等待: %s", reason)

    with sync_lock:
        config = load_config()
        platform_groups = group_map(config)
        concurrency = setting_int(config, "sync_concurrency", SYNC_CONCURRENCY, 1, 16)
        retry_count = setting_int(config, "sync_retry_count", SYNC_RETRY_COUNT, 0, 10)
        interval = setting_int(config, "check_interval_minutes", 30, 1, 1440)
        webhook_url = effective_webhook_url(config)
        selected_sources = normalize_sources(only_source, only_sources)
        only_label = ",".join(sorted(selected_sources)) if selected_sources else None
        run_id = create_run(reason, only_label)
        attach_sync_queue_run(queue_id, run_id)
        set_runtime_state("sync_running", "true")
        set_runtime_state("sync_reason", reason)
        set_runtime_state("registry_count", str(len(config.get("registries", [])) + 1))
        set_runtime_state("mirror_group_count", str(len(platform_groups)))
        set_runtime_state("last_started_at", now_iso())
        update_heartbeat(interval=interval, concurrency=concurrency, retry_count=retry_count)

        logger.info("===== 开始检查镜像更新（%s，并发 %d）=====", reason, concurrency)
        record_event("INFO", f"开始检查镜像更新（{reason}，并发 {concurrency}）", run_id)
        audit_log(
            "start",
            "sync_run",
            str(run_id),
            {
                "reason": reason,
                "concurrency": concurrency,
                "retry_count": retry_count,
                "selected_sources": sorted(selected_sources),
                "groups": sorted(platform_groups.keys()),
            },
        )
        state = load_state()
        mirrors = valid_mirrors(config)
        if selected_sources:
            mirrors = [mirror for mirror in mirrors if mirror["source"] in selected_sources]

        total = len(mirrors)
        updated = 0
        skipped = 0
        failed = 0

        if not mirrors:
            message = "镜像列表为空"
            logger.info("镜像列表为空，跳过")
            record_event("INFO", "镜像列表为空，跳过", run_id)
            update_run(run_id, "completed", 0, 0, 0, 0, message)
            set_runtime_state("sync_running", "false")
            set_runtime_state("last_finished_at", now_iso())
            check_disk_space(run_id, webhook_url)
            return {"status": "completed", "run_id": run_id, "message": message, "total": 0, "updated": 0, "skipped": 0, "failed": 0}

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(process_mirror, run_id, mirror, state, retry_count) for mirror in mirrors]
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.exception("同步任务内部异常: %s", exc)
                        record_event("ERROR", f"同步任务内部异常: {exc}", run_id)
                        failed += 1
                        continue
                    if result == "updated":
                        updated += 1
                    elif result == "skipped":
                        skipped += 1
                    else:
                        failed += 1
        finally:
            status = "failed" if failed else "completed"
            message = f"更新 {updated}，跳过 {skipped}，失败 {failed}"
            update_run(run_id, status, total, updated, skipped, failed, message)
            set_runtime_state("sync_running", "false")
            set_runtime_state("last_finished_at", now_iso())
            logger.info("===== 检查完成，本次更新 %d 个镜像，失败 %d 个 =====", updated, failed)
            record_event("INFO", f"检查完成：{message}", run_id)
            audit_log("finish", "sync_run", str(run_id), {"status": status, "total": total, "updated": updated, "skipped": skipped, "failed": failed})
            disk = check_disk_space(run_id, webhook_url)
            was_failed = runtime_value("last_sync_failed", "false") == "true"
            if failed:
                set_runtime_state("last_sync_failed", "true")
                notify_webhook(
                    "sync_failed",
                    {"run_id": run_id, "reason": reason, "total": total, "updated": updated, "skipped": skipped, "failed": failed, "disk": disk},
                    webhook_url,
                )
            elif was_failed:
                set_runtime_state("last_sync_failed", "false")
                notify_webhook(
                    "sync_recovered",
                    {"run_id": run_id, "reason": reason, "total": total, "updated": updated, "skipped": skipped, "failed": failed, "disk": disk},
                    webhook_url,
                )
            else:
                set_runtime_state("last_sync_failed", "false")
        return {"status": status, "run_id": run_id, "message": message, "total": total, "updated": updated, "skipped": skipped, "failed": failed}


def check_scheduled_policies(force: bool = False) -> None:
    policies = load_due_scheduled_policies(force=force)
    if not policies:
        return
    logger.info("计划推送策略进入同步队列: %d", len(policies))
    for policy in policies:
        task = enqueue_sync_queue_task(
            f"scheduled-policy:{policy['id']}",
            source=policy["source"],
            priority=80 if not force else 60,
        )
        logger.info("计划推送已入队: %s queue=%s duplicate=%s", policy["id"], task.get("id") if task else "-", task.get("duplicate") if task else "-")


def process_sync_queue_task(row: dict) -> bool:
    queue_id = int(row["id"])
    current = sync_queue_row(queue_id)
    if not current or current.get("status") != "queued":
        return False

    reason = str(current.get("reason") or "manual")
    sources = parse_queue_sources(current.get("sources"))
    mark_sync_queue_task(queue_id, "running", "running", started=True, increment_attempts=True)
    logger.info("开始执行同步队列任务: queue=%s reason=%s sources=%s", queue_id, reason, sources or "all")

    try:
        if sync_queue_cancel_requested(queue_id):
            mark_sync_queue_task(queue_id, "canceled", "canceled before execution", finished=True)
            return True

        effective_reason = reason.removeprefix("replay:")
        result_run_id = None
        if effective_reason.startswith("scheduled-policy:"):
            policy_id = effective_reason.split(":", 1)[1]
            policy = load_scheduled_policy(policy_id)
            if not policy:
                raise ValueError(f"scheduled policy not found: {policy_id}")
            config = load_config()
            retry_count = setting_int(config, "sync_retry_count", SYNC_RETRY_COUNT, 0, 10)
            result = process_scheduled_policy(policy, retry_count, queue_id=queue_id)
            final_status = "completed" if result["status"] == "completed" else "failed"
            message = str(result.get("message") or final_status)
            result_run_id = result.get("run_id")
            if final_status == "failed":
                notify_webhook(
                    "scheduled_push_failed",
                    {"policy_id": policy["id"], "source": policy["source"], "target": policy["target"], "result": result.get("result")},
                    effective_webhook_url(config),
                )
        elif effective_reason == "scheduled-policy":
            check_scheduled_policies(force=True)
            final_status = "completed"
            message = "scheduled policies queued"
        else:
            result = sync_all(reason, only_sources=sources, queue_id=queue_id)
            final_status = "completed" if result.get("status") == "completed" else "failed"
            message = str(result.get("message") or final_status)
            result_run_id = result.get("run_id")

        if sync_queue_cancel_requested(queue_id):
            mark_sync_queue_task(queue_id, "canceled", "cancel requested while running; execution finished", run_id=result_run_id, finished=True)
        else:
            mark_sync_queue_task(queue_id, final_status, message, run_id=result_run_id, finished=True)
        return True
    except Exception as exc:
        logger.exception("同步队列任务失败: queue=%s", queue_id)
        mark_sync_queue_task(queue_id, "failed", str(exc), finished=True)
        return True


def process_sync_queue(limit: int = 1) -> int:
    if not queue_lock.acquire(blocking=False):
        return 0
    try:
        processed = 0
        while processed < limit:
            row = next_sync_queue_task()
            if not row:
                break
            if process_sync_queue_task(row):
                processed += 1
        return processed
    finally:
        queue_lock.release()


def parse_trigger() -> tuple[str, list[str] | None]:
    try:
        payload = json.loads(TRIGGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "manual", None
    reason = str(payload.get("reason") or "manual")
    sources = payload.get("sources")
    if isinstance(sources, list):
        clean_sources = [str(source).strip() for source in sources if str(source).strip()]
        return reason, clean_sources or None
    source = payload.get("source")
    return reason, [str(source).strip()] if source else None


def check_trigger() -> None:
    if TRIGGER_PATH.exists():
        try:
            payload = json.loads(TRIGGER_PATH.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        reason, sources = parse_trigger()
        queued = bool(isinstance(payload, dict) and payload.get("queued"))
        logger.info("收到同步触发: reason=%s queued=%s", reason, queued)
        TRIGGER_PATH.unlink(missing_ok=True)
        if not queued:
            enqueue_sync_queue_task(reason, sources=sources, priority=50)
    process_sync_queue()


def enqueue_periodic_sync() -> None:
    enqueue_sync_queue_task("scheduled", priority=120)


def main() -> None:
    config = load_config()
    interval = setting_int(config, "check_interval_minutes", 30, 1, 1440)
    concurrency = setting_int(config, "sync_concurrency", SYNC_CONCURRENCY, 1, 16)
    retry_count = setting_int(config, "sync_retry_count", SYNC_RETRY_COUNT, 0, 10)
    update_heartbeat(interval, concurrency, retry_count)
    recover_stale_queue_tasks()

    logger.info("同步服务启动，调度间隔: %d 分钟", interval)
    enqueue_sync_queue_task("startup", priority=120)
    process_sync_queue()

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(enqueue_periodic_sync, "interval", minutes=interval, id="auto_sync_enqueue")
    scheduler.add_job(process_sync_queue, "interval", seconds=10, id="queue_poll")
    scheduler.add_job(check_trigger, "interval", seconds=10, id="trigger_poll")
    scheduler.add_job(check_scheduled_policies, "interval", seconds=60, id="scheduled_push_poll")
    scheduler.add_job(lambda: update_heartbeat(interval, concurrency, retry_count), "interval", seconds=30, id="heartbeat")
    scheduler.start()


if __name__ == "__main__":
    main()
