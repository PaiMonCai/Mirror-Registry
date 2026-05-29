from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=512)


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


class MirrorDiscoveryIn(BaseModel):
    content: str = Field(min_length=1, max_length=200000)
    source_type: str = Field(default="auto", max_length=16)
    target_registry: str = Field(default="localhost:5000", min_length=1, max_length=255)
    registry: str = Field(default="local", min_length=1, max_length=64)
    group: str = Field(default="default", min_length=1, max_length=64)
    project: str = Field(default="default", min_length=1, max_length=64)
    environment: str = Field(default="local", min_length=1, max_length=64)
    namespace: str = Field(default="library", min_length=1, max_length=128)
    source_credential_id: str | None = Field(default=None, max_length=64)
    target_credential_id: str | None = Field(default=None, max_length=64)
    mode: str = Field(default="missing_only", max_length=16)
    trigger_sync: bool = False


class MirrorPreflightIn(MirrorIn):
    check_remote: bool = False


class MirrorPreflightBatchIn(BaseModel):
    mirrors: list[MirrorIn] = Field(default_factory=list, max_length=500)
    check_remote: bool = False


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


class BackupRestoreDrillIn(BaseModel):
    require_credentials_secret: bool = True
    verify_registry_sample: bool = False


class ScheduledPushPolicyIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)
    cron: str = Field(default="0 18 * * *", min_length=1, max_length=120)
    enabled: bool = False
    allow_latest: bool = False
    source_credential_id: str | None = Field(default=None, max_length=64)
    target_credential_id: str | None = Field(default=None, max_length=64)
