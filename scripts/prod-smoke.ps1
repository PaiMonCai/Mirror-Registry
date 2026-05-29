[CmdletBinding()]
param(
    [string]$EnvFile = ".env",
    [string]$ComposeFile = "docker-compose.yml",
    [string]$PanelUrl = "http://localhost:8080",
    [string]$RegistryUrl = "http://localhost:5000",
    [string]$AdminUsername = "",
    [string]$AdminPassword = "",
    [string]$PanelToken = "",
    [switch]$StartServices,
    [switch]$AllowInsecureLocal,
    [switch]$SkipSync,
    [int]$ServiceTimeoutSeconds = 120,
    [int]$SyncTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

$script:Failures = @()
$script:Warnings = @()
$script:EnvValues = @{}
$script:PanelSession = $null

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Add-Failure {
    param([string]$Message)
    $script:Failures += $Message
    Write-Host "[fail] $Message" -ForegroundColor Red
}

function Add-WarningMessage {
    param([string]$Message)
    $script:Warnings += $Message
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Add-SecurityIssue {
    param([string]$Message)
    if ($AllowInsecureLocal) {
        Add-WarningMessage "Allowed by -AllowInsecureLocal: $Message"
    } else {
        Add-Failure $Message
    }
}

function Stop-IfFailed {
    if ($script:Failures.Count -eq 0) {
        return
    }
    Write-Host ""
    Write-Host "Production smoke failed:" -ForegroundColor Red
    foreach ($failure in $script:Failures) {
        Write-Host "  - $failure" -ForegroundColor Red
    }
    exit 1
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )
    Write-Step $Name
    try {
        & $Action
        Write-Host "[ok] $Name" -ForegroundColor Green
    } catch {
        Add-Failure ("{0}: {1}" -f $Name, $_.Exception.Message)
    }
}

function Read-DotEnv {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        Add-SecurityIssue "Environment file not found: $Path"
        return $values
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
        $value = $line.Substring($index + 1).Trim()
        if ($value.Length -ge 2) {
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $values[$key] = $value
    }
    return $values
}

function Get-ConfigValue {
    param(
        [string]$Name,
        [string]$Default = ""
    )
    if ($script:EnvValues.ContainsKey($Name)) {
        return [string]$script:EnvValues[$Name]
    }
    $envValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($envValue)) {
        return $envValue
    }
    return $Default
}

function Test-PlaceholderValue {
    param(
        [string]$Value,
        [string[]]$Placeholders
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $true
    }
    $normalized = $Value.Trim().ToLowerInvariant()
    if ($normalized.StartsWith("replace-with-")) {
        return $true
    }
    foreach ($placeholder in $Placeholders) {
        if ($normalized -eq $placeholder.ToLowerInvariant()) {
            return $true
        }
    }
    return $false
}

function Test-CommandAvailable {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-CheckedCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Join-Url {
    param(
        [string]$BaseUrl,
        [string]$Path
    )
    return ($BaseUrl.TrimEnd('/') + "/" + $Path.TrimStart('/'))
}

function Get-HttpStatus {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
        return [int]$response.StatusCode
    } catch {
        $response = $_.Exception.Response
        if ($null -ne $response -and $null -ne $response.StatusCode) {
            return [int]$response.StatusCode
        }
        throw
    }
}

