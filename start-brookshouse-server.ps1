param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8001,
    [string]$HostAddress = "0.0.0.0",
    [string]$DatabasePath = "",
    [string]$StdoutLog = "",
    [string]$StderrLog = "",
    [switch]$EnableBackgroundJobs,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logRoot = Join-Path $projectRoot "logs"

if (-not $StdoutLog) {
    $StdoutLog = Join-Path $logRoot "brookshouse-server.stdout.log"
}
if (-not $StderrLog) {
    $StderrLog = Join-Path $logRoot "brookshouse-server.stderr.log"
}

try {
    Set-Location -LiteralPath $projectRoot
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Python executable not found: $python"
    }

    foreach ($logPath in @($StdoutLog, $StderrLog)) {
        $parent = Split-Path -Parent $logPath
        if ($parent) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
    }

    $backgroundJobsValue = if ($EnableBackgroundJobs) { "true" } else { "false" }
    $env:BROOKSHOUSE_BACKGROUND_JOBS_ENABLED = $backgroundJobsValue
    if ($DatabasePath) {
        $resolvedDatabase = [System.IO.Path]::GetFullPath($DatabasePath)
        if (-not (Test-Path -LiteralPath $resolvedDatabase -PathType Leaf)) {
            throw "Database file not found: $resolvedDatabase"
        }
        $env:DATABASE_URL = "sqlite:///" + $resolvedDatabase.Replace("\", "/")
    }

    $arguments = @(
        "-m", "uvicorn", "app.main:app",
        "--host", $HostAddress,
        "--port", $Port
    )
    if ($ValidateOnly) {
        [pscustomobject]@{
            executable = $python
            arguments = $arguments
            background_jobs_enabled = $backgroundJobsValue
            database_override = if ($DatabasePath) { $resolvedDatabase } else { $null }
        } | ConvertTo-Json -Compress
        exit 0
    }
    # Windows PowerShell 5.1 turns normal native stderr output into error
    # records when ErrorActionPreference is Stop. Keep uvicorn as a direct
    # child so Task Scheduler can terminate its complete process tree, while
    # allowing its ordinary stderr logging to pass through to the log file.
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $python @arguments 1>> $StdoutLog 2>> $StderrLog
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if ($exitCode -ne 0) {
        Add-Content -LiteralPath $StderrLog -Value (
            "[$(Get-Date -Format 'o')] uvicorn exited with code $exitCode"
        )
    }
    exit $exitCode
}
catch {
    $message = "[$(Get-Date -Format 'o')] launcher failure: $($_.Exception.Message)"
    try {
        Add-Content -LiteralPath $StderrLog -Value $message
    }
    catch {
        [Console]::Error.WriteLine($message)
    }
    exit 1
}
