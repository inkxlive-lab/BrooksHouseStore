from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.operations_reports import CENTRAL, create_report_snapshot, load_snapshot
from scripts.validate_operations_reports_copy import fingerprint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    if database == Path("app/data/brookshouse_store.db").resolve():
        raise RuntimeError("Refusing protected database")
    args.output_directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    before = fingerprint(connection)
    connection.close()
    filters = {"channel": "walmart", "physical_site": "all", "stage": "all", "include_staged": True,
               "exclude_channels": [], "allow_stale_channels": ["walmart"], "ship_start": "", "ship_end": ""}
    freshness = {"channels": {"walmart": {"stale": False, "last_success": None}}}
    snapshots = {}
    for report_type in ("active", "due_today", "master_pull", "exceptions", "reconciliation"):
        run_id = create_report_snapshot(report_type=report_type, filters=filters, freshness=freshness,
            warnings=["COPY VALIDATION — no live channel call"], actor="Copied database validation",
            database=database, allow_fixture=True, today_central=datetime.now(CENTRAL).date())
        metadata, snapshot = load_snapshot(run_id, database, allow_fixture=True)
        snapshots[report_type] = {"run_id": run_id, "sha256": metadata["snapshot_sha256"], "snapshot": snapshot}
    active_rows = snapshots["active"]["snapshot"]["order_rows"]
    audit_path = args.output_directory / "walmart-actionable-audit.csv"
    with audit_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["Order ID", "Marketplace status", "Local status", "Internal fulfillment stage",
                         "Ship by Central", "Already staged", "Why actionable"])
        seen = set()
        for row in active_rows:
            if row["order_id"] in seen:
                continue
            seen.add(row["order_id"])
            writer.writerow([row["order_id"], row["marketplace_status"], row["fulfillment_stage"],
                             row["fulfillment_stage"], row["ship_by_central"],
                             "YES" if row["quantity_staged"] else "NO",
                             "Authoritative marketplace status is nonterminal; local verification is fresh; " +
                             ("overdue" if row["overdue"] else "awaiting fulfillment")])
    connection = sqlite3.connect(database)
    after = fingerprint(connection)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    result = {"integrity_check": integrity, "inventory_before": before, "inventory_after": after,
              "inventory_unchanged": before == after, "audit_csv": str(audit_path),
              "reports": {key: {"run_id": value["run_id"], "sha256": value["sha256"],
                                  "totals": value["snapshot"]["totals"]} for key, value in snapshots.items()}}
    result_path = args.output_directory / "operations-reports-repair-validation.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if integrity == "ok" and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
