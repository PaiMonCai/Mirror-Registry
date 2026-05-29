[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$ImageTag = "",
    [string]$SmokeResultPath = "",
    [string]$VersionNotesPath = "CHANGELOG.md",
    [switch]$AllowLatest,
    [switch]$SkipBuildChecks
)

$ErrorActionPreference = "Stop"
$failures = [System.Collections.Generic.List[string]]::new()

function Add-Failure {
    param([string]$Message)
    $failures.Add($Message) | Out-Null
    Write-Host "[fail] $Message" -ForegroundColor Red
}

function Add-Ok {
    param([string]$Message)
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Invoke-RequiredCommand {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Host "[run] $Label"
    $global:LASTEXITCODE = 0
    try {
        & $Command
        if ($LASTEXITCODE -ne 0) {
            Add-Failure "$Label failed with exit code $LASTEXITCODE."
        } else {
            Add-Ok "$Label passed"
        }
    } catch {
        Add-Failure "$Label failed: $($_.Exception.Message)"
    }
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    Add-Failure "Version is required. Pass -Version vX.Y.Z."
} elseif ($Version -notmatch '^v\d+\.\d+\.\d+$') {
    Add-Failure "Version must look like vX.Y.Z."
} else {
    Add-Ok "Version is set: $Version"
}

if ([string]::IsNullOrWhiteSpace($ImageTag)) {
    Add-Failure "Image tag is required. Pass -ImageTag $Version or another explicit release tag."
} elseif ($ImageTag -eq "latest" -and -not $AllowLatest) {
    Add-Failure "ImageTag must not be latest for release checklist unless -AllowLatest is explicit."
} else {
    Add-Ok "Image tag is set: $ImageTag"
}

if (-not (Test-Path -LiteralPath "README.md" -PathType Leaf) -or -not (Test-Path -LiteralPath "README.en.md" -PathType Leaf)) {
    Add-Failure "README.md and README.en.md are required."
} else {
    Add-Ok "README files are present"
}

if (-not (Test-Path -LiteralPath $VersionNotesPath -PathType Leaf)) {
    Add-Failure "Version notes are missing: $VersionNotesPath"
} else {
    $notes = Get-Content -LiteralPath $VersionNotesPath -Raw -Encoding UTF8
    if (-not [string]::IsNullOrWhiteSpace($Version) -and $notes -notmatch [regex]::Escape($Version)) {
        Add-Failure "Version notes do not mention $Version."
    } else {
        Add-Ok "Version notes mention $Version"
    }
}

if ([string]::IsNullOrWhiteSpace($SmokeResultPath) -or -not (Test-Path -LiteralPath $SmokeResultPath -PathType Leaf)) {
    Add-Failure "Smoke result file is required. Run scripts\\prod-smoke.ps1 and pass -SmokeResultPath."
} else {
    $smoke = Get-Content -LiteralPath $SmokeResultPath -Raw -Encoding UTF8
    if ($smoke -match "Production smoke passed|Verification passed|passed") {
        Add-Ok "Smoke result file looks successful"
    } else {
        Add-Failure "Smoke result file does not contain a success marker."
    }
}

if (-not $SkipBuildChecks) {
    Invoke-RequiredCommand "python scripts\verify.py" { python scripts\verify.py }
    Invoke-RequiredCommand "npm.cmd run build" { npm.cmd run build }
    Invoke-RequiredCommand "python -m pytest" { python -m pytest }
} else {
    Write-Warning "Build checks skipped by -SkipBuildChecks."
}

if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "Release checklist failed:" -ForegroundColor Red
    foreach ($failure in $failures) {
        Write-Host "  - $failure" -ForegroundColor Red
    }
    exit 1
}

Write-Host "Release checklist passed." -ForegroundColor Green
