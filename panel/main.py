import asyncio
import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import yaml
from cryptography.fernet import Fernet, InvalidToken
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
CREDENTIALS_SECRET_KEY = os.getenv("CREDENTIALS_SECRET_KEY", "")

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
    source_credential_id: str | None = Field(default=None, max_length=64)
    target_credential_id: str | None = Field(default=None, max_length=64)


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


class CredentialIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    registry_host: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    secret: str | None = Field(default=None, max_length=2000)
    scope: str = Field(default="both", max_length=16)


class CredentialTestIn(BaseModel):
    registry_url: str | None = Field(default=None, max_length=500)


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


class TagProtectionRuleIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    repo_pattern: str = Field(default="*", min_length=1, max_length=255)
    tag_pattern: str = Field(default="*", min_length=1, max_length=128)
    environment: str = Field(default="*", min_length=1, max_length=64)
    enabled: bool = True
    reason: str | None = Field(default="", max_length=500)


class RetentionPolicyIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    repo_pattern: str = Field(default="*", min_length=1, max_length=255)
    environment: str = Field(default="*", min_length=1, max_length=64)
    keep_last: int = Field(default=5, ge=1, le=200)
    max_age_days: int | None = Field(default=None, ge=1, le=3650)
    enabled: bool = False


class BackupRestoreVerifyIn(BaseModel):
    require_credentials_secret: bool = True


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

