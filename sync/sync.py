import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/mirrors.yml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/sync-state.json"))
LOG_PATH = Path(os.getenv("LOG_PATH", "/data/sync.log"))
TRIGGER_PATH = Path(os.getenv("TRIGGER_PATH", "/data/.trigger"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/mirror-registry.db")
COMMAND_TIMEOUT_SECONDS = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "900"))
SYNC_ENGINE = os.getenv("SYNC_ENGINE", "skopeo")
SYNC_RETRY_COUNT = int(os.getenv("SYNC_RETRY_COUNT", "2"))
SKOPEO_COPY_ALL = os.getenv("SKOPEO_COPY_ALL", "1") != "0"
SKOPEO_SRC_TLS_VERIFY = os.getenv("SKOPEO_SRC_TLS_VERIFY", "true").lower()
SKOPEO_DEST_TLS_VERIFY = os.getenv("SKOPEO_DEST_TLS_VERIFY", "false").lower()
SKOPEO_AUTHFILE = os.getenv("SKOPEO_AUTHFILE", "").strip()
SYNC_TARGET_REGISTRY = os.getenv("SYNC_TARGET_REGISTRY", "registry:5000").strip()
LOCAL_REGISTRY_ALIASES = [
    item.strip()
    for item in os.getenv("LOCAL_REGISTRY_ALIASES", "localhost:5000,127.0.0.1:5000").split(",")
    if item.strip()
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


def db_write(sql: str, params: tuple = ()) -> int:
    try:
        with connect_db() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.lastrowid or 0)
    except sqlite3.Error as exc:
        logger.warning("SQLite 写入失败: %s", exc)
        return 0


