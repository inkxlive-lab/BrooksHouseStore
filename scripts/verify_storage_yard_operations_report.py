"""Generate and verify the Storage Yard Operations Report on a database copy."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.operations_reports import _run_report_job, enqueue_report_job, load_report_job, load_snapshot
from app.services.marketplace_order_ingestion import ensure_marketplace_operations_schema
from app.services.operations_report_pdf import write_report_pdf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    output = args.output.resolve()

    with sqlite3.connect(database) as connection:
        ensure_marketplace_operations_schema(connection)
        connection.commit()

    filters = {"channel": "all", "ship_start": "", "ship_end": "", "physical_site": "all",
               "stage": "all", "include_staged": True, "exclude_channels": [], "allow_stale_channels": []}
    job_id, created = enqueue_report_job(report_type="storage_yard_pull", mode="current", filters=filters,
                                         actor="Codex local verification", database=database,
                                         allow_fixture=True, start=False)
    if not created:
        raise RuntimeError(f"Verification copy already has active report job {job_id}")
    with patch("app.operations_reports.write_report_pdf",
               side_effect=lambda metadata, snapshot: write_report_pdf(metadata, snapshot, output.parent)):
        _run_report_job(job_id, database=database, allow_fixture=True)
    job = load_report_job(job_id, database, allow_fixture=True)
    if job["state"] != "complete":
        raise RuntimeError(job.get("error_message") or "Storage Yard report job failed")
    metadata, snapshot = load_snapshot(job["result_report_run_id"], database, allow_fixture=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    generated = write_report_pdf(metadata, snapshot, output.parent)
    if generated != output:
        generated.replace(output)
    required = ("active_orders", "units_required", "unique_aggregated_products", "blocked_or_at_risk", "unmatched_items")
    missing = [key for key in required if key not in snapshot["totals"]]
    if missing:
        raise RuntimeError(f"Missing required totals: {missing}")
    if any("inventory_mutation" in str(row) for row in snapshot.get("pull_rows", [])):
        raise RuntimeError("Unexpected inventory mutation marker in report snapshot")
    print(json.dumps({"job_id": job_id, "report_run_id": metadata["report_run_id"],
                      "pdf": str(output), "totals": snapshot["totals"],
                      "warnings": snapshot["warnings"], "pull_rows": len(snapshot["pull_rows"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