CREATE TABLE IF NOT EXISTS retention_policies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_pattern TEXT NOT NULL,
    environment TEXT NOT NULL,
    keep_last INTEGER NOT NULL,
    max_age_days INTEGER,
    enabled INTEGER NOT NULL DEFAULT 0,
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
    CREATE TABLE IF NOT EXISTS retention_policies (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        repo_pattern VARCHAR(255) NOT NULL,
        environment VARCHAR(64) NOT NULL,
        keep_last INTEGER NOT NULL,
        max_age_days INTEGER,
        enabled INTEGER NOT NULL DEFAULT 0,
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


def credential_row(credential_id: str) -> dict:
    clean_id = validate_slug(credential_id, "credential_id")
    row = db_one(
        "SELECT id, name, registry_host, username, encrypted_secret, scope, created_at, updated_at FROM credentials WHERE id = ?",
        (clean_id,),
    )
    if not row:
        raise HTTPException(404, "凭据不存在")
    return row


def mirror_credential_references(config: dict, credential_id: str) -> list[str]:
    refs = []
    for mirror in valid_mirrors(config):
        if mirror.get("source_credential_id") == credential_id:
            refs.append(f"{mirror['source']} source")
        if mirror.get("target_credential_id") == credential_id:
            refs.append(f"{mirror['source']} target")
    return refs


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


def slug_candidate(value: str, default: str = "credential") -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-_")
    if not candidate:
        candidate = default
    if not re.match(r"^[A-Za-z0-9]", candidate):
        candidate = f"{default}-{candidate}"
    return candidate[:64]


def optional_slug(value: str | None, field_name: str) -> str:
    if value is None:
        return ""
    clean = str(value).strip()
    return validate_slug(clean, field_name) if clean else ""


def image_registry_host(value: str) -> str:
    first = value.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return "docker.io"


def normalize_registry_host(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise HTTPException(400, "registry_host 不能为空")
    if "://" in raw:
        parsed = urlparse(raw)
        if not parsed.netloc:
            raise HTTPException(400, "registry_host 格式不合法")
        return parsed.netloc.lower()
    return image_registry_host(raw).lower() if "/" in raw else raw.lower()


def validate_credential_scope(value: str) -> str:
    scope = value.strip().lower() or "both"
    if scope not in {"source", "target", "both"}:
        raise HTTPException(400, "scope 必须是 source、target 或 both")
    return scope


def require_credentials_secret() -> str:
    secret = CREDENTIALS_SECRET_KEY.strip()
    if not secret:
        raise HTTPException(400, "保存仓库凭据前必须设置 CREDENTIALS_SECRET_KEY")
    return secret


def credential_fernet() -> Fernet:
    digest = hashlib.sha256(require_credentials_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(secret: str) -> str:
    return credential_fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(encrypted_secret: str) -> str:
    try:
        return credential_fernet().decrypt(encrypted_secret.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(400, "凭据无法解密，请检查 CREDENTIALS_SECRET_KEY") from exc


def public_credential(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "registry_host": row["registry_host"],
        "username": row["username"],
        "scope": row["scope"],
        "configured": bool(row.get("encrypted_secret")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def image_repo_tag(image: str) -> tuple[str, str]:
    value = image.strip()
    if ":" not in value:
        raise HTTPException(400, "镜像地址必须包含 tag")
    first, rest = (value.split("/", 1) + [""])[:2]
    without_registry = rest if rest and ("." in first or ":" in first or first == "localhost") else value
    repo, tag = without_registry.rsplit(":", 1)
    return validate_repo_tag(repo, tag)


def pattern_matches(pattern: str, value: str) -> bool:
    clean_pattern = (pattern or "*").lower()
    return fnmatch.fnmatchcase(value.lower(), clean_pattern)


def mirror_context_for_tag(repo: str, tag: str, config: dict | None = None) -> dict:
    for mirror in valid_mirrors(config or load_config()):
        try:
            target_repo, target_tag = image_repo_tag(mirror["target"])
        except HTTPException:
            continue
        if target_repo == repo and target_tag == tag:
            return mirror
    return {}


def default_protection_reasons(tag: str, environment: str = "") -> list[str]:
    reasons = []
    env = environment.lower()
    if env in {"prod", "production"}:
        reasons.append("protected_environment")
    if re.match(r"^v\d", tag):
        reasons.append("release_tag")
    return reasons


def protection_rule_rows(enabled_only: bool = False) -> list[dict]:
    where = "WHERE enabled = 1" if enabled_only else ""
    return db_rows(
        f"""
        SELECT id, name, repo_pattern, tag_pattern, environment, enabled, reason, created_at, updated_at
        FROM tag_protection_rules
        {where}
        ORDER BY id
        """
    )


def protection_result(repo: str, tag: str, environment: str = "") -> dict:
    clean_repo, clean_tag = validate_repo_tag(repo, tag)
    env = environment or mirror_context_for_tag(clean_repo, clean_tag).get("environment", "")
    reasons = default_protection_reasons(clean_tag, env)
    matched_rules = []
    for row in protection_rule_rows(enabled_only=True):
        if not pattern_matches(row["repo_pattern"], clean_repo):
            continue
        if not pattern_matches(row["tag_pattern"], clean_tag):
            continue
        rule_env = row.get("environment") or "*"
        if rule_env != "*" and env and not pattern_matches(rule_env, env):
            continue
        if rule_env != "*" and not env:
            continue
        matched_rules.append(public_protection_rule(row))
        reasons.append(row.get("reason") or row.get("name") or row["id"])
    return {
        "repo": clean_repo,
        "tag": clean_tag,
        "environment": env or "",
        "protected": bool(reasons),
        "reasons": reasons,
        "rules": matched_rules,
    }


def assert_tag_mutation_allowed(repo: str, tag: str, action: str, environment: str = "") -> dict:
    result = protection_result(repo, tag, environment)
    if result["protected"]:
        raise HTTPException(
            409,
            f"受保护 tag 不能执行 {action}: {repo}:{tag} ({', '.join(result['reasons'])})",
        )
    return result


def public_protection_rule(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "repo_pattern": row["repo_pattern"],
        "tag_pattern": row["tag_pattern"],
        "environment": row["environment"],
        "enabled": bool(row["enabled"]),
        "reason": row.get("reason") or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def public_retention_policy(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "repo_pattern": row["repo_pattern"],
        "environment": row["environment"],
        "keep_last": int(row["keep_last"]),
        "max_age_days": row.get("max_age_days"),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def retention_policy_row(policy_id: str) -> dict:
    clean_id = validate_slug(policy_id, "policy_id")
    row = db_one(
        """
        SELECT id, name, repo_pattern, environment, keep_last, max_age_days, enabled, created_at, updated_at
        FROM retention_policies
        WHERE id = ?
        """,
        (clean_id,),
    )
    if not row:
        raise HTTPException(404, "保留策略不存在")
    return row


def parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def known_image_tags() -> list[dict]:
    rows = db_rows(
        """
        SELECT source, target, status, new_digest, ended_at, started_at
        FROM sync_run_items
        WHERE target != ''
        ORDER BY COALESCE(ended_at, started_at) DESC
        """
    )
    seen = {}
    config = load_config()
    for row in rows:
        try:
            repo, tag = image_repo_tag(row["target"])
        except HTTPException:
            continue
        ref = f"{repo}:{tag}"
        if ref in seen:
            continue
        context = mirror_context_for_tag(repo, tag, config)
        seen[ref] = {
            "repo": repo,
            "tag": tag,
            "source": row.get("source") or "",
            "target": row.get("target") or "",
            "digest": row.get("new_digest") or "",
            "status": row.get("status") or "",
            "synced_at": row.get("ended_at") or row.get("started_at") or "",
            "environment": context.get("environment", ""),
        }
    return list(seen.values())


def retention_dry_run(policy: dict) -> dict:
    public = public_retention_policy(policy)
    now = datetime.now(timezone.utc)
    grouped: dict[str, list[dict]] = {}
    for item in known_image_tags():
        if not pattern_matches(policy["repo_pattern"], item["repo"]):
            continue
        policy_env = policy.get("environment") or "*"
        item_env = item.get("environment") or ""
        if policy_env != "*" and not pattern_matches(policy_env, item_env):
            continue
        grouped.setdefault(item["repo"], []).append(item)

    candidates = []
    skipped_protected = []
    keep_last = int(policy["keep_last"])
    max_age_days = policy.get("max_age_days")
    for repo, items in grouped.items():
        sorted_items = sorted(items, key=lambda item: parse_iso(item["synced_at"]), reverse=True)
        for index, item in enumerate(sorted_items):
            reasons = []
            if index >= keep_last:
                reasons.append(f"exceeds_keep_last_{keep_last}")
            if max_age_days:
                age = now - parse_iso(item["synced_at"])
                if age > timedelta(days=int(max_age_days)):
                    reasons.append(f"older_than_{max_age_days}_days")
            if not reasons:
                continue
            protection = protection_result(item["repo"], item["tag"], item.get("environment", ""))
            entry = {**item, "reasons": reasons, "protection": protection}
            if protection["protected"]:
                skipped_protected.append(entry)
            else:
                candidates.append(entry)
    return {"policy": public, "candidates": candidates, "skipped_protected": skipped_protected}


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
        "source_credential_id": optional_slug(item.get("source_credential_id"), "source_credential_id"),
        "target_credential_id": optional_slug(item.get("target_credential_id"), "target_credential_id"),
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
                        "source_credential_id": "",
                        "target_credential_id": "",
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
    checks.append(
        diagnostic_item(
            "仓库凭据密钥",
            "ok" if CREDENTIALS_SECRET_KEY.strip() else "warn",
            "CREDENTIALS_SECRET_KEY 已配置" if CREDENTIALS_SECRET_KEY.strip() else "未配置 CREDENTIALS_SECRET_KEY，不能保存加密仓库凭据",
            "生产环境保存仓库凭据前必须设置 CREDENTIALS_SECRET_KEY",
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


@app.get("/api/tag-protection")
def list_tag_protection_rules():
    return [public_protection_rule(row) for row in protection_rule_rows()]


@app.post("/api/tag-protection", dependencies=[Depends(require_write_token)])
def upsert_tag_protection_rule(body: TagProtectionRuleIn):
    rule_id = validate_slug(body.id or slug_candidate(body.name, "protect"), "rule_id")
    now = now_iso()
    existing = db_one("SELECT id, created_at FROM tag_protection_rules WHERE id = ?", (rule_id,))
    params = (
        rule_id,
        body.name.strip(),
        body.repo_pattern.strip(),
        body.tag_pattern.strip(),
        body.environment.strip() or "*",
        1 if body.enabled else 0,
        body.reason or "",
        existing["created_at"] if existing else now,
        now,
    )
    if existing:
        db_execute(
            """
            UPDATE tag_protection_rules
            SET name = ?, repo_pattern = ?, tag_pattern = ?, environment = ?, enabled = ?, reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (params[1], params[2], params[3], params[4], params[5], params[6], params[8], rule_id),
        )
    else:
        db_execute(
            """
            INSERT INTO tag_protection_rules(id, name, repo_pattern, tag_pattern, environment, enabled, reason, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
    audit_log("upsert", "tag_protection_rule", rule_id, {"repo_pattern": body.repo_pattern, "tag_pattern": body.tag_pattern, "environment": body.environment, "enabled": body.enabled})
    return {"ok": True, "rule": public_protection_rule(db_one("SELECT * FROM tag_protection_rules WHERE id = ?", (rule_id,)))}


@app.delete("/api/tag-protection/{rule_id}", dependencies=[Depends(require_write_token)])
def delete_tag_protection_rule(rule_id: str):
    clean_id = validate_slug(rule_id, "rule_id")
    db_execute("DELETE FROM tag_protection_rules WHERE id = ?", (clean_id,))
    audit_log("delete", "tag_protection_rule", clean_id, {})
    return {"ok": True}


@app.get("/api/tag-protection/check")
def check_tag_protection(repo: str, tag: str, environment: str = ""):
    return protection_result(repo, tag, environment)


@app.get("/api/retention-policies")
def list_retention_policies():
    rows = db_rows(
        """
        SELECT id, name, repo_pattern, environment, keep_last, max_age_days, enabled, created_at, updated_at
        FROM retention_policies
        ORDER BY id
        """
    )
    return [public_retention_policy(row) for row in rows]


@app.post("/api/retention-policies", dependencies=[Depends(require_write_token)])
def upsert_retention_policy(body: RetentionPolicyIn):
    policy_id = validate_slug(body.id or slug_candidate(body.name, "retention"), "policy_id")
    now = now_iso()
    existing = db_one("SELECT id, created_at FROM retention_policies WHERE id = ?", (policy_id,))
    params = (
        policy_id,
        body.name.strip(),
        body.repo_pattern.strip(),
        body.environment.strip() or "*",
        body.keep_last,
        body.max_age_days,
        1 if body.enabled else 0,
        existing["created_at"] if existing else now,
        now,
    )
    if existing:
        db_execute(
            """
            UPDATE retention_policies
            SET name = ?, repo_pattern = ?, environment = ?, keep_last = ?, max_age_days = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (params[1], params[2], params[3], params[4], params[5], params[6], params[8], policy_id),
        )
    else:
        db_execute(
            """
            INSERT INTO retention_policies(id, name, repo_pattern, environment, keep_last, max_age_days, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
    audit_log("upsert", "retention_policy", policy_id, {"repo_pattern": body.repo_pattern, "environment": body.environment, "enabled": body.enabled})
    return {"ok": True, "policy": public_retention_policy(retention_policy_row(policy_id))}


@app.delete("/api/retention-policies/{policy_id}", dependencies=[Depends(require_write_token)])
def delete_retention_policy(policy_id: str):
    row = retention_policy_row(policy_id)
    db_execute("DELETE FROM retention_policies WHERE id = ?", (row["id"],))
    audit_log("delete", "retention_policy", row["id"], {})
    return {"ok": True}


@app.post("/api/retention-policies/{policy_id}/dry-run", dependencies=[Depends(require_write_token)])
def dry_run_retention_policy(policy_id: str):
    row = retention_policy_row(policy_id)
    result = retention_dry_run(row)
    audit_log("dry_run", "retention_policy", row["id"], {"candidates": len(result["candidates"]), "skipped_protected": len(result["skipped_protected"])})
    return result


@app.post("/api/retention-policies/{policy_id}/apply", dependencies=[Depends(require_write_token)])
def apply_retention_policy(policy_id: str):
    row = retention_policy_row(policy_id)
    result = retention_dry_run(row)
    created = []
    for candidate in result["candidates"]:
        db_execute(
            """
            INSERT INTO deletion_marks(repo, tag, reason, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo, tag) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at
            """,
            (candidate["repo"], candidate["tag"], f"retention:{row['id']}:{','.join(candidate['reasons'])}", now_iso()),
        )
        created.append(f"{candidate['repo']}:{candidate['tag']}")
    audit_log("apply", "retention_policy", row["id"], {"marked": created, "skipped_protected": len(result["skipped_protected"])})
    return {"ok": True, "marked": created, "dry_run": result}


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


@app.get("/api/storage/search")
async def search_storage(q: str = "", status: str = "", limit: int = 100):
    storage = await get_storage()
    term = q.strip().lower()
    status_filter = status.strip().lower()
    results = []
    for image in storage["images"]:
        for tag in image.get("tags", []):
            ref = f"{image['repo']}:{tag['name']}"
            protection = protection_result(image["repo"], tag["name"])
            item_status = "marked" if tag.get("marked_for_deletion") else ("protected" if protection["protected"] else "active")
            if term and term not in ref.lower() and term not in json.dumps(protection, ensure_ascii=False).lower():
                continue
            if status_filter and status_filter != item_status:
                continue
            results.append(
                {
                    "repo": image["repo"],
                    "tag": tag["name"],
                    "ref": ref,
                    "status": item_status,
                    "deletion_mark": tag.get("deletion_mark"),
                    "protection": protection,
                }
            )
            if len(results) >= min(limit, 500):
                return {"items": results, "registry_error": storage["registry_error"]}
    return {"items": results, "registry_error": storage["registry_error"]}


@app.get("/api/storage/images/{repo:path}/tags/{tag}")
def get_storage_image_detail(repo: str, tag: str):
    clean_repo, clean_tag = validate_repo_tag(repo, tag)
    mark = db_one("SELECT id, repo, tag, reason, created_at FROM deletion_marks WHERE repo = ? AND tag = ?", (clean_repo, clean_tag))
    latest_item = db_one(
        """
        SELECT id, run_id, source, target, copy_target, status, old_digest, new_digest, step, error, started_at, ended_at
        FROM sync_run_items
        WHERE target LIKE ?
        ORDER BY id DESC
        """,
        (f"%/{clean_repo}:{clean_tag}",),
    )
    context = mirror_context_for_tag(clean_repo, clean_tag)
    return {
        "repo": clean_repo,
        "tag": clean_tag,
        "digest": latest_item.get("new_digest") if latest_item else "",
        "source": context.get("source", latest_item.get("source") if latest_item else ""),
        "target": context.get("target", latest_item.get("target") if latest_item else ""),
        "environment": context.get("environment", ""),
        "deletion_mark": mark,
        "protection": protection_result(clean_repo, clean_tag, context.get("environment", "")),
        "latest_sync_item": latest_item,
        "estimated_size_bytes": estimate_repo_size(clean_repo),
    }


@app.get("/api/backup-restore-guide")
def get_backup_restore_guide():
    return {
        "required_items": [
            "config/",
            "data/registry/",
            "data/mirror-registry.db",
            ".env",
            "CREDENTIALS_SECRET_KEY",
        ],
        "backup_commands": [
            "docker compose stop registry",
            "Compress-Archive -Path config,data\\registry,data\\mirror-registry.db,.env -DestinationPath mirror-registry-backup.zip -Force",
            "docker compose start registry",
        ],
        "restore_steps": [
            "还原 config/、data/registry/、SQLite 数据库和 .env。",
            "先设置原始 CREDENTIALS_SECRET_KEY，再启动 panel 进行只读验证。",
            "通过 /api/backup-restore/verify 确认配置、数据库和 registry 数据可读。",
            "确认至少一个已同步 tag 可通过 Registry API 读取后，再启动 sync 写入。",
        ],
        "readonly_verification": [
            "docker compose up -d registry panel",
            "curl http://localhost:8080/api/status",
            "curl http://localhost:5000/v2/_catalog",
        ],
        "tls_entry": {
            "panel": "面板入口建议只暴露 HTTPS，并叠加 Basic Auth 或 SSO。",
            "registry": "Registry /v2/ 入口必须单独配置 HTTPS，跨机器长期使用时不要裸 HTTP 暴露。",
        },
    }


@app.post("/api/backup-restore/verify", dependencies=[Depends(require_write_token)])
def verify_backup_restore_readiness(body: BackupRestoreVerifyIn):
    try:
        db_rows("SELECT 1")
        database_ok = True
    except HTTPException:
        database_ok = False
    checks = [
        {"name": "config", "ok": CONFIG_PATH.exists(), "path": str(CONFIG_PATH)},
        {"name": "database", "ok": database_ok, "path": str(DB_PATH)},
        {"name": "registry_storage", "ok": REGISTRY_STORAGE_PATH.exists(), "path": str(REGISTRY_STORAGE_PATH)},
        {"name": "credentials_secret", "ok": bool(CREDENTIALS_SECRET_KEY.strip()) or not body.require_credentials_secret, "path": "CREDENTIALS_SECRET_KEY"},
    ]
    ok_status = all(item["ok"] for item in checks)
    audit_log("verify", "backup_restore", "readiness", {"ok": ok_status, "failed": [item["name"] for item in checks if not item["ok"]]})
    return {"ok": ok_status, "checks": checks, "guide": get_backup_restore_guide()}


@app.post("/api/storage/delete-mark", dependencies=[Depends(require_write_token)])
def mark_image_for_delete(body: StorageDeleteMarkIn):
    repo, tag = validate_repo_tag(body.repo, body.tag)
    protection = assert_tag_mutation_allowed(repo, tag, "delete-mark")
    mark_id = db_execute(
        """
        INSERT INTO deletion_marks(repo, tag, reason, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(repo, tag) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at
        """,
        (repo, tag, body.reason or "", now_iso()),
    )
    audit_log("mark_delete", "image", f"{repo}:{tag}", {"reason": body.reason or "", "protection": protection})
    return {"ok": True, "id": mark_id, "repo": repo, "tag": tag, "protection": protection}


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
        "tls_reverse_proxy": [
            "管理面板入口和 Registry /v2/ 入口分开配置 server_name 和证书。",
            "Registry /v2/ 长期跨机器使用必须走 HTTPS；仅本机 compose 内部流量可以保留 HTTP。",
            "不要把 sync 服务端口暴露到公网；sync 不需要入站流量。",
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


@app.get("/api/credentials")
def list_credentials():
    rows = db_rows(
        """
        SELECT id, name, registry_host, username, encrypted_secret, scope, created_at, updated_at
        FROM credentials
        ORDER BY registry_host, name
        """
    )
    return [public_credential(row) for row in rows]


@app.post("/api/credentials", dependencies=[Depends(require_write_token)])
def create_credential(body: CredentialIn):
    require_credentials_secret()
    if not body.secret:
        raise HTTPException(400, "创建凭据时必须填写 secret")
    registry_host = normalize_registry_host(body.registry_host)
    credential_id = validate_slug(body.id or slug_candidate(f"{registry_host.replace(':', '-')}-{body.name}"), "credential_id")
    existing = db_one("SELECT id FROM credentials WHERE id = ?", (credential_id,))
    if existing:
        raise HTTPException(409, "凭据 id 已存在")
    now = now_iso()
    encrypted = encrypt_secret(body.secret)
    scope = validate_credential_scope(body.scope)
    db_execute(
        """
        INSERT INTO credentials(id, name, registry_host, username, encrypted_secret, scope, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (credential_id, body.name.strip(), registry_host, body.username.strip(), encrypted, scope, now, now),
    )
    row = credential_row(credential_id)
    audit_log("create", "credential", credential_id, {"registry_host": registry_host, "scope": scope})
    return {"ok": True, "credential": public_credential(row)}


@app.put("/api/credentials/{credential_id}", dependencies=[Depends(require_write_token)])
def update_credential(credential_id: str, body: CredentialIn):
    require_credentials_secret()
    current = credential_row(credential_id)
    registry_host = normalize_registry_host(body.registry_host)
    scope = validate_credential_scope(body.scope)
    encrypted = encrypt_secret(body.secret) if body.secret else current["encrypted_secret"]
    db_execute(
        """
        UPDATE credentials
        SET name = ?, registry_host = ?, username = ?, encrypted_secret = ?, scope = ?, updated_at = ?
        WHERE id = ?
        """,
        (body.name.strip(), registry_host, body.username.strip(), encrypted, scope, now_iso(), current["id"]),
    )
    row = credential_row(current["id"])
    audit_log("update", "credential", current["id"], {"registry_host": registry_host, "scope": scope, "secret_updated": bool(body.secret)})
    return {"ok": True, "credential": public_credential(row)}


@app.delete("/api/credentials/{credential_id}", dependencies=[Depends(require_write_token)])
def delete_credential(credential_id: str):
    row = credential_row(credential_id)
    refs = mirror_credential_references(load_config(), row["id"])
    if refs:
        raise HTTPException(400, f"凭据仍被镜像引用: {', '.join(refs[:5])}")
    db_execute("DELETE FROM credentials WHERE id = ?", (row["id"],))
    audit_log("delete", "credential", row["id"], {"registry_host": row["registry_host"], "scope": row["scope"]})
    return {"ok": True}


@app.post("/api/credentials/{credential_id}/test", dependencies=[Depends(require_write_token)])
async def test_credential(credential_id: str, body: CredentialTestIn | None = None):
    row = credential_row(credential_id)
    secret = decrypt_secret(row["encrypted_secret"])
    registry_url = (body.registry_url if body else None) or f"https://{row['registry_host']}"
    registry_url = registry_url.strip().rstrip("/")
    if "://" not in registry_url:
        registry_url = f"https://{registry_url}"
    parsed = urlparse(registry_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "registry_url 必须是 http:// 或 https:// 地址")
    check_url = f"{registry_url}/v2/"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(check_url, auth=(row["username"], secret))
    except httpx.ConnectError as exc:
        audit_log("test_failed", "credential", row["id"], {"registry_host": row["registry_host"], "reason": "network"})
        return {"ok": False, "status": "network_error", "message": f"无法连接 Registry: {exc.__class__.__name__}"}
    except httpx.TimeoutException:
        audit_log("test_failed", "credential", row["id"], {"registry_host": row["registry_host"], "reason": "timeout"})
        return {"ok": False, "status": "timeout", "message": "连接 Registry 超时"}
    if response.status_code in {200, 401, 403}:
        ok = response.status_code == 200
        status = "ok" if ok else ("authentication_failed" if response.status_code == 401 else "permission_denied")
        audit_log("test_ok" if ok else "test_failed", "credential", row["id"], {"registry_host": row["registry_host"], "status": status})
        return {"ok": ok, "status": status, "http_status": response.status_code}
    audit_log("test_failed", "credential", row["id"], {"registry_host": row["registry_host"], "status": "registry_error", "http_status": response.status_code})
    return {"ok": False, "status": "registry_error", "http_status": response.status_code}


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
