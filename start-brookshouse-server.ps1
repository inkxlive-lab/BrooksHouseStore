$ErrorActionPreference = "Stop"

Set-Location "C:\BrooksHouseStore"

& "C:\BrooksHouseStore\.venv\Scripts\python.exe" `
    -m uvicorn app.main:app `
    --host 0.0.0.0 `
    --port 8001 `
    *>> "C:\BrooksHouseStore\logs\brookshouse-server.log"
