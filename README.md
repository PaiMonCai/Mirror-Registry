# Mirror Registry

[English](README.en.md)

单机私有 Docker 镜像仓库，包含轻量管理面板和定时镜像同步服务。适合在内网或个人服务器上缓存、同步常用上游镜像。

## 服务组成

- `registry`：官方 `registry:2`，镜像层数据保存在 `data/registry`。
- `panel`：FastAPI 后端和静态管理面板，默认监听 `8080` 端口。
- `sync`：Python 同步任务，定时检查上游镜像 digest，发现变化后用 `skopeo copy` 同步到本地 Registry。

## 生产部署

生产服务器直接拉取已发布镜像，不在服务器上构建项目：

```powershell
Copy-Item .env.example .env
docker compose pull
docker compose up -d
docker compose ps
```

也可以把更新命令合并为一行执行：`docker compose pull && docker compose up -d`。

启动后打开 `http://localhost:8080`。

默认写入接口令牌是 `change-me`。如果要暴露管理面板，先在 `.env` 中设置强随机令牌：

```dotenv
PANEL_TOKEN=replace-with-a-long-random-token
MIRROR_REGISTRY_IMAGE_TAG=latest
APP_VERSION=v4
DATABASE_URL=sqlite:////data/mirror-registry.db
SYNC_CONCURRENCY=2
SYNC_RETRY_COUNT=2
SYNC_RETRY_BACKOFF_SECONDS=2
DISK_LOW_BYTES=2147483648
NOTIFY_WEBHOOK_URL=
SKOPEO_COPY_ALL=1
SKOPEO_DEST_TLS_VERIFY=false
CREDENTIALS_SECRET_KEY=
```

`MIRROR_REGISTRY_IMAGE_TAG` 默认是 `latest`。如果要锁定正式版本，可以改成指定 tag：

```dotenv
MIRROR_REGISTRY_IMAGE_TAG=v1.0.0
```

管理面板会把令牌保存在浏览器 local storage 中，并在新增、修改、删除、触发同步等写操作时通过 Bearer token 发送。

## 前端工程化与仓库凭据

- 管理面板前端使用 React + Vite + TypeScript 开发，构建产物继续由 FastAPI 静态托管。
- 本地修改前端后运行 `npm.cmd run build`，生产镜像构建会在 Node 阶段执行前端 build，运行阶段不需要 Node.js。
- 面板「仓库凭据」页可加密保存源仓库和目标仓库的用户名 + token/password。
- 凭据支持 host 默认和单条镜像覆盖，匹配优先级为 mirror 覆盖 > host 默认 > 无凭据。
- 生产环境保存凭据前必须设置 `CREDENTIALS_SECRET_KEY`；secret 不回显、不明文导出、不写入日志和审计详情。
- sync 会在执行 `skopeo inspect/copy` 前生成临时 authfile，并在命令结束后清理。

## 仓库治理与备份恢复

- 面板「仓库治理」页支持 tag 保护规则，生产环境、正式 release tag 和显式规则命中的 tag 会阻止删除标记、保留策略和自动覆盖。
- 保留策略先执行 dry-run，列出候选 repo/tag、匹配原因和被保护跳过的 tag；应用策略只生成删除标记，不直接删除 manifest。
- 「存储管理」保留搜索和镜像详情 API，可关联 tag 来源、digest、同步任务、删除标记和保护状态。
- 凭据测试会区分认证失败、网络失败、Registry 不可达和权限不足，并保持 token/password 脱敏。
- 备份恢复清单覆盖 `config/`、`data/registry/`、`data/mirror-registry.db`、`.env` 和 `CREDENTIALS_SECRET_KEY`，恢复时先做只读验证再启动 sync。
- 安全指南区分管理面板 HTTPS 入口和 Registry `/v2/` HTTPS 入口，sync 不需要暴露入站端口。

## 自动发布与计划推送

- `Dev Images` workflow 支持手动触发和 nightly 定时触发，定时镜像只发布 `nightly-YYYYMMDD` 和 `dev-<sha>`，不会覆盖正式 `latest`。
- 正式镜像仍只由 `v*` tag 触发，`latest` 继续代表最新正式版本。
- 面板「计划推送」页可创建业务镜像推送策略，默认关闭；cron 使用 UTC，例如 `0 18 * * *` 对应北京时间 02:00。
- 计划推送会显示启用状态、上次运行、下次运行和最近失败原因。
- 手动运行、创建、修改和 sync 执行结果都会写入审计；失败会进入任务历史、文本日志、事件和 webhook。
- 计划推送默认不允许覆盖 `latest`，必须显式勾选允许，并且仍会受到 tag 保护规则约束。

## v3 管理增强能力

- 并发同步：`sync_concurrency` 默认 `2`，同一目标镜像写入时会加锁，避免并发写入同一个 tag。
- 重试策略：`sync_retry_count` 控制最大重试次数，失败复制使用指数退避；面板可重试失败任务或失败明细。
- 存储管理：面板展示本地 Registry 仓库、tag、估算占用、删除标记和垃圾回收指引。
- 通知能力：配置 `NOTIFY_WEBHOOK_URL` 或面板 webhook 后，会发送同步失败、失败恢复和磁盘空间不足事件。
- 认证增强：`PANEL_TOKEN` 只保护写接口；公网暴露前建议放在反向代理后，并启用 Basic Auth 或其他登录态。
- 导入导出：面板支持镜像列表 JSON 导出、合并导入和覆盖导入，用于备份和恢复。

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
- UI 默认浅色主题，深色主题和写操作令牌都保存在浏览器 local storage。

## 本地开发

本地开发需要构建源码镜像时，使用开发 compose 文件：

```powershell
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml ps
```

## 镜像同步配置

可以直接编辑 `config/mirrors.yml`，也可以在管理面板中维护：

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
