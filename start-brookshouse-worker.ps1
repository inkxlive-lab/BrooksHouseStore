$ErrorActionPreference = "Stop"

$projectRoot = "C:\BrooksHouseStore"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logRoot = Join-Path $projectRoot "logs"

New-Item -ItemType Directory -Force $logRoot | Out-Null
Set-Location $projectRoot

$env:BROOKSHOUSE_PROCESS_ROLE = "worker"
$env:BROOKSHOUSE_BACKGROUND_JOBS_ENABLED = "true"
$env:PYTHONPATH = $projectRoot

$stdout = Join-Path $logRoot "brookshouse-worker.stdout.log"
$stderr = Join-Path $logRoot "brookshouse-worker.stderr.log"

$process = Start-Process `
    -FilePath $python `
    -ArgumentList "-m","scripts.run_marketplace_worker" `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -NoNewWindow `
    -PassThru `
    -Wait

exit $process.ExitCode
