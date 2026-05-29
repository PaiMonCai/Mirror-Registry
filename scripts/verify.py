import ast
import compileall
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"OK: {message}")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def compose_service_names(compose: str) -> set[str]:
    names: set[str] = set()
    in_services = False
    for line in compose.splitlines():
        if line == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([a-zA-Z0-9_-]+):$", line)
        if match:
            names.add(match.group(1))
    return names


def require_paths() -> None:
    required = [
        "README.md",
        "README.en.md",
        ".dockerignore",
        ".env.example",
        ".github/workflows/dev-images.yml",
        ".github/workflows/release-images.yml",
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "requirements-dev.txt",
        "config/mirrors.yml",
        "config/registry-config.yml",
        "data/.gitkeep",
        "data/registry/.gitkeep",
        "panel/__init__.py",
        "panel/.dockerignore",
        "panel/Dockerfile",
        "panel/app.py",
        "panel/main.py",
        "panel/schemas.py",
        "package.json",
        "package-lock.json",
        "panel/package.json",
        "panel/package-lock.json",
        "panel/frontend/index.html",
        "panel/frontend/vite.config.ts",
        "panel/frontend/tsconfig.json",
        "panel/frontend/src/api.ts",
        "panel/frontend/src/main.tsx",
        "panel/frontend/src/styles.css",
        "panel/frontend/src/vite-env.d.ts",
        "panel/requirements.txt",
        "panel/static/index.html",
        "sync/__init__.py",
        "sync/.dockerignore",
        "sync/Dockerfile",
        "sync/requirements.txt",
        "sync/worker.py",
        "sync/sync.py",
        "mirror_registry_core/__init__.py",
        "mirror_registry_core/config.py",
        "scripts/check-runtime.ps1",
        "scripts/build-dev-images.ps1",
        "tests/test_panel.py",
        "tests/test_sync.py",
    ]
    missing = [path for path in required if not (ROOT / path).exists()]
    if missing:
        fail(f"missing required paths: {', '.join(missing)}")
    ok("target directory structure is present")


def require_no_flattened_prototype_files() -> None:
    old_files = [
        "panel-main.py",
        "panel-index.html",
        "panel-Dockerfile",
        "panel-requirements.txt",
        "sync.py",
    ]
    leftovers = [path for path in old_files if (ROOT / path).exists()]
    if leftovers:
        fail(f"prototype files still exist at root: {', '.join(leftovers)}")
    ok("prototype files were moved into target directories")


def require_python_compiles() -> None:
    paths = [ROOT / "panel", ROOT / "sync", ROOT / "mirror_registry_core", ROOT / "tests", ROOT / "scripts"]
    if not compileall.compile_dir(str(ROOT / "panel"), quiet=1):
        fail("panel Python files do not compile")
    for path in paths[1:]:
        if not compileall.compile_dir(str(path), quiet=1):
            fail(f"{path.relative_to(ROOT)} Python files do not compile")
    ok("Python files compile")


