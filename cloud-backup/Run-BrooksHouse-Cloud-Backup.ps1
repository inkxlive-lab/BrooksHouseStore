param(
    [ValidateSet("database", "media")]
    [string]$Mode = "database"
)

$ErrorActionPreference = "Stop"
$installRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $installRoot "backup-config.json"
$pythonPath = Join-Path $installRoot "python-path.txt"

if (-not (Test-Path $configPath)) {
    throw "Backup configuration not found: $configPath"
}

if (-not (Test-Path $pythonPath)) {
    throw "Python path file not found: $pythonPath"
}

$python = (Get-Content $pythonPath -Raw).Trim()
if (-not (Test-Path $python)) {
    throw "Configured Python executable does not exist: $python"
}

& $python `
    (Join-Path $installRoot "backup_agent.py") `
    --config $configPath `
    --mode $Mode

exit $LASTEXITCODE
