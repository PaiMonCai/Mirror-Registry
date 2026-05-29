[CmdletBinding()]
param(
    [string]$Root = ".",
    [string]$ReportPath = "",
    [switch]$IncludeRegistryChecksums
)

$ErrorActionPreference = "Stop"

function Resolve-MigrationPath {
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

function Get-FileEntry {
    param(
        [string]$Name,
        [string]$Path,
        [bool]$Required = $true,
        [bool]$Secret = $false
    )
    $exists = Test-Path -LiteralPath $Path -PathType Leaf
    $size = $null
    $sha256 = ""
    if ($exists) {
        $item = Get-Item -LiteralPath $Path
        $size = $item.Length
        if (-not $Secret) {
            $sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    return [ordered]@{
        name = $Name
        path = $Path
        required = $Required
        exists = $exists
        kind = "file"
        size_bytes = $size
        secret = $Secret
        sha256 = $sha256
    }
}

function Get-DirectoryEntry {
    param(
        [string]$Name,
        [string]$Path,
        [bool]$Required = $true,
        [bool]$IncludeChecksums = $false
    )
    $exists = Test-Path -LiteralPath $Path -PathType Container
    $files = @()
    if ($exists) {
        $files = @(Get-ChildItem -LiteralPath $Path -File -Recurse -Force)
    }
    $size = 0L
    foreach ($file in $files) {
        $size += $file.Length
    }
    $sha256 = ""
    if ($exists -and $IncludeChecksums) {
        $builder = [System.Text.StringBuilder]::new()
        foreach ($file in ($files | Sort-Object FullName)) {
            $relative = $file.FullName.Substring((Resolve-Path -LiteralPath $Path).Path.Length).TrimStart("\", "/")
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            [void]$builder.Append($relative).Append("`0").Append($hash).Append("`0")
        }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($builder.ToString())
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $sha256 = (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
        } finally {
            $sha.Dispose()
        }
    }
    return [ordered]@{
        name = $Name
        path = $Path
        required = $Required
        exists = $exists
        kind = "directory"
        file_count = $files.Count
        size_bytes = $size
        secret = $false
        sha256 = $sha256
        checksum_mode = if ($IncludeChecksums) { "full" } else { "size-and-count" }
    }
}

$rootPath = (Resolve-Path -LiteralPath $Root).Path
$configPath = Resolve-MigrationPath "config\mirrors.yml"
$registryPath = Resolve-MigrationPath "data\registry"
$databasePath = Resolve-MigrationPath "data\mirror-registry.db"
$envPath = Resolve-MigrationPath ".env"

$items = @(
    (Get-FileEntry -Name "config_file" -Path $configPath),
    (Get-DirectoryEntry -Name "registry_storage" -Path $registryPath -IncludeChecksums:$IncludeRegistryChecksums),
    (Get-FileEntry -Name "database" -Path $databasePath),
    (Get-FileEntry -Name "env_file" -Path $envPath -Secret $true)
)

$checks = [System.Collections.Generic.List[object]]::new()
foreach ($item in $items) {
    if ($item.required -and -not $item.exists) {
        Add-Check $checks $item.name "error" "$($item.path) is missing" "Restore requires this item from the source machine."
    } else {
        Add-Check $checks $item.name "ok" "$($item.path) is present"
    }
}

$secret = [Environment]::GetEnvironmentVariable("CREDENTIALS_SECRET_KEY")
if ([string]::IsNullOrWhiteSpace($secret)) {
    $secret = Read-DotEnvValue -Path $envPath -Name "CREDENTIALS_SECRET_KEY"
}
if ([string]::IsNullOrWhiteSpace($secret)) {
    Add-Check $checks "credentials_secret" "error" "CREDENTIALS_SECRET_KEY is missing" "Use the original source-machine key before starting panel or sync."
} else {
    Add-Check $checks "credentials_secret" "ok" "CREDENTIALS_SECRET_KEY presence checked without printing it"
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    Add-Check $checks "docker" "ok" "docker command is available at $($docker.Source)"
} else {
    Add-Check $checks "docker" "warn" "docker command is not available in this shell" "Install Docker and Compose on the target machine before restore."
}

$drive = (Get-Item -LiteralPath $rootPath).PSDrive
$freeBytes = $drive.Free
$estimatedBytes = ($items | ForEach-Object { if ($_.size_bytes) { [int64]$_.size_bytes } else { 0L } } | Measure-Object -Sum).Sum
if ($freeBytes -gt $estimatedBytes) {
    Add-Check $checks "disk_space" "ok" "free=$freeBytes estimated_backup=$estimatedBytes"
} else {
    Add-Check $checks "disk_space" "warn" "free=$freeBytes estimated_backup=$estimatedBytes" "Free more disk space before restoring registry data."
}

$errorCount = @($checks | Where-Object { $_.status -eq "error" }).Count
$warnCount = @($checks | Where-Object { $_.status -eq "warn" }).Count
$status = if ($errorCount -gt 0) { "error" } elseif ($warnCount -gt 0) { "warn" } else { "ok" }

$result = [ordered]@{
    ok = $errorCount -eq 0
    readonly = $true
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    root = $rootPath
    summary = [ordered]@{
        status = $status
        ok = @($checks | Where-Object { $_.status -eq "ok" }).Count
        warn = $warnCount
        error = $errorCount
    }
    items = $items
    checks = $checks
}

$json = $result | ConvertTo-Json -Depth 10
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
