# Mirror Registry

[English](README.en.md)

单机私有 Docker 镜像仓库，包含轻量管理面板和定时镜像同步服务。适合在内网或个人服务器上缓存、同步常用上游镜像。

## 服务组成

- `registry`：官方 `registry:2`，镜像层数据保存在 `data/registry`。
- `panel`：FastAPI 后端和静态管理面板，默认监听 `8080` 端口。
- `sync`：Python 同步任务，定时检查上游镜像 digest，发现变化后用 `skopeo copy` 同步到本地 Registry。

## 项目结构

- `panel/main.py`：FastAPI ASGI 兼容入口，实际实现委托给 `panel/app.py`。
- `panel/app.py`：管理面板 API、路由注册和后端业务编排。
- `panel/schemas.py`：面板请求模型和字段约束。
- `sync/sync.py`：同步 worker 兼容入口，实际实现委托给 `sync/worker.py`。
- `sync/worker.py`：定时调度、触发器轮询和 `skopeo` 同步执行逻辑。
- `mirror_registry_core/`：panel 和 sync 共享的默认配置等公共能力。

## 生产部署

生产服务器直接拉取已发布镜像，不在服务器上构建项目。部署目录只需要 `docker-compose.yml` 和 `.env`，运行数据由 Docker named volumes 保存：

```powershell
docker compose pull
docker compose up -d
docker compose ps
```

也可以把更新命令合并为一行执行：`docker compose pull && docker compose up -d`。

启动后打开 `http://localhost:8080`。

生产 compose 不再依赖项目内的 `config/` 和 `data/` 文件夹：

- `mirror-registry-config`：保存面板生成的 `mirrors.yml`。
- `mirror-registry-data`：保存 SQLite、日志、触发文件和同步状态。
- `mirror-registry-storage`：保存 Registry 镜像层数据，并只读挂载给面板做存储统计。

首次启动时，面板会在配置卷中自动初始化默认 `busybox` 镜像配置。

面板默认使用账号密码登录，并保留 `PANEL_TOKEN` 作为脚本和自动化调用的兼容入口。如果要暴露管理面板，先在 `.env` 中设置强管理员密码和强随机令牌：

```dotenv
PANEL_TOKEN=replace-with-a-long-random-token
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-strong-admin-password
SESSION_TTL_SECONDS=604800
SESSION_COOKIE_NAME=mirror_registry_session
SESSION_COOKIE_SECURE=false
MIRROR_REGISTRY_IMAGE_TAG=latest
APP_VERSION=v4
DATABASE_URL=sqlite:////data/mirror-registry.db
SYNC_CONCURRENCY=2
SYNC_RETRY_COUNT=2
SYNC_RETRY_BACKOFF_SECONDS=2
DISK_LOW_BYTES=2147483648
NOTIFY_WEBHOOK_URL=
NOTIFY_DEDUPE_SECONDS=1800
SKOPEO_COPY_ALL=1
SKOPEO_DEST_TLS_VERIFY=false
CREDENTIALS_SECRET_KEY=replace-with-a-long-random-secret
```

`SESSION_COOKIE_SECURE=true` 适用于 HTTPS 入口；本机或纯 HTTP 内网测试可保持 `false`。首次启动时，如果数据卷里还没有管理员，面板会用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 初始化单管理员账号。

`MIRROR_REGISTRY_IMAGE_TAG` 默认是 `latest`。如果要锁定正式版本，可以改成指定 tag：

```dotenv
MIRROR_REGISTRY_IMAGE_TAG=v1.0.0
```

浏览器访问使用 HttpOnly session cookie。`PANEL_TOKEN` 不再作为主要前端入口，只用于脚本、CI 或外部自动化通过 Bearer token 调用受保护 API。

### 生产 smoke 验收

生产验收以 `scripts\prod-smoke.ps1` 为准。默认模式只做安全检查和只读探测，不会拉镜像、启动或重启服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prod-smoke.ps1
```

新机器或明确要启动服务时，显式传入 `-StartServices`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prod-smoke.ps1 -StartServices
```

