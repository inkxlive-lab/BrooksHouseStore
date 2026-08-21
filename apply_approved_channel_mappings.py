#!/usr/bin/env python
"""Preview or apply Martel-approved STRONG marketplace product mappings."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.services.approved_mapping_application import (
    apply_safe_plans, file_sha256, integrity_check, inventory_fingerprint,
    load_approved_report, preflight, verified_backup,
)
from app.services.channel_inventory_reconciliation import connect_read_only


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="app/data/brookshouse_store.db")
    parser.add_argument("--approved-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--confirm-approved-strong", type=int)
    args = parser.parse_args()
    if args.apply and (args.confirm_approved_strong != 42 or not args.backup):
        parser.error("--apply requires --confirm-approved-strong 42 and --backup")

    database = Path(args.database).resolve()
    report = load_approved_report(args.approved_report)
    with connect_read_only(database) as connection:
        before_integrity = integrity_check(connection)
        before_inventory = inventory_fingerprint(connection)
        plans = preflight(connection, report)
    payload = {
        "mode": "apply" if args.apply else "preview", "created_at": datetime.now().astimezone().isoformat(),
        "database": str(database), "approved_report": str(Path(args.approved_report).resolve()),
        "approved_report_sha256": file_sha256(args.approved_report),
        "database_sha256_before": file_sha256(database), "integrity_before": before_integrity,
        "inventory_before": before_inventory, "plans": [plan.as_dict() for plan in plans],
    }
    if args.apply:
        payload["backup"] = verified_backup(database, args.backup)
        connection = sqlite3.connect(database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            payload["results"] = apply_safe_plans(connection, plans)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        with connect_read_only(database) as connection:
            payload["integrity_after"] = integrity_check(connection)
            payload["inventory_after"] = inventory_fingerprint(connection)
        payload["database_sha256_after"] = file_sha256(database)
        payload["inventory_unchanged"] = payload["inventory_before"] == payload["inventory_after"]
        if payload["integrity_after"] != "ok" or not payload["inventory_unchanged"]:
            raise RuntimeError("Post-apply integrity or inventory invariant failed; restore the verified backup")
    statuses = [item.get("apply_status", item.get("status")) for item in payload.get("results", payload["plans"])]
    payload["summary"] = {
        "approved_requested": 42, "safe": sum(plan.status == "safe" for plan in plans),
        "already_correct": statuses.count("already_correct") + statuses.count("already_mapped"),
        "conflicts_skipped": statuses.count("conflict_skipped") + statuses.count("conflict"),
        "successfully_applied": statuses.count("applied"), "failures": statuses.count("failed"),
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