function Wait-HttpStatus {
    param(
        [string]$Url,
        [int[]]$AcceptStatusCodes,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    do {
        try {
            $status = Get-HttpStatus -Url $Url
            if ($AcceptStatusCodes -contains $status) {
                return $status
            }
            $lastError = "HTTP $status"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for $Url. Last result: $lastError"
}

function Invoke-PanelJson {
    param(
        [string]$Method,
        [string]$Path,
        [object]$Body = $null,
        [hashtable]$Headers = $null,
        [Microsoft.PowerShell.Commands.WebRequestSession]$WebSession = $null
    )
    $uri = Join-Url -BaseUrl $PanelUrl -Path ("api" + $Path)
    $params = @{
        Uri = $uri
        Method = $Method
        TimeoutSec = 20
        ErrorAction = "Stop"
    }
    if ($null -ne $Headers -and $Headers.Count -gt 0) {
        $params.Headers = $Headers
    }
    if ($null -ne $WebSession) {
        $params.WebSession = $WebSession
    }
    if ($null -ne $Body) {
        $params.ContentType = "application/json"
        $params.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
    }
    try {
        return Invoke-RestMethod @params
    } catch {
        throw "Panel API $Method $Path failed: $($_.Exception.Message)"
    }
}

function Get-BearerHeaders {
    $token = $PanelToken
    if ([string]::IsNullOrWhiteSpace($token)) {
        $token = Get-ConfigValue -Name "PANEL_TOKEN"
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        return @{}
    }
    return @{ Authorization = "Bearer $token" }
}

function Get-EffectiveAdminUsername {
    if (-not [string]::IsNullOrWhiteSpace($AdminUsername)) {
        return $AdminUsername
    }
    return Get-ConfigValue -Name "ADMIN_USERNAME" -Default "admin"
}

function Get-EffectiveAdminPassword {
    if (-not [string]::IsNullOrWhiteSpace($AdminPassword)) {
        return $AdminPassword
    }
    return Get-ConfigValue -Name "ADMIN_PASSWORD"
}

function Get-MaxRunId {
    param([hashtable]$Headers)
    $runs = @(Invoke-PanelJson -Method "GET" -Path "/sync-runs?limit=50" -Headers $Headers)
    $max = 0
    foreach ($run in $runs) {
        if ($null -ne $run.id -and [int]$run.id -gt $max) {
            $max = [int]$run.id
        }
    }
    return $max
}

function Wait-SyncRun {
    param(
        [int]$AfterId,
        [hashtable]$Headers,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $runs = @(Invoke-PanelJson -Method "GET" -Path "/sync-runs?limit=50" -Headers $Headers)
        $candidate = $runs |
            Where-Object { $null -ne $_.id -and [int]$_.id -gt $AfterId } |
            Sort-Object -Property id -Descending |
            Select-Object -First 1
        if ($null -ne $candidate -and $candidate.status -ne "running") {
            return $candidate
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for a sync run after id $AfterId"
}

function Wait-BusyboxTag {
    param([int]$TimeoutSeconds)
    $url = Join-Url -BaseUrl $RegistryUrl -Path "/v2/library/busybox/tags/list"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ""
    do {
        try {
            $payload = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 10 -ErrorAction Stop
            if (@($payload.tags) -contains "latest") {
                return
            }
            $lastError = "latest tag is not listed yet"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for library/busybox:latest in Registry. Last result: $lastError"
}

function Test-EnvironmentSecurity {
    Write-Step "Checking production environment settings"
    $script:EnvValues = Read-DotEnv -Path $EnvFile

    $panelTokenValue = Get-ConfigValue -Name "PANEL_TOKEN"
    if (Test-PlaceholderValue -Value $panelTokenValue -Placeholders @("change-me", "changeme", "test-token", "replace-with-a-long-random-token")) {
        Add-SecurityIssue "PANEL_TOKEN is empty, default, or a placeholder."
    }

    $adminPasswordValue = Get-EffectiveAdminPassword
    if (Test-PlaceholderValue -Value $adminPasswordValue -Placeholders @("change-me", "changeme", "password", "admin", "admin-password", "replace-with-a-strong-admin-password")) {
        Add-SecurityIssue "ADMIN_PASSWORD is empty, weak, or a placeholder."
    }

    $secretKeyValue = Get-ConfigValue -Name "CREDENTIALS_SECRET_KEY"
    if (Test-PlaceholderValue -Value $secretKeyValue -Placeholders @("change-me", "changeme", "replace-with-a-long-random-secret")) {
        Add-SecurityIssue "CREDENTIALS_SECRET_KEY is empty or a placeholder."
    }

    $cookieSecure = (Get-ConfigValue -Name "SESSION_COOKIE_SECURE" -Default "false").Trim().ToLowerInvariant()
    if ($PanelUrl.Trim().ToLowerInvariant().StartsWith("https://") -and $cookieSecure -notin @("1", "true", "yes")) {
        Add-SecurityIssue "PanelUrl is HTTPS but SESSION_COOKIE_SECURE is not true."
    }

    if ($script:Failures.Count -eq 0) {
        Write-Host "[ok] Production environment settings checked" -ForegroundColor Green
    }
}

function Test-DockerCompose {
    Invoke-Step "Checking Docker Compose" {
        if (-not (Test-CommandAvailable -Name "docker")) {
            if ($StartServices) {
                throw "Docker CLI is required when -StartServices is used."
            }
            Add-WarningMessage "Docker CLI is not available; skipping docker compose config."
            return
        }

        Invoke-CheckedCommand -FilePath "docker" -Arguments @("compose", "-f", $ComposeFile, "config")
        if ($StartServices) {
            Invoke-CheckedCommand -FilePath "docker" -Arguments @("compose", "-f", $ComposeFile, "pull")
            Invoke-CheckedCommand -FilePath "docker" -Arguments @("compose", "-f", $ComposeFile, "up", "-d")
            Invoke-CheckedCommand -FilePath "docker" -Arguments @("compose", "-f", $ComposeFile, "ps")
        }
    }
}

function Test-HttpEntrypoints {
    Invoke-Step "Checking panel and Registry entrypoints" {
        $timeout = 15
        if ($StartServices) {
            $timeout = $ServiceTimeoutSeconds
        }
        $panelAuthUrl = Join-Url -BaseUrl $PanelUrl -Path "/api/auth/me"
        $registryV2Url = Join-Url -BaseUrl $RegistryUrl -Path "/v2/"
        $panelStatus = Wait-HttpStatus -Url $panelAuthUrl -AcceptStatusCodes @(200, 401) -TimeoutSeconds $timeout
        $registryStatus = Wait-HttpStatus -Url $registryV2Url -AcceptStatusCodes @(200, 401) -TimeoutSeconds $timeout
        Write-Host "Panel auth endpoint returned HTTP $panelStatus"
        Write-Host "Registry /v2/ returned HTTP $registryStatus"
    }
}

function Test-PanelApis {
    Invoke-Step "Checking panel login and protected APIs" {
        $username = Get-EffectiveAdminUsername
        $password = Get-EffectiveAdminPassword
        if ([string]::IsNullOrWhiteSpace($username) -or [string]::IsNullOrWhiteSpace($password)) {
            throw "Admin username/password are required for login smoke."
        }

        $script:PanelSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        $login = Invoke-PanelJson -Method "POST" -Path "/auth/login" -Body @{ username = $username; password = $password } -WebSession $script:PanelSession
        if (-not $login.ok) {
            throw "Login response did not report ok=true."
        }

        $sessionStatus = Invoke-PanelJson -Method "GET" -Path "/status" -WebSession $script:PanelSession
        Write-Host "Session status: total mirrors=$($sessionStatus.total), synced=$($sessionStatus.synced)"

        $headers = Get-BearerHeaders
        if ($headers.Count -eq 0) {
            throw "PANEL_TOKEN is required for automation compatibility smoke."
        }
        $bearerStatus = Invoke-PanelJson -Method "GET" -Path "/status" -Headers $headers
        Write-Host "Bearer status: auth_mode=$($bearerStatus.auth_mode), app_version=$($bearerStatus.app_version)"

        $diagnostics = Invoke-PanelJson -Method "POST" -Path "/diagnostics/run" -Headers $headers -Body @{}
        $diagnosticErrors = @()
        foreach ($check in @($diagnostics.checks)) {
            if ($check.status -eq "error") {
                $diagnosticErrors += "$($check.name): $($check.message)"
            } elseif ($check.status -eq "warn") {
                Add-WarningMessage "Diagnostic warning: $($check.name): $($check.message)"
            }
        }
        if ($diagnosticErrors.Count -gt 0) {
            throw "Diagnostic errors: $($diagnosticErrors -join '; ')"
        }

        $verify = Invoke-PanelJson -Method "POST" -Path "/backup-restore/verify" -Headers $headers -Body @{ require_credentials_secret = $true }
        if (-not $verify.ok) {
            $failed = @($verify.checks | Where-Object { -not $_.ok } | ForEach-Object { $_.name }) -join ", "
            throw "Backup restore readiness failed: $failed"
        }
    }
}

function Test-SyncSmoke {
    Invoke-Step "Checking sync smoke" {
        if (-not $StartServices) {
            Add-WarningMessage "Skipping sync trigger because -StartServices was not set."
            return
        }
        if ($SkipSync) {
            Add-WarningMessage "Skipping sync trigger because -SkipSync was set."
            return
        }

        $headers = Get-BearerHeaders
        if ($headers.Count -eq 0) {
            throw "PANEL_TOKEN is required to trigger sync."
        }

        $mirrors = @(Invoke-PanelJson -Method "GET" -Path "/mirrors" -Headers $headers)
        if ($mirrors.Count -eq 0) {
            throw "No mirrors are configured."
        }
        $busybox = $mirrors |
            Where-Object { $_.source -eq "docker.io/library/busybox:latest" -and $_.target -like "*library/busybox:latest" } |
            Select-Object -First 1
        if ($null -eq $busybox) {
            Add-WarningMessage "Default busybox mirror is not configured; sync smoke will validate the current mirror set only."
        }

        $beforeRunId = Get-MaxRunId -Headers $headers
        Invoke-PanelJson -Method "POST" -Path "/sync" -Headers $headers -Body @{} | Out-Null
        $run = Wait-SyncRun -AfterId $beforeRunId -Headers $headers -TimeoutSeconds $SyncTimeoutSeconds
        Write-Host "Sync run $($run.id) finished with status=$($run.status), failed=$($run.failed)"
        if ($run.status -ne "completed" -or [int]$run.failed -gt 0) {
            throw "Sync smoke failed. Run id=$($run.id), status=$($run.status), failed=$($run.failed), message=$($run.message)"
        }

        if ($null -ne $busybox) {
            Wait-BusyboxTag -TimeoutSeconds 60
            Write-Host "Registry contains library/busybox:latest"
        }
    }
}

Test-EnvironmentSecurity
Stop-IfFailed

Test-DockerCompose
Test-HttpEntrypoints
Test-PanelApis
Test-SyncSmoke

Stop-IfFailed

Write-Host ""
if ($script:Warnings.Count -gt 0) {
    Write-Host "Production smoke completed with warnings:" -ForegroundColor Yellow
    foreach ($warning in $script:Warnings) {
        Write-Host "  - $warning" -ForegroundColor Yellow
    }
} else {
    Write-Host "Production smoke passed." -ForegroundColor Green
}
