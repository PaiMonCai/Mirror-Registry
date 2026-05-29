# Mirror Registry

[中文文档](README.md)

Single-node private Docker registry with a lightweight management panel and scheduled image synchronization.

## What It Runs

- `registry`: official `registry:2`, storing image layers under `data/registry`.
- `panel`: FastAPI API plus the static web panel on port `8080`.
- `sync`: Python worker that checks upstream image digests and mirrors changed images into the local registry with `skopeo copy`.

## Project Layout

- `panel/main.py`: FastAPI ASGI compatibility entrypoint delegated to `panel/app.py`.
- `panel/app.py`: panel API, route registration, and backend orchestration.
- `panel/schemas.py`: panel request models and field constraints.
- `sync/sync.py`: sync worker compatibility entrypoint delegated to `sync/worker.py`.
- `sync/worker.py`: scheduler, trigger polling, and `skopeo` sync execution.
- `mirror_registry_core/`: shared defaults and common capabilities used by panel and sync.

## Production Deployment

Production servers do not build the `panel` or `sync` images locally. They pull published images from GHCR. The deployment directory only needs `docker-compose.yml` and `.env`; runtime data is stored in Docker named volumes:

```powershell
docker compose pull
docker compose up -d
docker compose ps
```

You can also run the update as one command: `docker compose pull && docker compose up -d`.

Open `http://localhost:8080`.

Production Compose no longer depends on project-side `config/` or `data/` folders:

- `mirror-registry-config`: stores the panel-generated `mirrors.yml`.
- `mirror-registry-data`: stores SQLite, logs, trigger files, and sync state.
- `mirror-registry-storage`: stores Registry image layers and is mounted read-only into the panel for storage statistics.

On first startup, the panel initializes default `busybox` mirror configuration in the config volume.

The panel now uses account/password login by default, while `PANEL_TOKEN` remains available for scripts and automation. Set a strong admin password and a real token in `.env` before exposing the panel:

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

Use `SESSION_COOKIE_SECURE=true` behind HTTPS. Local or plain HTTP intranet tests can keep it `false`. On first startup, if no admin exists in the data volume, the panel initializes one from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

`MIRROR_REGISTRY_IMAGE_TAG` defaults to `latest`. To pin a release, set it to a specific tag:

```dotenv
MIRROR_REGISTRY_IMAGE_TAG=v1.0.0
```

Browser access uses an HttpOnly session cookie. `PANEL_TOKEN` is no longer the primary frontend entry; it is kept for scripts, CI, or external automation using a Bearer token against protected APIs.

### Production Smoke Test

Production acceptance is centered on `scripts\prod-smoke.ps1`. By default, it performs security checks and read-only probes only; it does not pull images, start services, or restart services:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prod-smoke.ps1
```

On a new host, or when you explicitly want the script to start services, pass `-StartServices`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prod-smoke.ps1 -StartServices
```

The script treats `.env` as a production gate by default: `PANEL_TOKEN` cannot be the default value, `ADMIN_PASSWORD` cannot be empty or a placeholder, and `CREDENTIALS_SECRET_KEY` must be set. If `PanelUrl` uses HTTPS, `SESSION_COOKIE_SECURE` must be `true`. For local trials, use `-AllowInsecureLocal` to downgrade those security findings to warnings:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prod-smoke.ps1 -AllowInsecureLocal
```

The full smoke checks Docker Compose config, panel login, Bearer token automation access, Registry `/v2/`, diagnostics API, and read-only backup/restore readiness. With `-StartServices` and without `-SkipSync`, it also triggers mirror sync and verifies `library/busybox:latest` in the local Registry when the default busybox mirror is configured. If the admin account was initialized earlier with a different password, pass `-AdminUsername` and `-AdminPassword`.

### Operations Summary and Release Checks

The dashboard loads `/api/ops/summary` to show health, recent sync failures, disk state, deletion marks, and the running version in one place. Common auth, TLS, network, DNS, manifest, disk, and `skopeo` errors are mapped to readable reasons and suggestions while the original task error remains available in run details.

The Observability page loads `/api/observability/summary` for 24h/7d sync success rate, failure breakdown, sync trend, disk state, deletion mark backlog, and active alerts. External scripts can pull `/api/observability/metrics` for lightweight metrics JSON; alert webhooks are still sent by the sync worker and `NOTIFY_DEDUPE_SECONDS` controls the dedupe window for repeated event types.

For troubleshooting handoff, export a diagnostic bundle from the dashboard or call `/api/ops/diagnostic-bundle`. The bundle includes version, config summary, diagnostics, recent runs, recent failures, and events, while redacting password, token, session cookie, authfile, Authorization, and encrypted credential fields. The upgrade guide is available at `/api/ops/upgrade-guide` and covers environment variables, volumes, backups, and compatibility checks.

Before a release, run the local release checklist to block missing version numbers, image tags, version notes, README files, or smoke results:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release-check.ps1 -Version v1.0.0 -ImageTag v1.0.0 -SmokeResultPath .\smoke-result.txt
```

`ImageTag` cannot be `latest` unless `-AllowLatest` is explicit. Use `-SkipBuildChecks` only when you need a partial checklist; run the full checklist before publishing.