def require_compose_shape() -> None:
    compose = read("docker-compose.yml")
    required_snippets = [
        "image: registry:2",
        "REGISTRY_STORAGE_DELETE_ENABLED: \"true\"",
        "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY: /var/lib/registry",
        'image: "ghcr.io/paimoncai/mirror-registry-panel:${MIRROR_REGISTRY_IMAGE_TAG:-latest}"',
        'image: "ghcr.io/paimoncai/mirror-registry-sync:${MIRROR_REGISTRY_IMAGE_TAG:-latest}"',
        "mirror-registry-config:/config",
        "mirror-registry-data:/data",
        "mirror-registry-storage:/var/lib/registry",
        "mirror-registry-storage:/data/registry:ro",
        "name: mirror-registry-config",
        "name: mirror-registry-data",
        "name: mirror-registry-storage",
        "APP_VERSION: v4",
        "DATABASE_URL: ${DATABASE_URL:-sqlite:////data/mirror-registry.db}",
        "MIRROR_REGISTRY_IMAGE_TAG: ${MIRROR_REGISTRY_IMAGE_TAG:-latest}",
        "SYNC_ENGINE: skopeo",
        "SYNC_CONCURRENCY: ${SYNC_CONCURRENCY:-2}",
        "SYNC_RETRY_BACKOFF_SECONDS: ${SYNC_RETRY_BACKOFF_SECONDS:-2}",
        "DISK_LOW_BYTES: ${DISK_LOW_BYTES:-2147483648}",
        "NOTIFY_WEBHOOK_URL: ${NOTIFY_WEBHOOK_URL:-}",
        "REGISTRY_STORAGE_PATH: /data/registry",
        "SKOPEO_DEST_TLS_VERIFY",
        "PANEL_TOKEN: ${PANEL_TOKEN:-change-me}",
        "ADMIN_USERNAME: ${ADMIN_USERNAME:-admin}",
        "ADMIN_PASSWORD: ${ADMIN_PASSWORD:-}",
        "SESSION_TTL_SECONDS: ${SESSION_TTL_SECONDS:-604800}",
        "SESSION_COOKIE_NAME: ${SESSION_COOKIE_NAME:-mirror_registry_session}",
        "SESSION_COOKIE_SECURE: ${SESSION_COOKIE_SECURE:-false}",
        "COMMAND_TIMEOUT_SECONDS: 900",
        "CREDENTIALS_SECRET_KEY: ${CREDENTIALS_SECRET_KEY:-}",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in compose]
    if missing:
        fail(f"docker-compose.yml missing snippets: {missing}")
    forbidden_snippets = ["build: ./panel", "build: ./sync", "pull_policy: always", "/var/run/docker.sock", "./config", "./data"]
    forbidden = [snippet for snippet in forbidden_snippets if snippet in compose]
    if forbidden:
        fail(f"production docker-compose.yml must pull images, not build locally: {forbidden}")
    service_names = compose_service_names(compose)
    if service_names != {"registry", "panel", "sync"}:
        fail(f"docker-compose.yml service set is wrong: {sorted(service_names)}")
    if "    ports:\n      - \"5000:5000\"" not in compose:
        fail("registry port 5000 mapping missing")
    if "    ports:\n      - \"8080:8080\"" not in compose:
        fail("panel port 8080 mapping missing")
    if "volumes:\n  mirror-registry-config:\n    name: mirror-registry-config\n  mirror-registry-data:\n    name: mirror-registry-data\n  mirror-registry-storage:\n    name: mirror-registry-storage" not in compose:
        fail("production docker-compose.yml must declare named volumes")

    dev_compose = read("docker-compose.dev.yml")
    dev_required_snippets = [
        "image: registry:2",
        "dockerfile: panel/Dockerfile",
        "dockerfile: sync/Dockerfile",
        "./config/registry-config.yml:/etc/docker/registry/config.yml",
        "./config:/config",
        "./data:/data",
        "APP_VERSION: v4",
        "DATABASE_URL: ${DATABASE_URL:-sqlite:////data/mirror-registry.db}",
        "MIRROR_REGISTRY_IMAGE_TAG: ${MIRROR_REGISTRY_IMAGE_TAG:-latest}",
        "SYNC_ENGINE: skopeo",
        "SYNC_CONCURRENCY: ${SYNC_CONCURRENCY:-2}",
        "SYNC_RETRY_BACKOFF_SECONDS: ${SYNC_RETRY_BACKOFF_SECONDS:-2}",
        "DISK_LOW_BYTES: ${DISK_LOW_BYTES:-2147483648}",
        "NOTIFY_WEBHOOK_URL: ${NOTIFY_WEBHOOK_URL:-}",
        "REGISTRY_STORAGE_PATH: /data/registry",
        "SKOPEO_DEST_TLS_VERIFY",
        "PANEL_TOKEN: ${PANEL_TOKEN:-change-me}",
        "ADMIN_USERNAME: ${ADMIN_USERNAME:-admin}",
        "ADMIN_PASSWORD: ${ADMIN_PASSWORD:-}",
        "SESSION_TTL_SECONDS: ${SESSION_TTL_SECONDS:-604800}",
        "SESSION_COOKIE_NAME: ${SESSION_COOKIE_NAME:-mirror_registry_session}",
        "SESSION_COOKIE_SECURE: ${SESSION_COOKIE_SECURE:-false}",
        "COMMAND_TIMEOUT_SECONDS: 900",
        "CREDENTIALS_SECRET_KEY: ${CREDENTIALS_SECRET_KEY:-}",
    ]
    missing_dev = [snippet for snippet in dev_required_snippets if snippet not in dev_compose]
    if missing_dev:
        fail(f"docker-compose.dev.yml missing snippets: {missing_dev}")
    dev_service_names = compose_service_names(dev_compose)
    if dev_service_names != {"registry", "panel", "sync"}:
        fail(f"docker-compose.dev.yml service set is wrong: {sorted(dev_service_names)}")
    ok("compose files separate production image pulls from local development builds")