def set_runtime_state(key: str, value: str) -> None:
    db_write(
        """
        INSERT INTO runtime_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def record_event(level: str, message: str, run_id: int | None = None, source: str = "", target: str = "") -> None:
    db_write(
        "INSERT INTO log_events(created_at, level, run_id, source, target, message) VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), level, run_id, source, target, message),
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
        return {"mirrors": [], "settings": {"check_interval_minutes": 30}}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.error("读取配置失败: %s", exc)
        return {"mirrors": [], "settings": {"check_interval_minutes": 30}}
    config.setdefault("mirrors", [])
    config.setdefault("settings", {})
    return config


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
    for index, item in enumerate(config.get("mirrors", []), start=1):
        if not isinstance(item, dict):
            logger.error("第 %d 条镜像配置不是对象，已跳过", index)
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if not source or not target:
            logger.error("第 %d 条镜像配置缺少 source 或 target，已跳过", index)
            continue
        result.append({"source": source, "target": target})
        upsert_mirror(source, target)
    return result


def run_command(step_name: str, cmd: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        message = f"{step_name} 超时（{timeout} 秒）: {' '.join(cmd)}"
        logger.error(message)
        return False, message
    except OSError as exc:
        message = f"{step_name} 启动失败: {exc}"
        logger.error(message)
        return False, message

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        message = f"{step_name} 失败 [{' '.join(cmd)}]: {stderr}"
        logger.error(message)
        return False, message
    return True, ""


def inspect_remote_digest(image: str) -> tuple[str | None, str]:
    try:
        result = subprocess.run(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{image}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
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


def build_skopeo_copy_command(source: str, copy_target: str) -> list[str]:
    cmd = [
        "skopeo",
        "copy",
        f"--src-tls-verify={SKOPEO_SRC_TLS_VERIFY}",
        f"--dest-tls-verify={SKOPEO_DEST_TLS_VERIFY}",
    ]
    if SKOPEO_COPY_ALL:
        cmd.append("--all")
    if SKOPEO_AUTHFILE:
        cmd.extend(["--authfile", SKOPEO_AUTHFILE])
    cmd.extend([f"docker://{source}", f"docker://{copy_target}"])
    return cmd


def copy_image(source: str, target: str) -> tuple[bool, str, str]:
    copy_target = resolve_copy_target(target)
    cmd = build_skopeo_copy_command(source, copy_target)
    for attempt in range(1, SYNC_RETRY_COUNT + 2):
        logger.info("skopeo copy 尝试 %d/%d: %s -> %s", attempt, SYNC_RETRY_COUNT + 1, source, copy_target)
        ok, error = run_command("copy", cmd)
        if ok:
            return True, copy_target, ""
        if attempt <= SYNC_RETRY_COUNT:
            logger.warning("copy 失败，将重试: %s", error)
            time.sleep(min(2 * attempt, 10))
    return False, copy_target, error


def pull_and_push(source: str, target: str) -> bool:
    ok, _, _ = copy_image(source, target)
    return ok


def cleanup_local_tags(source: str, target: str) -> None:
    logger.info("skopeo copy 不产生本地 Docker tag，跳过本地镜像清理: %s -> %s", source, target)


def update_heartbeat(interval: int | None = None) -> None:
    skopeo_path = shutil.which("skopeo") or ""
    set_runtime_state("sync_engine", SYNC_ENGINE)
    set_runtime_state("skopeo_available", "true" if skopeo_path else "false")
    set_runtime_state("skopeo_path", skopeo_path)
    set_runtime_state("last_heartbeat", now_iso())
    if interval is not None:
        set_runtime_state("check_interval_minutes", str(interval))
        set_runtime_state("next_run_at", (datetime.now(timezone.utc) + timedelta(minutes=interval)).replace(microsecond=0).isoformat())


def sync_all(reason: str = "scheduled", only_source: str | None = None) -> None:
    if sync_lock.locked():
        logger.warning("已有同步任务正在运行，本次触发将排队等待: %s", reason)

    with sync_lock:
        run_id = create_run(reason, only_source)
        set_runtime_state("sync_running", "true")
        set_runtime_state("sync_reason", reason)
        set_runtime_state("last_started_at", now_iso())
        update_heartbeat()

        logger.info("===== 开始检查镜像更新（%s）=====", reason)
        record_event("INFO", f"开始检查镜像更新（{reason}）", run_id)
        config = load_config()
        state = load_state()
        mirrors = valid_mirrors(config)
        if only_source:
            mirrors = [mirror for mirror in mirrors if mirror["source"] == only_source]

        total = len(mirrors)
        updated = 0
        skipped = 0
        failed = 0

        if not mirrors:
            logger.info("镜像列表为空，跳过")
            record_event("INFO", "镜像列表为空，跳过", run_id)
            update_run(run_id, "completed", 0, 0, 0, 0, "镜像列表为空")
            set_runtime_state("sync_running", "false")
            set_runtime_state("last_finished_at", now_iso())
            return

        try:
            for mirror in mirrors:
                started_at = time.monotonic()
                source = mirror["source"]
                target = mirror["target"]
                cached = state.get(source)
                item_id = create_run_item(run_id, source, target, cached)

                logger.info("检查镜像: %s", source)
                record_event("INFO", f"检查镜像: {source}", run_id, source, target)

                remote, error = inspect_remote_digest(source)
                if not remote:
                    failed += 1
                    logger.warning("跳过（无法获取 digest）: %s", source)
                    record_event("WARNING", f"无法获取 digest: {error}", run_id, source, target)
                    update_run_item(item_id, "failed", step="inspect", error=error, started_at_monotonic=started_at)
                    continue

                if remote == cached:
                    skipped += 1
                    logger.info("无更新: %s", source)
                    record_event("INFO", "digest 未变化，跳过同步", run_id, source, target)
                    update_run_item(item_id, "skipped", new_digest=remote, step="inspect", started_at_monotonic=started_at)
                    upsert_mirror(source, target, remote)
                    continue

                short_old = (cached[:19] + "...") if cached else "新镜像"
                short_new = remote[:19] + "..."
                logger.info("发现更新: %s  %s -> %s", source, short_old, short_new)
                record_event("INFO", f"发现更新: {short_old} -> {short_new}", run_id, source, target)

                ok, copy_target, copy_error = copy_image(source, target)
                if ok:
                    state[source] = remote
                    save_state(state)
                    upsert_mirror(source, target, remote)
                    updated += 1
                    logger.info("同步完成: %s", target)
                    record_event("INFO", "同步完成", run_id, source, target)
                    update_run_item(
                        item_id,
                        "success",
                        new_digest=remote,
                        step="copy",
                        copy_target=copy_target,
                        started_at_monotonic=started_at,
                    )
                else:
                    failed += 1
                    logger.error("同步失败: %s -> %s，失败步骤: copy", source, target)
                    record_event("ERROR", f"同步失败: {copy_error}", run_id, source, target)
                    update_run_item(
                        item_id,
                        "failed",
                        new_digest=remote,
                        step="copy",
                        error=copy_error,
                        copy_target=copy_target,
                        started_at_monotonic=started_at,
                    )
        finally:
            status = "failed" if failed else "completed"
            message = f"更新 {updated}，跳过 {skipped}，失败 {failed}"
            update_run(run_id, status, total, updated, skipped, failed, message)
            set_runtime_state("sync_running", "false")
            set_runtime_state("last_finished_at", now_iso())
            logger.info("===== 检查完成，本次更新 %d 个镜像，失败 %d 个 =====", updated, failed)
            record_event("INFO", f"检查完成：{message}", run_id)


def parse_trigger() -> tuple[str, str | None]:
    try:
        payload = json.loads(TRIGGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "manual", None
    reason = str(payload.get("reason") or "manual")
    source = payload.get("source")
    return reason, str(source) if source else None


def check_trigger() -> None:
    if TRIGGER_PATH.exists():
        reason, source = parse_trigger()
        logger.info("收到手动同步触发")
        TRIGGER_PATH.unlink(missing_ok=True)
        sync_all(reason, only_source=source)


if __name__ == "__main__":
    config = load_config()
    interval = int(config.get("settings", {}).get("check_interval_minutes", 30))
    update_heartbeat(interval)

    logger.info("同步服务启动，调度间隔: %d 分钟", interval)
    sync_all("startup")

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(sync_all, "interval", minutes=interval, id="auto_sync")
    scheduler.add_job(check_trigger, "interval", seconds=10, id="trigger_poll")
    scheduler.add_job(lambda: update_heartbeat(interval), "interval", seconds=30, id="heartbeat")
    scheduler.start()
