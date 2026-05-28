import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/mirrors.yml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/sync-state.json"))
LOG_PATH = Path(os.getenv("LOG_PATH", "/data/sync.log"))
TRIGGER_PATH = Path(os.getenv("TRIGGER_PATH", "/data/.trigger"))
COMMAND_TIMEOUT_SECONDS = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "900"))

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
    return result


def run_command(step_name: str, cmd: list[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("%s 超时（%d 秒）: %s", step_name, timeout, " ".join(cmd))
        return False
    except OSError as exc:
        logger.error("%s 启动失败: %s", step_name, exc)
        return False

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        logger.error("%s 失败 [%s]: %s", step_name, " ".join(cmd), stderr)
        return False
    return True


def get_remote_digest(image: str) -> str | None:
    try:
        result = subprocess.run(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{image}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.error("获取 digest 超时: %s", image)
        return None
    except OSError as exc:
        logger.error("执行 skopeo 失败: %s", exc)
        return None

    if result.returncode == 0:
        digest = result.stdout.strip()
        return digest or None

    logger.warning("skopeo 返回错误 %s: %s", image, result.stderr.strip())
    return None


def pull_and_push(source: str, target: str) -> bool:
    logger.info("开始同步: %s -> %s", source, target)
    steps = [
        ("docker pull", ["docker", "pull", source]),
        ("docker tag", ["docker", "tag", source, target]),
        ("docker push", ["docker", "push", target]),
    ]
    for step_name, cmd in steps:
        if not run_command(step_name, cmd):
            logger.error("同步失败: %s -> %s，失败步骤: %s", source, target, step_name)
            return False
    cleanup_local_tags(source, target)
    logger.info("同步完成: %s", target)
    return True


def cleanup_local_tags(source: str, target: str) -> None:
    for image in dict.fromkeys([source, target]):
        if not run_command("docker rmi", ["docker", "rmi", image], timeout=120):
            logger.warning("本地镜像清理失败，已忽略: %s", image)


def sync_all(reason: str = "scheduled") -> None:
    if sync_lock.locked():
        logger.warning("已有同步任务正在运行，本次触发将排队等待: %s", reason)

    with sync_lock:
        logger.info("===== 开始检查镜像更新（%s）=====", reason)
        config = load_config()
        state = load_state()
        mirrors = valid_mirrors(config)
        updated = 0
        failed = 0

        if not mirrors:
            logger.info("镜像列表为空，跳过")
            return

        for mirror in mirrors:
            source = mirror["source"]
            target = mirror["target"]
            logger.info("检查镜像: %s", source)

            remote = get_remote_digest(source)
            if not remote:
                failed += 1
                logger.warning("跳过（无法获取 digest）: %s", source)
                continue

            cached = state.get(source)
            if remote == cached:
                logger.info("无更新: %s", source)
                continue

            short_old = (cached[:19] + "...") if cached else "新镜像"
            short_new = remote[:19] + "..."
            logger.info("发现更新: %s  %s -> %s", source, short_old, short_new)

            if pull_and_push(source, target):
                state[source] = remote
                save_state(state)
                updated += 1
            else:
                failed += 1

        logger.info("===== 检查完成，本次更新 %d 个镜像，失败 %d 个 =====", updated, failed)


def check_trigger() -> None:
    if TRIGGER_PATH.exists():
        logger.info("收到手动同步触发")
        TRIGGER_PATH.unlink(missing_ok=True)
        sync_all("manual")


if __name__ == "__main__":
    config = load_config()
    interval = int(config.get("settings", {}).get("check_interval_minutes", 30))

    logger.info("同步服务启动，调度间隔: %d 分钟", interval)
    sync_all("startup")

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(sync_all, "interval", minutes=interval, id="auto_sync")
    scheduler.add_job(check_trigger, "interval", seconds=10, id="trigger_poll")
    scheduler.start()