def require_dockerfile_contexts() -> None:
    checks = {
        "panel": {
            "required": ["requirements.txt", "app.py", "main.py", "schemas.py", "package.json", "frontend/index.html", "frontend/src/main.tsx"],
            "dockerfile_snippets": [
                "FROM node:24-slim AS frontend",
                "RUN npm ci",
                "COPY panel/frontend/ frontend/",
                "RUN npm run build",
                "FROM python:3.12-slim",
                "COPY panel/requirements.txt panel/requirements.txt",
                "COPY mirror_registry_core/ mirror_registry_core/",
                "COPY panel/*.py panel/",
                "COPY --from=frontend /panel/static/ panel/static/",
                'CMD ["uvicorn", "panel.main:app", "--host", "0.0.0.0", "--port", "8080"]',
            ],
            "requirements": [
                "fastapi==0.111.0",
                "uvicorn==0.29.0",
                "pyyaml==6.0.1",
                "httpx==0.27.0",
                "sqlalchemy==2.0.30",
                "pymysql==1.1.1",
                "psycopg[binary]==3.1.19",
                "cryptography==42.0.8",
            ],
        },
        "sync": {
            "required": ["requirements.txt", "worker.py", "sync.py"],
            "dockerfile_snippets": [
                "FROM python:3.12-slim",
                "apt-get install -y --no-install-recommends ca-certificates skopeo",
                "COPY sync/requirements.txt sync/requirements.txt",
                "COPY mirror_registry_core/ mirror_registry_core/",
                "COPY sync/*.py sync/",
                'CMD ["python", "-m", "sync.sync"]',
            ],
            "requirements": [
                "apscheduler==3.10.4",
                "pyyaml==6.0.1",
                "sqlalchemy==2.0.30",
                "pymysql==1.1.1",
                "psycopg[binary]==3.1.19",
                "cryptography==42.0.8",
            ],
        },
    }
    for context, spec in checks.items():
        base = ROOT / context
        dockerfile = read(f"{context}/Dockerfile")
        requirements = read(f"{context}/requirements.txt")
        for relative in spec["required"]:
            if not (base / relative).exists():
                fail(f"{context}/Dockerfile COPY source is missing: {relative}")
        for snippet in spec["dockerfile_snippets"]:
            if snippet not in dockerfile:
                fail(f"{context}/Dockerfile missing {snippet!r}")
        for package in spec["requirements"]:
            if package not in requirements:
                fail(f"{context}/requirements.txt missing pinned package {package!r}")
        dockerignore = read(f"{context}/.dockerignore")
        for snippet in ["__pycache__/", "*.py[cod]", ".pytest_cache/"]:
            if snippet not in dockerignore:
                fail(f"{context}/.dockerignore missing {snippet!r}")
    ok("Dockerfile contexts and pinned requirements are consistent")


def require_config_shape() -> None:
    mirrors = read("config/mirrors.yml")
    registry = read("config/registry-config.yml")
    for snippet in [
        "mirrors:",
        "registries:",
        "mirror_groups:",
        "source: docker.io/library/busybox:latest",
        "target: localhost:5000/library/busybox:latest",
        "registry: local",
        "group: default",
        "project: default",
        "environment: local",
        "namespace: library",
        "check_interval_minutes: 30",
        "registry_url: http://registry:5000",
        "database_url: sqlite:////data/mirror-registry.db",
        "sync_concurrency: 2",
        "sync_retry_count: 2",
    ]:
        if snippet not in mirrors:
            fail(f"config/mirrors.yml missing {snippet!r}")
    for snippet in ["version: 0.1", "rootdirectory: /var/lib/registry", "enabled: true", "addr: :5000"]:
        if snippet not in registry:
            fail(f"config/registry-config.yml missing {snippet!r}")
    ok("default configuration files are usable")