## Frontend Engineering and Registry Credentials

- The panel frontend is built with React + Vite + TypeScript, and FastAPI continues to serve the built static files.
- After frontend edits, run `npm.cmd run build`; production image builds run a Node build stage while the runtime image stays Python-only.
- The Credentials page stores source and target registry username + token/password pairs encrypted.
- Credentials support host defaults and per-mirror overrides. Matching priority is mirror override > host default > no credential.
- Production deployments must set `CREDENTIALS_SECRET_KEY` before saving credentials. Secrets are not echoed, exported in plaintext, logged, or written into audit detail.
- The sync worker creates a temporary authfile for `skopeo inspect/copy` and removes it after the command finishes.
- Panel login uses a single admin account and a session cookie. Login success, login failure, and logout are audited without recording passwords or session tokens.

## Repository Governance and Backup Restore

- The Governance page supports tag protection rules. Production environments, release tags, and explicit rules block delete marks, retention policies, and automatic overwrites.
- Retention policies run dry-run first and list candidate repo/tags, matching reasons, and protected skips. Applying a policy creates deletion marks only; it does not delete manifests.
- Storage management has search and detail APIs that connect tag source, digest, sync task, deletion mark, and protection state.
- Credential tests distinguish authentication failures, network failures, unreachable registries, and missing permissions while keeping token/password values redacted.
- The backup checklist covers `config/`, `data/registry/`, `data/mirror-registry.db`, `.env`, and `CREDENTIALS_SECRET_KEY`; restore should run read-only validation before starting sync.
- Restore drills can run from the Governance page or `scripts\restore-drill.ps1` to produce a read-only report for the backup package shape, SQLite, Registry data directory, and credentials master key without starting sync.
- The security guide separates the panel HTTPS entry from the Registry `/v2/` HTTPS entry, and sync does not need an exposed inbound port.

## Automated Publishing and Scheduled Push

- The `Dev Images` workflow supports manual dispatch and nightly schedule. Scheduled images publish `nightly-YYYYMMDD` and `dev-<sha>` only; they never overwrite release `latest`.
- Release images are still triggered only by `v*` tags, and `latest` continues to mean the latest release.
- The Scheduled Push page creates business image push policies. Policies are disabled by default; cron is UTC, for example `0 18 * * *` is 02:00 Beijing time.
- Each policy shows enabled state, last run, next run, and latest error.
- Manual runs, create/update operations, and sync execution results are audited. Failures appear in run history, text logs, events, and webhook notifications.
- Scheduled push refuses `latest` unless explicitly allowed, and tag protection rules still apply.

## Sync Queue

- Manual sync, single-mirror sync, post-import sync, scheduled push, and retry actions all enter the persistent SQLite `sync_queue`, and the worker consumes tasks by priority.
- The Sync Tasks page shows the queue and lets you pause, resume, cancel, or replay `queued`, `completed`, `failed`, and `canceled` tasks.
- The API exposes `GET /api/sync-queue` plus `/api/sync-queue/{id}/pause`, `/resume`, `/cancel`, and `/replay` controls.
- On startup, the worker recovers unfinished `running` / `cancel_requested` tasks back into retryable queue items, and legacy `.trigger` files are still converted into queue tasks.

## Image Size Statistics

- The Storage page can queue manifest/blob recalculation in the background. Page requests read SQLite cache and do not perform heavy full scans.
- Manifest requests send Docker schema2, OCI manifest, OCI index, and Docker manifest list `Accept` headers.
- Tags show logical size, deduplicated size, shared blob count, and multi-platform breakdown.
- Repository size deduplicates by blob digest, while physical Registry usage scans `data/registry/docker/registry/v2/blobs/sha256` separately.
- If Registry is temporarily unavailable, `/api/storage` still returns deletion marks, cached stats, and a readable error.

## v3 Management

- Concurrent sync: `sync_concurrency` defaults to `2`; the sync worker locks each target image so the same tag is not written concurrently.
- Retry policy: `sync_retry_count` controls max retries; copy failures use exponential backoff, and the panel can retry failed runs or failed items.
- Storage management: the panel shows local Registry repositories, tags, estimated usage, deletion marks, and garbage collection guidance.
- Notifications: configure `NOTIFY_WEBHOOK_URL` or the panel webhook setting to send sync failure, recovery, and low disk space events.
- Authentication boundary: backend APIs require account/password login by default. `PANEL_TOKEN` is retained only for automation compatibility, and the panel should still sit behind a reverse proxy with optional Basic Auth or trusted IP limits before public exposure.
- Import/export: the panel can export, merge import, and replace import mirror lists for backup and restore.
- Sync preflight: the panel can run single-image or batch read-only checks for image config, credentials, tag protection, and `latest` risk. Remote probes for upstream manifests and target Registry `/v2/` run only when explicitly enabled.

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
- The UI defaults to a light operations theme. Dark theme is stored in browser local storage; login state is stored in an HttpOnly cookie.

## Local Development

Use the development compose file when you need to build source images locally:

```powershell
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml ps
```

## Configuration

For production, maintain mirror configuration through the panel; first startup creates a default config automatically. For local development, you can still edit `config/mirrors.yml` directly:

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
