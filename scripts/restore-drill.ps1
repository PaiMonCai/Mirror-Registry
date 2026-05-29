[CmdletBinding()]
param(
    [string]$Root = ".",
    [bool]$RequireCredentialsSecret = $true,
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"

function Resolve-DrillPath {
    param([string]$RelativePath)
    return (Join-Path -Path $Root -ChildPath $RelativePath)
}

function Add-Check {
    param(
        [System.Collections.Generic.List[object]]$Checks,
        [string]$Name,
        [string]$Status,
        [string]$Message,
        [string]$Path = ""
    )
    $Checks.Add([ordered]@{
        name = $Name
        status = $Status
        ok = $Status -ne "error"
        message = $Message
        path = $Path
    }) | Out-Null
}

function Read-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name
    )
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
        $key = $line.Substring(0, $index).Trim()
        if ($key -ne $Name) {
            continue
        }
        return $line.Substring($index + 1).Trim().Trim('"').Trim("'")
    }
    return ""
}

$checks = [System.Collections.Generic.List[object]]::new()
$configPath = Resolve-DrillPath "config\mirrors.yml"
$registryPath = Resolve-DrillPath "data\registry"
$databasePath = Resolve-DrillPath "data\mirror-registry.db"
$envPath = Resolve-DrillPath ".env"

if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    Add-Check $checks "config" "ok" "config/mirrors.yml is present" $configPath
} else {
    Add-Check $checks "config" "error" "config/mirrors.yml is missing" $configPath
}

if (Test-Path -LiteralPath $registryPath -PathType Container) {
    Add-Check $checks "registry_storage" "ok" "data/registry is present" $registryPath
} else {
    Add-Check $checks "registry_storage" "error" "data/registry is missing" $registryPath
}

if (Test-Path -LiteralPath $databasePath -PathType Leaf) {
    Add-Check $checks "database" "ok" "data/mirror-registry.db is present" $databasePath
} else {
    Add-Check $checks "database" "error" "data/mirror-registry.db is missing" $databasePath
}

if (Test-Path -LiteralPath $envPath -PathType Leaf) {
    Add-Check $checks "env_file" "ok" ".env is present" $envPath
} else {
    Add-Check $checks "env_file" "error" ".env is missing" $envPath
}

$secret = [Environment]::GetEnvironmentVariable("CREDENTIALS_SECRET_KEY")
if ([string]::IsNullOrWhiteSpace($secret)) {
    $secret = Read-DotEnvValue -Path $envPath -Name "CREDENTIALS_SECRET_KEY"
}
if ($RequireCredentialsSecret -and [string]::IsNullOrWhiteSpace($secret)) {
    Add-Check $checks "credentials_secret" "error" "CREDENTIALS_SECRET_KEY is missing" "CREDENTIALS_SECRET_KEY"
} else {
    Add-Check $checks "credentials_secret" "ok" "CREDENTIALS_SECRET_KEY presence checked without printing it" "CREDENTIALS_SECRET_KEY"
}

$errorCount = @($checks | Where-Object { $_.status -eq "error" }).Count
$warnCount = @($checks | Where-Object { $_.status -eq "warn" }).Count
$status = if ($errorCount -gt 0) { "error" } elseif ($warnCount -gt 0) { "warn" } else { "ok" }
$result = [ordered]@{
    ok = $errorCount -eq 0
    readonly = $true
    checked_at = (Get-Date).ToUniversalTime().ToString("o")
    summary = [ordered]@{
        status = $status
        ok = @($checks | Where-Object { $_.status -eq "ok" }).Count
        warn = $warnCount
        error = $errorCount
    }
    checks = $checks
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