def require_panel_features() -> None:
    source = read("panel/app.py")
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in [
        "require_write_token",
        "atomic_write_text",
        "validate_image_ref",
        "save_config",
        "save_state",
        "run_diagnostics",
        "list_sync_runs",
        "trigger_mirror_sync",
        "connect_db",
        "export_mirrors",
        "import_mirrors",
        "retry_sync_run",
        "retry_sync_run_item",
        "get_storage",
        "mark_image_for_delete",
        "get_security_guide",
        "get_platform",
        "list_grouped_mirrors",
        "upsert_registry",
        "upsert_mirror_group",
        "list_audit_logs",
        "get_database_guide",
        "audit_log",
        "create_credential",
        "update_credential",
        "delete_credential",
        "test_credential",
        "encrypt_secret",
        "decrypt_secret",
        "list_tag_protection_rules",
        "upsert_tag_protection_rule",
        "dry_run_retention_policy",
        "apply_retention_policy",
        "get_backup_restore_guide",
        "verify_backup_restore_readiness",
        "search_storage",
        "get_storage_image_detail",
        "list_schedules",
        "upsert_schedule",
        "run_schedule",
        "assert_scheduled_policy_allowed",
        "compute_manifest_stats",
        "fetch_manifest",
        "recalculate_storage_stats",
        "queue_storage_stats_recalculate",
    ]:
        if name not in function_names:
            fail(f"panel/app.py missing function {name}")

    required_snippets = [
        "PANEL_TOKEN",
        "LoginIn",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "SESSION_COOKIE_NAME",
        "SESSION_TTL_SECONDS",
        "pbkdf2_sha256",
        "hashlib.pbkdf2_hmac",
        "users",
        "sessions",
        "ensure_admin_user",
        "authenticate_request",
        "@app.middleware(\"http\")",
        "@app.post(\"/api/auth/login\")",
        "@app.get(\"/api/auth/me\")",
        "@app.post(\"/api/auth/logout\")",
        "httponly=True",
        "samesite=\"lax\"",
        "from mirror_registry_core.config import default_config",
        "Depends(require_write_token)",
        "DATABASE_URL",
        "APP_VERSION",
        "IMAGE_TAG",
        "版本信息",
        "sync_runs",
        "sync_run_items",
        "log_events",
        "@app.get(\"/api/diagnostics\")",
        "@app.get(\"/api/sync-runs\")",
        "@app.post(\"/api/mirrors/{index}/sync\"",
        "IMAGE_REF_RE",
        "settings.get(\"registry_url\")",
        "min(lines, 1000)",
        "response.raise_for_status()",
        "StaticFiles(directory=STATIC_DIR",
        "sync_concurrency",
        "sync_retry_count",
        "notify_webhook_url",
        "deletion_marks",
        "@app.get(\"/api/mirrors/export\")",
        "@app.post(\"/api/mirrors/import\"",
        "@app.post(\"/api/sync-runs/{run_id}/retry\"",
        "@app.post(\"/api/sync-run-items/{item_id}/retry\"",
        "@app.get(\"/api/storage\")",
        "@app.post(\"/api/storage/delete-mark\"",
        "@app.get(\"/api/security-guide\")",
        "@app.get(\"/api/platform\")",
        "@app.get(\"/api/platform/groups\")",
        "@app.post(\"/api/registries\"",
        "@app.post(\"/api/mirror-groups\"",
        "@app.get(\"/api/audit-logs\")",
        "@app.get(\"/api/database-guide\")",
        "registries",
        "mirror_groups",
        "audit_logs",
        "database_backend",
        "CREDENTIALS_SECRET_KEY",
        "credentials",
        "@app.get(\"/api/credentials\")",
        "@app.post(\"/api/credentials\"",
        "@app.get(\"/api/tag-protection\")",
        "@app.post(\"/api/retention-policies/{policy_id}/dry-run\"",
        "@app.get(\"/api/backup-restore-guide\")",
        "protected_environment",
        "release_tag",
        "CREDENTIALS_SECRET_KEY",
        "@app.get(\"/api/schedules\")",
        "scheduled_push_policies",
        "计划推送默认不允许覆盖 latest",
        "storage_stats",
        "MANIFEST_ACCEPT",
        "Docker-Content-Digest",
        "@app.post(\"/api/storage/stats/recalculate\"",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in source]
    if missing:
        fail(f"panel/app.py missing security/reliability snippets: {missing}")
    ok("panel backend has v1 security and reliability boundaries")


