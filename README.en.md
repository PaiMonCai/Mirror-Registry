# Mirror Registry

[中文文档](README.md)

Single-node private Docker registry with a lightweight management panel and scheduled image synchronization.

## What It Runs

- `registry`: official `registry:2`, storing image layers under `data/registry`.
- `panel`: FastAPI API plus the static web panel on port `8080`.
- `sync`: Python worker that checks upstream image digests and mirrors changed images into the local registry.

## Production Deployment

Production servers do not build the `panel` or `sync` images locally. They pull published images from GHCR:

```powershell
Copy-Item .env.example .env
docker compose pull
docker compose up -d
docker compose ps
```

Open `http://localhost:8080`.

The default write API token is `change-me`. Set a real token in `.env` before exposing the panel:

```dotenv
PANEL_TOKEN=replace-with-a-long-random-token
MIRROR_REGISTRY_IMAGE_TAG=latest
APP_VERSION=v2
SYNC_RETRY_COUNT=2
SKOPEO_COPY_ALL=1
SKOPEO_DEST_TLS_VERIFY=false
```

`MIRROR_REGISTRY_IMAGE_TAG` defaults to `latest`. To pin a release, set it to a specific tag:

```dotenv
MIRROR_REGISTRY_IMAGE_TAG=v1.0.0
```

The panel stores the token in browser local storage and sends it as a Bearer token for write operations.

## v2 Operations

- `sync` uses `skopeo copy` and no longer depends on host Docker CLI or `/var/run/docker.sock`.
- Runtime data is stored in SQLite by default: `data/mirror-registry.db`.
- The panel has a sync runs view for each run and per-image result.
- The panel has a diagnostics view for Registry, config, data, SQLite, current image tag, app version, and sync heartbeat checks.
- The UI defaults to a light operations theme. Dark theme and write token preferences are stored in browser local storage.

## Local Development

Use the development compose file when you need to build source images locally:

```powershell
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml ps
```

## Configuration

Edit `config/mirrors.yml` or use the panel:

```yaml
mirrors:
  - source: docker.io/library/nginx:latest
    target: localhost:5000/library/nginx:latest

settings:
  check_interval_minutes: 30
  registry_url: http://registry:5000
```

After changing `check_interval_minutes`, restart the sync service:

```powershell
docker compose restart sync
```

## Local Checks

```powershell
python -m pip install -r requirements-dev.txt
python scripts\verify.py
.\scripts\check-runtime.ps1
python -m pytest
docker compose config
docker compose -f docker-compose.dev.yml config
```

`sync` needs `skopeo` at runtime. The default target Registry inside Compose is `registry:5000`; when config uses `localhost:5000/...`, sync rewrites that target to the internal address for copy operations.

## Development Images

Development images are built and pushed by GitHub Actions. Run the local script to push the current branch and dispatch the `Dev Images` workflow:

```powershell
.\scripts\build-dev-images.ps1
```

Optional overrides:

```powershell
$env:MIRROR_REGISTRY_DEV_TAG="dev"
$env:MIRROR_REGISTRY_DEV_REF="main"
$env:MIRROR_REGISTRY_DEV_REMOTE="origin"
.\scripts\build-dev-images.ps1
```

The script requires GitHub CLI:

```powershell
gh auth login
```

It refuses to run with uncommitted changes because GitHub Actions can only build commits available on GitHub.

The workflow publishes linux/amd64 dev images to GHCR:

- `ghcr.io/paimoncai/mirror-registry-panel:dev`
- `ghcr.io/paimoncai/mirror-registry-panel:dev-<sha>`
- `ghcr.io/paimoncai/mirror-registry-sync:dev`
- `ghcr.io/paimoncai/mirror-registry-sync:dev-<sha>`

## Release Images

Release images are built and published by GitHub Actions only when a tag matching `v*` is pushed:

```powershell
git tag v1.0.0
git push origin v1.0.0
```

The workflow publishes linux/amd64 images to GHCR:

- `ghcr.io/paimoncai/mirror-registry-panel:<tag>`
- `ghcr.io/paimoncai/mirror-registry-panel:latest`
- `ghcr.io/paimoncai/mirror-registry-sync:<tag>`
- `ghcr.io/paimoncai/mirror-registry-sync:latest`
