# Mirror Registry

[English](README.en.md)

单机私有 Docker 镜像仓库，包含轻量管理面板和定时镜像同步服务。适合在内网或个人服务器上缓存、同步常用上游镜像。

## 服务组成

- `registry`：官方 `registry:2`，镜像层数据保存在 `data/registry`。
- `panel`：FastAPI 后端和静态管理面板，默认监听 `8080` 端口。
- `sync`：Python 同步任务，定时检查上游镜像 digest，发现变化后同步到本地 Registry。

## 生产部署

生产服务器直接拉取已发布镜像：

```powershell
Copy-Item .env.example .env
docker compose pull && docker compose up -d

```

启动后打开 `http://localhost:8080`。

默认写入接口令牌是 `change-me`。如果要暴露管理面板，先在 `.env` 中设置强随机令牌：

```dotenv
PANEL_TOKEN=replace-with-a-long-random-token
MIRROR_REGISTRY_IMAGE_TAG=latest
```

`MIRROR_REGISTRY_IMAGE_TAG` 默认是 `latest`。如果要锁定正式版本，可以改成指定 tag：

```dotenv
MIRROR_REGISTRY_IMAGE_TAG=v1.0.0
```

管理面板会把令牌保存在浏览器 local storage 中，并在新增、修改、删除、触发同步等写操作时通过 Bearer token 发送。

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
  - source: docker.io/library/nginx:latest
    target: localhost:5000/library/nginx:latest

settings:
  check_interval_minutes: 30
  registry_url: http://registry:5000
```

修改 `check_interval_minutes` 后，需要重启同步服务让调度间隔生效：

```powershell
docker compose restart sync
```

## 本地校验

```powershell
python -m pip install -r requirements-dev.txt
python scripts\verify.py
.\scripts\check-runtime.ps1
python -m pytest
docker compose config
docker compose -f docker-compose.dev.yml config
```

`sync` 服务运行时需要 Docker CLI、`skopeo`，并且需要访问 `/var/run/docker.sock`。

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