def require_sync_features() -> None:
    source = read("sync/worker.py")
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in [
        "atomic_write_text",
        "valid_mirrors",
        "run_command",
        "copy_image",
        "process_mirror",
        "notify_webhook",
        "check_disk_space",
        "get_target_lock",
        "audit_log",
        "group_map",
        "resolve_copy_target",
        "build_skopeo_copy_command",
        "create_run",
        "update_run_item",
        "pull_and_push",
        "cleanup_local_tags",
        "sync_all",
        "check_trigger",
        "load_credentials",
        "find_credential",
        "write_temp_authfile",
        "remove_temp_authfile",
        "build_skopeo_inspect_command",
        "load_tag_protection_rules",
        "tag_protection_reasons",
        "image_repo_tag",
        "load_due_scheduled_policies",
        "process_scheduled_policy",
        "check_scheduled_policies",
    ]:
        if name not in function_names:
            fail(f"sync/worker.py missing function {name}")

    required_snippets = [
        "COMMAND_TIMEOUT_SECONDS",
        "from mirror_registry_core.config import default_config",
        "DATABASE_URL",
        "SYNC_ENGINE",
        "APP_VERSION",
        "IMAGE_TAG",
        "skopeo",
        "copy",
        "--all",
        "SYNC_TARGET_REGISTRY",
        "sync_runs",
        "sync_run_items",
        "runtime_state",
        "log_events",
        "with sync_lock:",
        "排队等待",
        "subprocess.TimeoutExpired",
        ".invalid-",
        "失败步骤",
        "save_state(state)",
        "ThreadPoolExecutor",
        "as_completed",
        "SYNC_CONCURRENCY",
        "SYNC_RETRY_BACKOFF_SECONDS",
        "NOTIFY_WEBHOOK_URL",
        "DISK_LOW_BYTES",
        "deletion_marks",
        "audit_logs",
        "database_backend",
        "mirror_group_count",
        "target_locks",
        "parse_trigger",
        "CREDENTIALS_SECRET_KEY",
        "credentials",
        "--authfile",
        "redact_command",
        "tag_protection_rules",
        "copy_blocked",
        "tag_written",
        "scheduled_push_policies",
        "scheduled-policy:",
        "scheduled_push_failed",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in source]
    if missing:
        fail(f"sync/worker.py missing reliability snippets: {missing}")
    forbidden = ["docker pull", "docker tag", "docker push", "docker rmi"]
    bad = [snippet for snippet in forbidden if snippet in source]
    if bad:
        fail(f"sync/worker.py must use skopeo copy instead of Docker CLI: {bad}")
    ok("sync service has v2 skopeo copy, SQLite run history, timeout, and anti-reentry")


def require_frontend_features() -> None:
    source = read("panel/frontend/src/main.tsx")
    api_source = read("panel/frontend/src/api.ts")
    package_json = read("panel/package.json")
    static_index = read("panel/static/index.html")
    required_snippets = [
        "React",
        "createRoot",
        "createApiClient",
        "mirrorRegistryTheme",
        "loadAuth",
        "/auth/me",
        "/auth/login",
        "/auth/logout",
        "LoginScreen",
        "session-card",
        "user-pill",
        "formatMB",
        "breakable",
        "num",
        "loadDiagnostics",
        "loadRuns",
        "loadStorage",
        "loadSecurity",
        "loadPlatform",
        "loadAudit",
        "loadSettings",
        "loadCredentials",
        "source_credential_id",
        "target_credential_id",
        "/credentials",
        "/tag-protection",
        "/retention-policies",
        "/backup-restore-guide",
        "仓库治理",
        "/schedules",
        "计划推送",
        "/storage/stats/recalculate",
        "逻辑体积",
        "去重体积",
        "共享层",
        "sync_concurrency",
        "Webhook URL",
        "平台配置",
        "审计日志",
        "DATABASE_URL",
        "删除标记",
        "垃圾回收",
        "公网暴露安全边界",
        "验证诊断",
        "同步任务",
        "using_default_token",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in source]
    if missing:
        fail(f"panel frontend missing snippets: {missing}")

    for snippet in [
        "Authorization",
        "Bearer",
        "Content-Type",
        "credentials: 'same-origin'",
        "ApiError",
    ]:
        if snippet not in api_source:
            fail(f"panel API client missing {snippet!r}")
    for snippet in [
        '"vite"',
        '"typescript"',
        '"react"',
        '"react-dom"',
        '"lucide-react"',
        '"build"',
    ]:
        if snippet not in package_json:
            fail(f"panel/package.json missing {snippet!r}")
    if '<div id="root"></div>' not in static_index:
        fail("Vite build output is not present in panel/static/index.html")
    ok("React/Vite frontend uses session login and keeps Bearer automation support in the API client")


