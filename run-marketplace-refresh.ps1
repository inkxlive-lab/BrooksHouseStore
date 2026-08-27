$ErrorActionPreference = "Stop"

$projectRoot = "C:\BrooksHouseStore"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logRoot = Join-Path $projectRoot "logs"
$stdoutLog = Join-Path $logRoot "brookshouse-marketplace-refresh.stdout.log"
$stderrLog = Join-Path $logRoot "brookshouse-marketplace-refresh.stderr.log"

Set-Location $projectRoot
New-Item -ItemType Directory -Force $logRoot | Out-Null

$env:BROOKSHOUSE_PROCESS_ROLE = "worker"
$env:BROOKSHOUSE_BACKGROUND_JOBS_ENABLED = "false"

$scriptPath = Join-Path $projectRoot "run-marketplace-refresh.py"

@"
import json
from app.services.marketplace_order_ingestion import run_sync_cycle

result = run_sync_cycle(("walmart", "amazon"))
print(json.dumps(result, default=str))
"@ | Set-Content $scriptPath -Encoding UTF8

$ErrorActionPreference = "Continue"

& $python $scriptPath 1>> $stdoutLog 2>> $stderrLog
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Add-Content $stderrLog "[$(Get-Date -Format o)] marketplace refresh exited with code $exitCode"
}

exit $exitCode