脚本默认按生产门禁处理 `.env`：`PANEL_TOKEN` 不能是默认值，`ADMIN_PASSWORD` 不能为空或占位值，`CREDENTIALS_SECRET_KEY` 必须设置；如果 `PanelUrl` 使用 HTTPS，则 `SESSION_COOKIE_SECURE` 必须为 `true`。本机试跑可用 `-AllowInsecureLocal` 把这些安全项降级为 warning：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prod-smoke.ps1 -AllowInsecureLocal
```

完整 smoke 会检查 Docker Compose 配置、面板登录、Bearer token 自动化入口、Registry `/v2/`、诊断 API、备份恢复只读校验；在 `-StartServices` 且未传 `-SkipSync` 时，还会触发默认镜像同步并确认本地 Registry 中出现 `library/busybox:latest`。如果管理员账号已经在旧数据卷中初始化且密码不同，可通过 `-AdminUsername` 和 `-AdminPassword` 覆盖登录凭据。

### 运维摘要和发布检查

概览页会加载 `/api/ops/summary`，集中展示健康状态、最近同步失败、磁盘状态、删除标记和当前版本。常见认证、TLS、网络、DNS、manifest、磁盘和 `skopeo` 错误会映射为可读原因与建议，原始错误仍保留在任务明细中。

面板「可观测」页会加载 `/api/observability/summary`，展示 24h/7d 同步成功率、失败聚合、同步趋势、磁盘状态、删除标记积压和当前告警。外部脚本可拉取 `/api/observability/metrics` 获取轻量 metrics JSON；告警 webhook 继续由 sync worker 发送，并通过 `NOTIFY_DEDUPE_SECONDS` 控制同类事件去重窗口。

需要给他人排障时，可在概览页导出诊断包，或调用 `/api/ops/diagnostic-bundle`。诊断包包含版本、配置摘要、诊断结果、最近任务、最近失败和事件，但会脱敏 password、token、session cookie、authfile、Authorization 和加密凭据字段。升级说明可通过 `/api/ops/upgrade-guide` 查看，覆盖环境变量、数据卷、备份和兼容性检查。

正式发版前可使用本地 release checklist 阻断缺少版本号、镜像 tag、版本说明、README 或 smoke 结果的发布准备：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release-check.ps1 -Version v1.0.0 -ImageTag v1.0.0 -SmokeResultPath .\smoke-result.txt
```

`ImageTag` 默认不允许使用 `latest`，除非显式传入 `-AllowLatest`。如果只想检查版本说明和 smoke 文件而暂时跳过构建，可传入 `-SkipBuildChecks`；正式发布前仍应运行完整校验。

## 前端工程化与仓库凭据

- 管理面板前端使用 React + Vite + TypeScript 开发，构建产物继续由 FastAPI 静态托管。
- 本地修改前端后运行 `npm.cmd run build`，生产镜像构建会在 Node 阶段执行前端 build，运行阶段不需要 Node.js。
- 面板「仓库凭据」页可加密保存源仓库和目标仓库的用户名 + token/password。
- 凭据支持 host 默认和单条镜像覆盖，匹配优先级为 mirror 覆盖 > host 默认 > 无凭据。
- 生产环境保存凭据前必须设置 `CREDENTIALS_SECRET_KEY`；secret 不回显、不明文导出、不写入日志和审计详情。
- sync 会在执行 `skopeo inspect/copy` 前生成临时 authfile，并在命令结束后清理。
- 面板登录使用单管理员账号和 session cookie；登录成功、失败和退出会写入审计，但不会记录密码或 session token。

## 仓库治理与备份恢复

- 面板「仓库治理」页支持 tag 保护规则，生产环境、正式 release tag 和显式规则命中的 tag 会阻止删除标记、保留策略和自动覆盖。
- 保留策略先执行 dry-run，列出候选 repo/tag、匹配原因和被保护跳过的 tag；应用策略只生成删除标记，不直接删除 manifest。
- 「存储管理」保留搜索和镜像详情 API，可关联 tag 来源、digest、同步任务、删除标记和保护状态。
- 凭据测试会区分认证失败、网络失败、Registry 不可达和权限不足，并保持 token/password 脱敏。
- 备份恢复清单覆盖 `config/`、`data/registry/`、`data/mirror-registry.db`、`.env` 和 `CREDENTIALS_SECRET_KEY`，恢复时先做只读验证再启动 sync。
- 恢复演练可通过面板「仓库治理」页或 `scripts\restore-drill.ps1` 生成只读报告，验证备份包结构、SQLite、Registry 数据目录和凭据主密钥，不会启动 sync。
- 安全指南区分管理面板 HTTPS 入口和 Registry `/v2/` HTTPS 入口，sync 不需要暴露入站端口。

## 跨机器迁移