def require_tests_and_docs() -> None:
    tests = read("tests/test_panel.py") + "\n" + read("tests/test_sync.py")
    for snippet in [
        "Authorization",
        "/api/status",
        "/api/sync",
        "/api/diagnostics",
        "/api/sync-runs",
        "/api/mirrors/export",
        "/api/mirrors/import",
        "/api/storage",
        "/api/security-guide",
        "/api/platform",
        "/api/audit-logs",
        "/api/database-guide",
        "save_state",
        "valid_mirrors",
        "build_skopeo_copy_command",
        "parse_trigger",
        "/api/credentials",
        "write_temp_authfile",
        "CREDENTIALS_SECRET_KEY",
        "/api/tag-protection",
        "/api/retention-policies",
        "/api/backup-restore-guide",
        "tag_written",
        "/api/schedules",
        "scheduled-policy:",
        "compute_manifest_stats",
        "/api/storage",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "test_unauthenticated_api_requires_login",
        "test_bearer_token_remains_automation_compatible",
        "test_session_expiry_requires_login_again",
    ]:
        if snippet not in tests:
            fail(f"tests missing coverage hint {snippet!r}")
    readme = read("README.md")
    for snippet in [
        "docker compose pull",
        "docker compose up -d",
        "docker compose pull && docker compose up -d",
        "docker compose -f docker-compose.dev.yml up -d --build",
        "MIRROR_REGISTRY_IMAGE_TAG=v1.0.0",
        "skopeo copy",
        "data/mirror-registry.db",
        "验证诊断",
        "当前镜像 tag",
        "v3 管理增强能力",
        "v4 平台化扩展能力",
        "sync_concurrency",
        "DATABASE_URL",
        "registries",
        "mirror_groups",
        "audit_logs",
        "NOTIFY_WEBHOOK_URL",
        "删除标记",
        "Basic Auth",
        "导入导出",
        "仓库治理",
        "CREDENTIALS_SECRET_KEY",
        "计划推送",
        "镜像体积统计",
        "PANEL_TOKEN",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "SESSION_TTL_SECONDS",
        "SESSION_COOKIE_SECURE",
        "账号密码登录",
        "HttpOnly session cookie",
        "python scripts\\verify.py",
        ".\\scripts\\check-runtime.ps1",
        "python -m pytest",
        "docker compose config",
        "docker compose -f docker-compose.dev.yml config",
    ]:
        if snippet not in readme:
            fail(f"README.md missing {snippet!r}")
    readme_en = read("README.en.md")
    for snippet in [
        "Single-node private Docker registry",
        "Production Deployment",
        "v3 Management",
        "v4 Platform Extensions",
        "v2 Operations",
        "skopeo copy",
        "data/mirror-registry.db",
        "current image tag",
        "docker compose pull",
        "docker compose -f docker-compose.dev.yml up -d --build",
        "Development Images",
        "Release Images",
        "DATABASE_URL",
        "audit_logs",
        "NOTIFY_WEBHOOK_URL",
        "Basic Auth",
        "Import/export",
        "Repository Governance",
        "CREDENTIALS_SECRET_KEY",
        "Scheduled Push",
        "Image Size Statistics",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "SESSION_TTL_SECONDS",
        "SESSION_COOKIE_SECURE",
        "account/password login",
        "HttpOnly session cookie",
    ]:
        if snippet not in readme_en:
            fail(f"README.en.md missing {snippet!r}")
    env_example = read(".env.example")
    for snippet in [
        "PANEL_TOKEN=",
        "ADMIN_USERNAME=",
        "ADMIN_PASSWORD=",
        "SESSION_TTL_SECONDS=604800",
        "SESSION_COOKIE_NAME=mirror_registry_session",
        "SESSION_COOKIE_SECURE=false",
        "MIRROR_REGISTRY_IMAGE_TAG=latest",
        "APP_VERSION=v4",
        "DATABASE_URL=sqlite:////data/mirror-registry.db",
        "SYNC_CONCURRENCY=2",
        "SYNC_RETRY_COUNT=2",
        "SYNC_RETRY_BACKOFF_SECONDS=2",
        "DISK_LOW_BYTES=2147483648",
        "NOTIFY_WEBHOOK_URL=",
        "SKOPEO_COPY_ALL=1",
        "CREDENTIALS_SECRET_KEY=",
    ]:
        if snippet not in env_example:
            fail(f".env.example missing {snippet!r}")
    dev_requirements = read("requirements-dev.txt")
    for snippet in ["-r panel/requirements.txt", "-r sync/requirements.txt", "pytest=="]:
        if snippet not in dev_requirements:
            fail(f"requirements-dev.txt missing {snippet!r}")
    check_script = read("scripts/check-runtime.ps1")
    for snippet in [
        "python scripts\\verify.py",
        "python -m pytest",
        "docker compose config",
        "docker compose -f docker-compose.dev.yml config",
        "npm.cmd run build",
    ]:
        if snippet not in check_script:
            fail(f"scripts/check-runtime.ps1 missing {snippet!r}")
    dev_script = read("scripts/build-dev-images.ps1")
    for snippet in [
        "Get-Command gh",
        "git status --porcelain",
        "MIRROR_REGISTRY_DEV_TAG",
        "MIRROR_REGISTRY_DEV_REF",
        "git push $Remote $Branch",
        "gh workflow run dev-images.yml",
        "ghcr.io/paimoncai/mirror-registry-panel:$Tag",
        "ghcr.io/paimoncai/mirror-registry-sync:$Tag",
    ]:
        if snippet not in dev_script:
            fail(f"scripts/build-dev-images.ps1 missing {snippet!r}")
    ok("tests and README docs cover the v1 operating path")


