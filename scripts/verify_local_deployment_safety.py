"""Read-only fingerprints for local deployment safety verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


TABLE_GROUPS = {
    "inventory": ("inventory",),
    "transactions": ("inventory_transactions", "transactions"),
    "orders": (
        "walmart_orders", "walmart_order_lines",
        "amazon_order_history", "amazon_order_item_history",
    ),
    "alerts": ("marketplace_order_alerts",),
    "mappings": (
        "walmart_product_links", "walmart_listings",
        "amazon_product_links", "amazon_listings",
    ),
    "allocations": (
        "walmart_order_allocations", "channel_inventory_allocations",
    ),
    "fulfillment": (
        "walmart_order_inventory_sync", "operations_work_queue",
        "marketplace_picking_tasks",
    ),
}


def fingerprint(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return {"exists": False, "count": 0, "sha256": None}
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return {"exists": True, "count": count, "columns": columns, "sha256": digest.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        result = {
            "database": str(database),
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "groups": {
                group: {table: fingerprint(connection, table) for table in tables}
                for group, tables in TABLE_GROUPS.items()
            },
            "marketplace_sync_runs": connection.execute(
                "SELECT COUNT(*) FROM marketplace_sync_runs"
            ).fetchone()[0],
            "operations_report_objects": [
                list(row) for row in connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE tbl_name IN ('operations_report_runs','operations_report_jobs') "
                    "ORDER BY type,name"
                )
            ],
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