- 面板提供 `/api/migration/plan`、`/api/migration/package-manifest` 和 `/api/migration/preflight`，用于生成只读迁移向导、备份清单和迁移前检查。
- `scripts\migration-report.ps1` 可在源机器或目标机器输出 JSON 报告，检查 `config/`、`data/registry/`、`data/mirror-registry.db`、`.env`、`CREDENTIALS_SECRET_KEY`、Docker 和磁盘空间。
- 默认迁移流程不会自动替换目标数据卷；先等待 `/api/sync-queue` 清空，再停止 registry、打包数据、还原到目标机器并运行恢复演练。
- 如果旧数据卷的 `CREDENTIALS_SECRET_KEY` 不一致，面板只能读到加密凭据但无法解密，应停止 panel/sync 后恢复原始密钥再重试。

## 自动发布与计划推送

- `Dev Images` workflow 支持手动触发和 nightly 定时触发，定时镜像只发布 `nightly-YYYYMMDD` 和 `dev-<sha>`，不会覆盖正式 `latest`。
- 正式镜像仍只由 `v*` tag 触发，`latest` 继续代表最新正式版本。
- 面板「计划推送」页可创建业务镜像推送策略，默认关闭；cron 使用 UTC，例如 `0 18 * * *` 对应北京时间 02:00。
- 计划推送会显示启用状态、上次运行、下次运行和最近失败原因。
- 手动运行、创建、修改和 sync 执行结果都会写入审计；失败会进入任务历史、文本日志、事件和 webhook。
- 计划推送默认不允许覆盖 `latest`，必须显式勾选允许，并且仍会受到 tag 保护规则约束。

## 同步队列

- 手动同步、单镜像同步、导入后同步、计划推送和失败重试都会进入 SQLite 持久化 `sync_queue`，worker 按优先级消费。
- 面板「同步任务」页展示同步队列，可对 `queued` 任务暂停、恢复、取消，也可对 `completed`、`failed`、`canceled` 任务重放。
- API 可通过 `GET /api/sync-queue` 查看队列，并通过 `/api/sync-queue/{id}/pause`、`resume`、`cancel`、`replay` 控制任务。
- worker 启动时会把未完成的 `running` / `cancel_requested` 任务恢复为可重试队列项，旧 `.trigger` 文件仍会被兼容转换为队列任务。

## 远程 Worker

- 默认单机 `sync` worker 仍直接消费本地 `sync_queue`，同时会把 `WORKER_ID`、`WORKER_NAME`、`WORKER_LABELS` 心跳写入 `workers`。
- 面板「Worker」页和 `GET /api/workers` 可查看本地或远程执行节点、最近心跳、标签、能力和最近领取任务。
- 远程 worker 预留 `WORKER_TOKEN` 最小权限入口，通过 `X-Worker-Token` 调用 `/api/workers/heartbeat`、`/api/workers/claim` 和 `/api/workers/complete`。
- `WORKER_TOKEN` 不授予管理员面板权限；泄露时应在 `.env` 中轮换并重启 panel。

## 镜像体积统计

- 存储页支持后台重算 manifest/blob 统计，默认读取 SQLite 缓存，不在页面请求中执行重型全量扫描。
- manifest 请求会设置 Docker schema2、OCI manifest、OCI index 和 Docker manifest list 的 `Accept` 头。
- tag 展示逻辑体积、去重体积、共享层数量和多架构 platform breakdown。
- 仓库体积按 blob digest 去重，Registry 物理占用单独扫描 `data/registry/docker/registry/v2/blobs/sha256`。
- Registry 暂时不可用时，`/api/storage` 仍会返回删除标记、缓存统计和可读错误。

## v3 管理增强能力

- 并发同步：`sync_concurrency` 默认 `2`，同一目标镜像写入时会加锁，避免并发写入同一个 tag。
- 重试策略：`sync_retry_count` 控制最大重试次数，失败复制使用指数退避；面板可重试失败任务或失败明细。
- 存储管理：面板展示本地 Registry 仓库、tag、估算占用、删除标记和垃圾回收指引。
- 通知能力：配置 `NOTIFY_WEBHOOK_URL` 或面板 webhook 后，会发送同步失败、失败恢复和磁盘空间不足事件。
- 认证增强：后台 API 默认要求账号密码登录；`PANEL_TOKEN` 仅保留为自动化兼容入口，公网暴露前仍建议放在反向代理后，并可叠加 Basic Auth 或可信 IP 限制。
- 导入导出：面板支持镜像列表 JSON 导出、合并导入和覆盖导入，用于备份和恢复。
- 同步预检：面板支持单条和批量预检，默认只读检查镜像配置、凭据、tag 保护和 latest 风险；显式启用远程探测后才访问上游 manifest 和目标 Registry `/v2/`。

## v4 平台化扩展能力

