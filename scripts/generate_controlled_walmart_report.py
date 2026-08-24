from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from app.database_resolution import configured_sqlite_path
from app.operations_reports import create_report_snapshot, friendly_central, load_snapshot
from app.services.marketplace_order_ingestion import run_sync_cycle, sync_health
from scripts.validate_operations_reports_copy import fingerprint


def main() -> int:
    database = configured_sqlite_path()
    connection = sqlite3.connect(database)
    before = fingerprint(connection)
    connection.close()
    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    results = run_sync_cycle(channels=["walmart"])
    completed = datetime.now(timezone.utc)
    refresh = {"started_at_utc": started.isoformat(), "started_at_central": friendly_central(started),
               "completed_at_utc": completed.isoformat(), "completed_at_central": friendly_central(completed),
               "duration_seconds": round(time.monotonic() - clock, 3), "channels_requested": ["walmart"],
               "results": results}
    filters = {"channel": "walmart", "physical_site": "all", "stage": "all", "include_staged": True,
               "exclude_channels": [], "allow_stale_channels": [], "ship_start": "", "ship_end": ""}
    report_results = {}
    for report_type in ("active", "due_today"):
        run_id = create_report_snapshot(report_type=report_type, filters=filters, freshness=sync_health(), warnings=[],
                                        actor="Controlled local Walmart validation", refresh_metadata=refresh)
        metadata, snapshot = load_snapshot(run_id)
        report_results[report_type] = {"run_id": run_id, "sha256": metadata["snapshot_sha256"],
                                       "totals": snapshot["totals"]}
    connection = sqlite3.connect(database)
    after = fingerprint(connection)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    output = {"refresh": refresh, "reports": report_results, "integrity": integrity,
              "inventory_before": before, "inventory_after": after, "inventory_unchanged": before == after,
              "amazon_called": False}
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if results.get("walmart", {}).get("success") and integrity == "ok" and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
