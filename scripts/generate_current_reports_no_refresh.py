from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from app.database_resolution import configured_sqlite_path
from app.operations_reports import create_report_snapshot, load_snapshot
from app.services.marketplace_order_ingestion import sync_health
from scripts.validate_operations_reports_copy import fingerprint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor", default="Current reconciled data export - no channel refresh")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    database = args.database.resolve() if args.database else configured_sqlite_path()
    allow_fixture = args.database is not None
    connection = sqlite3.connect(database)
    before = fingerprint(connection)
    connection.close()
    filters = {"channel": "all", "physical_site": "all", "stage": "all", "include_staged": True,
               "exclude_channels": [], "allow_stale_channels": [], "ship_start": "", "ship_end": ""}
    reports = {}
    health = sync_health(database, allow_fixture=allow_fixture)
    for report_type in ("due_today", "master_pull"):
        run_id = create_report_snapshot(report_type=report_type, filters=filters, freshness=health,
                                        warnings=[], actor=args.actor, database=database, allow_fixture=allow_fixture)
        metadata, snapshot = load_snapshot(run_id, database, allow_fixture=allow_fixture)
        reports[report_type] = {"run_id": run_id, "sha256": metadata["snapshot_sha256"],
                                "totals": snapshot["totals"]}
    connection = sqlite3.connect(database)
    after = fingerprint(connection)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    result = {"reports": reports, "inventory_unchanged": before == after, "inventory": after,
              "integrity": integrity, "channel_refresh_called": False}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if integrity == "ok" and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
