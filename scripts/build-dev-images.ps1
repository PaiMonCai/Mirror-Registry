$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI is required. Install gh and run: gh auth login"
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required."
}

$Tag = $env:MIRROR_REGISTRY_DEV_TAG
if ([string]::IsNullOrWhiteSpace($Tag)) {
    $Tag = "dev"
}

$Branch = $env:MIRROR_REGISTRY_DEV_REF
if ([string]::IsNullOrWhiteSpace($Branch)) {
    $Branch = (git rev-parse --abbrev-ref HEAD).Trim()
}

if ($Branch -eq "HEAD") {
    throw "Detached HEAD is not supported for dev image dispatch. Checkout a branch first."
}

$Dirty = git status --porcelain
if (-not [string]::IsNullOrWhiteSpace($Dirty)) {
    throw "Working tree has uncommitted changes. Commit or stash them before triggering a dev image build."
}

$Remote = $env:MIRROR_REGISTRY_DEV_REMOTE
if ([string]::IsNullOrWhiteSpace($Remote)) {
    $Remote = "origin"
}

$SkipPush = $env:MIRROR_REGISTRY_SKIP_PUSH
if ($SkipPush -ne "1") {
    Write-Host "Pushing $Branch to $Remote so GitHub Actions can build the current commit"
    git push $Remote $Branch
}

$Sha = (git rev-parse --short HEAD).Trim()
$RefLabel = "$Branch@$Sha"

Write-Host "Dispatching Dev Images workflow"
Write-Host "  ref: $Branch"
Write-Host "  image_tag: $Tag"
Write-Host "  ref_label: $RefLabel"

gh workflow run dev-images.yml --ref $Branch -f image_tag=$Tag -f ref_label=$RefLabel

Write-Host "Triggered GitHub Actions dev build. Watch it with:"
Write-Host "  gh run list --workflow dev-images.yml --limit 5"
Write-Host "Published image tags will include:"
Write-Host "  ghcr.io/paimoncai/mirror-registry-panel:$Tag"
Write-Host "  ghcr.io/paimoncai/mirror-registry-sync:$Tag"