- 多 Registry 目标：`config/mirrors.yml` 支持 `registries`，面板提供 Registry 目标管理入口。
- 多镜像组：`mirror_groups` 可按项目、环境、命名空间和 Registry 组织镜像。
- 分组展示：面板「平台配置」页按项目、环境、命名空间和镜像组聚合展示。
- 外部数据库配置：默认仍使用 SQLite；可通过 `DATABASE_URL` 或 `settings.database_url` 预留 PostgreSQL/MySQL 配置。
- 审计日志：面板写操作和 sync 关键操作会写入 `audit_logs`，面板「审计」页可查看。
- 扩展评估：面板提供单机、多实例、远程 worker、队列化同步的状态说明；默认部署路径仍是单机 compose。

## v2 运维能力

- `sync` 使用 `skopeo copy` 同步镜像，不再依赖宿主机 Docker CLI，也不再挂载 `/var/run/docker.sock`。
- 运行数据默认写入 SQLite：`data/mirror-registry.db`。
- 面板提供「同步任务」页，展示每轮同步任务和每个镜像的结果。
- 面板提供「验证诊断」页，检查 Registry、配置目录、数据目录、SQLite、当前镜像 tag、版本信息和 sync 心跳。
- UI 默认浅色主题，深色主题保存在浏览器 local storage；登录态通过 HttpOnly cookie 保存。

## 本地开发

本地开发需要构建源码镜像时，使用开发 compose 文件：

```powershell
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml ps
```

## 镜像同步配置

生产部署建议在管理面板中维护镜像配置；首次启动会自动生成默认配置。本地开发时也可以直接编辑 `config/mirrors.yml`：

```yaml
mirrors:
  - source: docker.io/library/busybox:latest
    target: localhost:5000/library/busybox:latest
    registry: local
    group: default
    project: default
    environment: local
    namespace: library

settings:
  check_interval_minutes: 30
  registry_url: http://registry:5000
  database_url: sqlite:////data/mirror-registry.db
  sync_concurrency: 2
  sync_retry_count: 2
```

修改 `check_interval_minutes` 后，需要重启同步服务让调度间隔立即生效：

```powershell
docker compose restart sync
```

## 存储清理

面板里的删除标记只记录清理意图。真正释放 Registry 空间需要按指引删除 manifest 后，再执行垃圾回收：

```powershell
docker compose stop registry
docker compose run --rm registry registry garbage-collect /etc/docker/registry/config.yml
docker compose up -d registry
```

## 本地校验

```powershell
python -m pip install -r requirements-dev.txt
python scripts\verify.py
.\scripts\check-runtime.ps1
npm.cmd run build
python -m pytest
docker compose config
docker compose -f docker-compose.dev.yml config
```

`sync` 服务运行时需要 `skopeo`。默认目标 Registry 是 Compose 内部服务 `registry:5000`；配置里使用 `localhost:5000/...` 时，sync 会在复制时自动改写为内部地址。

## 开发镜像

开发镜像也通过 GitHub Actions 构建和推送。本地只需要执行脚本，把当前分支推送到远端并触发 `Dev Images` workflow：

```powershell
.\scripts\build-dev-images.ps1
```

可选环境变量：

```powershell
$env:MIRROR_REGISTRY_DEV_TAG="dev"
$env:MIRROR_REGISTRY_DEV_REF="main"
$env:MIRROR_REGISTRY_DEV_REMOTE="origin"
.\scripts\build-dev-images.ps1
```

脚本依赖 GitHub CLI：

```powershell
gh auth login
```

脚本会拒绝在存在未提交修改时运行，因为 GitHub Actions 只能构建已经推送到 GitHub 的提交。

workflow 会发布 linux/amd64 开发镜像到 GHCR：

- `ghcr.io/paimoncai/mirror-registry-panel:dev`
- `ghcr.io/paimoncai/mirror-registry-panel:dev-<sha>`
- `ghcr.io/paimoncai/mirror-registry-sync:dev`
- `ghcr.io/paimoncai/mirror-registry-sync:dev-<sha>`

## 正式镜像

正式镜像只在推送匹配 `v*` 的 Git tag 时由 GitHub Actions 构建和发布：

```powershell
git tag v1.0.0
git push origin v1.0.0
```

workflow 会发布 linux/amd64 正式镜像到 GHCR：

- `ghcr.io/paimoncai/mirror-registry-panel:<tag>`
- `ghcr.io/paimoncai/mirror-registry-panel:latest`
- `ghcr.io/paimoncai/mirror-registry-sync:<tag>`
- `ghcr.io/paimoncai/mirror-registry-sync:latest`
