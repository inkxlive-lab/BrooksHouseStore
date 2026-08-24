from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.operations_reports import load_snapshot


def _write(report_run_id: int, destination: Path, *, pull: bool, database: Path | None = None) -> None:
    _, snapshot = load_snapshot(report_run_id, database, allow_fixture=database is not None)
    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        if not pull:
            writer.writerow(["Channel", "Marketplace order ID", "Marketplace lifecycle status", "BrooksHouse fulfillment stage",
                "Ordered/received time (Central)", "Ship-by (Central)", "Overdue", "Product", "SKU", "UPC/barcode",
                "Quantity required", "Quantity picked", "Quantity packed", "Quantity staged", "Quantity shipped",
                "Mapping status", "Inventory readiness", "Exception/reason", "Repair action"])
            for row in snapshot.get("order_rows", []):
                writer.writerow([row["channel"], row["order_id"], row["marketplace_status"], row["fulfillment_stage"],
                    row["ordered_time_central"], row["ship_by_central"], "YES" if row["overdue"] else "NO",
                    row["product"], row["sku"], row["barcode"], row["quantity_required"], row["quantity_picked"],
                    row["quantity_packed"], row["quantity_staged"], row["quantity_shipped"], row["mapping_status"],
                    row["inventory_readiness"], row["exception"], row["action_url"]])
            return
        writer.writerow(["Product", "UPC/barcode", "Marketplace SKU", "Total units required", "Units already picked/staged",
            "Remaining units to pull", "Total available", "Contributing orders and ship deadlines",
            "Exact site/location/container/tote quantities", "Recommended pull location", "Shortage quantity",
            "Mapping/exception status", "Repair action"])
        for row in snapshot.get("pull_rows", []):
            recommended = row.get("recommended_location") or {}
            writer.writerow([row["product"], row["barcode"], "; ".join(row["skus"]), row["units_required"],
                row["units_picked_staged"], row["remaining_to_pull"], row["available_units"],
                "; ".join(f"{item['channel']} {item['order_id']} due {item['ship_by_central']}" for item in row["orders"]),
                "; ".join(f"{item['site']} / {item['location']} / {item['container']} / {item['tote'] or 'no tote'} = {item['available']}" for item in row["locations"]),
                f"{recommended.get('site', '')} / {recommended.get('location', '')} / {recommended.get('container', '')} ({recommended.get('available', 0)})" if recommended else "",
                row["shortage_quantity"], row["exception"] or row["mapping_status"], row["action_url"]])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--due-run", type=int, required=True)
    parser.add_argument("--pull-run", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    due = args.directory / f"marketplace-orders-due-today-run-{args.due_run}.csv"
    pull = args.directory / f"marketplace-master-pull-run-{args.pull_run}.csv"
    database = args.database.resolve() if args.database else None
    _write(args.due_run, due, pull=False, database=database)
    _write(args.pull_run, pull, pull=True, database=database)
    print(f"due_today={due}")
    print(f"master_pull={pull}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
