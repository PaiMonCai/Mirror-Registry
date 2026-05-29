[CmdletBinding()]
param(
    [string]$Root = ".",
    [string]$ExpectedTag = "",
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"

function Resolve-UpgradePath {
    param([string]$RelativePath)
    return (Join-Path -Path $Root -ChildPath $RelativePath)
}

function Add-Check {
    param(
        [System.Collections.Generic.List[object]]$Checks,
        [string]$Name,
        [string]$Status,
        [string]$Message,
        [string]$Suggestion = ""
    )
    $Checks.Add([ordered]@{
        name = $Name
        status = $Status
        ok = $Status -ne "error"
        message = $Message
        suggestion = $Suggestion
    }) | Out-Null
}

function Read-DotEnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        $index = $line.IndexOf("=")
        if ($index -lt 1) {
            continue
        }
        if ($line.Substring(0, $index).Trim() -eq $Name) {
            return $line.Substring($index + 1).Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

$rootPath = (Resolve-Path -LiteralPath $Root).Path
$envPath = Resolve-UpgradePath ".env"
$composePath = Resolve-UpgradePath "docker-compose.yml"
$dataPath = Resolve-UpgradePath "data"
$checks = [System.Collections.Generic.List[object]]::new()

Add-Check $checks "docker_compose_file" ($(if (Test-Path -LiteralPath $composePath -PathType Leaf) { "ok" } else { "error" })) "$composePath" "docker-compose.yml must exist before upgrade."
Add-Check $checks "data_directory" ($(if (Test-Path -LiteralPath $dataPath -PathType Container) { "ok" } else { "warn" })) "$dataPath" "Confirm named volumes or local data directory before upgrade."

$panelToken = Read-DotEnvValue -Path $envPath -Name "PANEL_TOKEN"
if ([string]::IsNullOrWhiteSpace($panelToken) -or $panelToken -eq "change-me") {
    Add-Check $checks "panel_token" "warn" "PANEL_TOKEN is missing or default" "Set a long random PANEL_TOKEN before exposing the panel."
} else {
    Add-Check $checks "panel_token" "ok" "PANEL_TOKEN is configured"
}

$adminPassword = Read-DotEnvValue -Path $envPath -Name "ADMIN_PASSWORD"
if ([string]::IsNullOrWhiteSpace($adminPassword)) {
    Add-Check $checks "admin_password" "warn" "ADMIN_PASSWORD is missing" "Existing initialized volumes can keep the old password; new installs need ADMIN_PASSWORD."
} else {
    Add-Check $checks "admin_password" "ok" "ADMIN_PASSWORD is present without printing it"
}

$credentialKey = Read-DotEnvValue -Path $envPath -Name "CREDENTIALS_SECRET_KEY"
if ([string]::IsNullOrWhiteSpace($credentialKey)) {
    Add-Check $checks "credentials_secret" "warn" "CREDENTIALS_SECRET_KEY is missing" "Set it before storing registry credentials."
} else {
    Add-Check $checks "credentials_secret" "ok" "CREDENTIALS_SECRET_KEY is present without printing it"
}

$imageTag = Read-DotEnvValue -Path $envPath -Name "MIRROR_REGISTRY_IMAGE_TAG"
if ([string]::IsNullOrWhiteSpace($imageTag)) {
    $imageTag = "latest"
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedTag) -and $imageTag -ne $ExpectedTag) {
    Add-Check $checks "image_tag" "warn" "MIRROR_REGISTRY_IMAGE_TAG=$imageTag expected=$ExpectedTag" "Update .env or pass the tag you intend to deploy."
} else {
    Add-Check $checks "image_tag" "ok" "MIRROR_REGISTRY_IMAGE_TAG=$imageTag"
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    Add-Check $checks "docker" "ok" "docker command is available at $($docker.Source)"
} else {
    Add-Check $checks "docker" "error" "docker command is unavailable" "Install Docker before install or upgrade."
}

$errorCount = @($checks | Where-Object { $_.status -eq "error" }).Count
$warnCount = @($checks | Where-Object { $_.status -eq "warn" }).Count
$status = if ($errorCount -gt 0) { "error" } elseif ($warnCount -gt 0) { "warn" } else { "ok" }
$result = [ordered]@{
    ok = $errorCount -eq 0
    readonly = $true
    checked_at = (Get-Date).ToUniversalTime().ToString("o")
    root = $rootPath
    summary = [ordered]@{ status = $status; ok = @($checks | Where-Object { $_.status -eq "ok" }).Count; warn = $warnCount; error = $errorCount }
    checks = $checks
    commands = [ordered]@{
        backup = "powershell -ExecutionPolicy Bypass -File .\scripts\migration-report.ps1 -ReportPath .\migration-report.json"
        upgrade = "docker compose pull && docker compose up -d"
        rollback = "set MIRROR_REGISTRY_IMAGE_TAG=<previous-tag> && docker compose pull && docker compose up -d"
    }
}

$json = $result | ConvertTo-Json -Depth 8
if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
    $parent = Split-Path -Path $ReportPath -Parent
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Set-Content -LiteralPath $ReportPath -Value $json -Encoding UTF8
}

Write-Output $json
if ($errorCount -gt 0) {
    exit 1
}
