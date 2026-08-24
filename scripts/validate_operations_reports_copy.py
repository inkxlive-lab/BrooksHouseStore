from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from app.marketplace_order_service import load_marketplace_orders
from app.operations_reports import create_report_snapshot, load_snapshot
from app.services.marketplace_order_ingestion import ensure_marketplace_operations_schema


def fingerprint(connection: sqlite3.Connection) -> dict:
    result = {}
    for table in ("inventory", "inventory_transactions", "channel_inventory_ledger", "channel_inventory_allocations"):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            result[table] = {"count": 0, "sha256": None}
            continue
        rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        encoded = json.dumps([list(row) for row in rows], default=str, separators=(",", ":")).encode()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    protected = (Path("app/data/brookshouse_store.db").resolve())
    if database == protected:
        raise RuntimeError("Refusing to validate against the protected database")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    before = fingerprint(connection)
    ensure_marketplace_operations_schema(connection)
    connection.commit()
    connection.close()
    orders = load_marketplace_orders(database, allow_fixture=True)
    active = [order for order in orders if order.get("is_actionable")]
    history = [order for order in orders if order.get("is_terminal")]
    run_id = create_report_snapshot(
        report_type="master_pull",
        filters={"channel": "all", "physical_site": "all", "stage": "all", "include_staged": True},
        freshness={"channels": {"walmart": {"stale": True}, "amazon": {"stale": True}}},
        warnings=["COPY VALIDATION: live channel refresh intentionally not performed"],
        actor="Codex copied-database validation",
        database=database,
        allow_fixture=True,
    )
    metadata, snapshot = load_snapshot(run_id, database, allow_fixture=True)
    connection = sqlite3.connect(database)
    after = fingerprint(connection)
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    schema = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    connection.close()
    result = {
        "database": str(database), "integrity_check": integrity,
        "active_orders": len(active), "history_orders": len(history),
        "report_run_id": run_id, "snapshot_sha256": metadata["snapshot_sha256"],
        "snapshot_totals": snapshot["totals"], "warnings": snapshot["warnings"],
        "required_tables_present": all(name in schema for name in ("marketplace_status_audit", "operations_report_runs")),
        "inventory_fingerprint_before": before, "inventory_fingerprint_after": after,
        "inventory_fingerprints_unchanged": before == after,
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if integrity == "ok" and before == after and result["required_tables_present"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
