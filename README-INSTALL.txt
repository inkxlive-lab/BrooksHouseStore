BROOKSHOUSE OFFLINE MODE - INSTALL
=================================

1. Extract this ZIP to a folder on the BrooksHouse server Desktop.
2. Open PowerShell as Administrator.
3. Run:

   Set-ExecutionPolicy -Scope Process Bypass
   & "$env:USERPROFILE\Desktop\brookshouse-offline-mode\Install-BrooksHouse-OfflineMode.ps1"

   If the extracted folder has a different name, drag Install-BrooksHouse-OfflineMode.ps1
   into PowerShell after typing an ampersand and a space.

4. Restart the SAME scheduled task that normally runs BrooksHouse. Do not start a
   second Uvicorn copy on port 8001.

5. Check:

   Invoke-WebRequest http://127.0.0.1:8001/offline -UseBasicParsing |
       Select-Object StatusCode

6. While connected, open /offline and click Download fresh inventory snapshot.
   Then test with the phone in airplane mode.

FEATURES
- Offline Inventory Search, including description and container wildcards.
- Offline barcode lookup for Batch Scan and Tote Audit.
- Automatic reconnect sync with unique transaction IDs (duplicate-safe retries).
- Tote Audit conflict hold when the server tote changed after the saved snapshot.
- Owner/admin review at /admin/offline-sync and from Admin System Check.

IMPORTANT
- Each phone/browser must download its own snapshot while connected.
- Offline results reflect the time shown on the snapshot, not live inventory.
- Do not clear browser/site data until pending work has synced.
- The installer creates a timestamped backup under C:\BrooksHouseStore\backups.