def require_release_workflow() -> None:
    workflow = read(".github/workflows/release-images.yml")
    required_snippets = [
        "tags:",
        '- "v*"',
        "packages: write",
        "IMAGE_NAMESPACE: paimoncai",
        "docker/login-action@v3",
        "docker/metadata-action@v5",
        "docker/build-push-action@v6",
        "context: .",
        "file: ${{ matrix.dockerfile }}",
        "dockerfile: panel/Dockerfile",
        "dockerfile: sync/Dockerfile",
        "mirror-registry-panel",
        "mirror-registry-sync",
        "platforms: linux/amd64",
        "push: true",
        "type=ref,event=tag",
        "type=raw,value=latest",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in workflow]
    if missing:
        fail(f"release workflow missing snippets: {missing}")
    if "branches:" in workflow:
        fail("release workflow must not publish on branch pushes")
    ok("tag-only release workflow publishes official GHCR images")


def require_dev_workflow() -> None:
    workflow = read(".github/workflows/dev-images.yml")
    required_snippets = [
        "schedule:",
        "cron: \"17 18 * * *\"",
        "workflow_dispatch:",
        "image_tag:",
        "ref_label:",
        "packages: write",
        "IMAGE_NAMESPACE: paimoncai",
        "docker/login-action@v3",
        "docker/build-push-action@v6",
        "context: .",
        "file: ${{ matrix.dockerfile }}",
        "dockerfile: panel/Dockerfile",
        "dockerfile: sync/Dockerfile",
        "mirror-registry-panel",
        "mirror-registry-sync",
        "platforms: linux/amd64",
        "push: true",
        "nightly-$(date -u +%Y%m%d)",
        ":${{ steps.dev_tag.outputs.image_tag }}",
        ":dev-${{ github.sha }}",
    ]
    missing = [snippet for snippet in required_snippets if snippet not in workflow]
    if missing:
        fail(f"dev workflow missing snippets: {missing}")
    if "branches:" in workflow or ":latest" in workflow:
        fail("dev workflow must not publish on branch pushes or overwrite latest")
    ok("manual and scheduled dev workflow publishes GHCR dev images without touching latest")


def main() -> None:
    require_paths()
    require_no_flattened_prototype_files()
    require_python_compiles()
    require_compose_shape()
    require_dockerfile_contexts()
    require_config_shape()
    require_panel_features()
    require_sync_features()
    require_frontend_features()
    require_tests_and_docs()
    require_release_workflow()
    require_dev_workflow()
    print("Verification passed.")


if __name__ == "__main__":
    main()
