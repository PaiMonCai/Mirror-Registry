# Mirror Registry

[中文文档](README.md)

Single-node private Docker registry with a lightweight management panel and scheduled image synchronization.

## What It Runs

- `registry`: official `registry:2`, storing image layers under `data/registry`.
- `panel`: FastAPI API plus the static web panel on port `8080`.
- `sync`: Python worker that checks upstream image digests and mirrors changed images into the local registry with `skopeo copy`.

## Production Deployment

Production servers do not build the `panel` or `sync` images locally. They pull published images from GHCR:

```powershell
Copy-Item .env.example .env
docker compose pull
docker compose up -d
docker compose ps
```

You can also run the update as one command: `docker compose pull && docker compose up -d`.

Open `http://localhost:8080`.

The default write API token is `change-me`. Set a real token in `.env` before exposing the panel:

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

`MIRROR_REGISTRY_IMAGE_TAG` defaults to `latest`. To pin a release, set it to a specific tag:

```dotenv
MIRROR_REGISTRY_IMAGE_TAG=v1.0.0
```

The panel stores the token in browser local storage and sends it as a Bearer token for write operations.

## Frontend Engineering and Registry Credentials

- The panel frontend is built with React + Vite + TypeScript, and FastAPI continues to serve the built static files.
- After frontend edits, run `npm.cmd run build`; production image builds run a Node build stage while the runtime image stays Python-only.
- The Credentials page stores source and target registry username + token/password pairs encrypted.
- Credentials support host defaults and per-mirror overrides. Matching priority is mirror override > host default > no credential.
- Production deployments must set `CREDENTIALS_SECRET_KEY` before saving credentials. Secrets are not echoed, exported in plaintext, logged, or written into audit detail.
- The sync worker creates a temporary authfile for `skopeo inspect/copy` and removes it after the command finishes.

## Repository Governance and Backup Restore

- The Governance page supports tag protection rules. Production environments, release tags, and explicit rules block delete marks, retention policies, and automatic overwrites.
- Retention policies run dry-run first and list candidate repo/tags, matching reasons, and protected skips. Applying a policy creates deletion marks only; it does not delete manifests.
- Storage management has search and detail APIs that connect tag source, digest, sync task, deletion mark, and protection state.
- Credential tests distinguish authentication failures, network failures, unreachable registries, and missing permissions while keeping token/password values redacted.
- The backup checklist covers `config/`, `data/registry/`, `data/mirror-registry.db`, `.env`, and `CREDENTIALS_SECRET_KEY`; restore should run read-only validation before starting sync.
- The security guide separates the panel HTTPS entry from the Registry `/v2/` HTTPS entry, and sync does not need an exposed inbound port.

## v3 Management

- Concurrent sync: `sync_concurrency` defaults to `2`; the sync worker locks each target image so the same tag is not written concurrently.
- Retry policy: `sync_retry_count` controls max retries; copy failures use exponential backoff, and the panel can retry failed runs or failed items.
- Storage management: the panel shows local Registry repositories, tags, estimated usage, deletion marks, and garbage collection guidance.
- Notifications: configure `NOTIFY_WEBHOOK_URL` or the panel webhook setting to send sync failure, recovery, and low disk space events.
- Authentication boundary: `PANEL_TOKEN` protects write APIs only. Put the panel behind a reverse proxy with Basic Auth or another login layer before exposing it publicly.
- Import/export: the panel can export, merge import, and replace import mirror lists for backup and restore.

## v4 Platform Extensions

- Multiple Registry targets: `config/mirrors.yml` supports `registries`, and the panel can manage Registry targets.
- Multiple mirror groups: `mirror_groups` organize mirrors by project, environment, namespace, and Registry.
- Grouped views: the Platform page groups mirrors by project, environment, namespace, and mirror group.
- External database configuration: SQLite remains the default; `DATABASE_URL` or `settings.database_url` can reserve PostgreSQL/MySQL configuration.
- Audit logs: panel write operations and important sync actions are stored in `audit_logs` and shown in the Audit page.
- Extension assessment: the panel documents single-node, multi-instance, remote worker, and queued sync modes while keeping single-node Compose as the default path.

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

After changing `check_interval_minutes`, restart the sync service if you need the scheduler interval to apply immediately:

```powershell
docker compose restart sync
```

## Storage Cleanup

Deletion marks in the panel record cleanup intent only. To actually release Registry storage, delete the relevant manifests first, then run garbage collection:

```powershell
docker compose stop registry
docker compose run --rm registry registry garbage-collect /etc/docker/registry/config.yml
docker compose up -d registry
```

## Local Checks

```powershell
python -m pip install -r requirements-dev.txt
python scripts\verify.py
.\scripts\check-runtime.ps1
npm.cmd run build
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
