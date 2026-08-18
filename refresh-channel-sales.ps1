$ErrorActionPreference = 'Stop'
Set-Location 'C:\BrooksHouseStore'

Write-Host 'Refreshing Amazon order history...' -ForegroundColor Cyan
& .\.venv\Scripts\python.exe .\amazon_order_history_sync.py --days 365

Write-Host ''
Write-Host 'Refreshing Walmart orders...' -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -c "from app.walmart_order_service import sync_orders; print(f'Walmart orders saved: {sync_orders(30)}')"

Write-Host ''
Write-Host 'Channel sales refresh complete.' -ForegroundColor Green
Write-Host 'Open: https://inventory.shopbrookshouse.com/reports/channel-performance'
