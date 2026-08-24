from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

REQUIRED_TABLES = {"marketplace_status_audit", "operations_report_runs"}
REQUIRED_COLUMNS = {
    "walmart_orders": {"channel_closed_at", "last_verified_at", "terminal_reason"},
    "amazon_order_history": {"local_status", "channel_closed_at", "last_verified_at", "terminal_reason", "raw_json", "ship_by_date"},
}


def ensure_marketplace_operations_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS marketplace_sync_runs (
            sync_run_id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, success INTEGER,
            orders_discovered INTEGER NOT NULL DEFAULT 0, new_orders_inserted INTEGER NOT NULL DEFAULT 0,
            orders_updated INTEGER NOT NULL DEFAULT 0, lines_processed INTEGER NOT NULL DEFAULT 0,
            sanitized_database_target TEXT NOT NULL, error_message TEXT, worker_identity TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_marketplace_sync_runs_channel_time
            ON marketplace_sync_runs(channel, started_at DESC);
        CREATE TABLE IF NOT EXISTS marketplace_order_alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            marketplace_order_id TEXT NOT NULL, first_seen_at TEXT NOT NULL,
            marketplace_status TEXT, acknowledged_at TEXT, acknowledged_by TEXT,
            alert_state TEXT NOT NULL DEFAULT 'new', push_notified_at TEXT, push_error TEXT,
            UNIQUE(channel, marketplace_order_id)
        );
        CREATE INDEX IF NOT EXISTS ix_marketplace_order_alerts_state
            ON marketplace_order_alerts(alert_state, channel, first_seen_at DESC);
        CREATE TABLE IF NOT EXISTS marketplace_status_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            marketplace_order_id TEXT NOT NULL, old_marketplace_status TEXT,
            new_marketplace_status TEXT, old_local_status TEXT, new_local_status TEXT,
            channel_response TEXT, sync_run_id INTEGER, synchronized_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_marketplace_status_audit_order
            ON marketplace_status_audit(channel, marketplace_order_id, synchronized_at DESC);
        CREATE TABLE IF NOT EXISTS operations_report_runs (
            report_run_id INTEGER PRIMARY KEY AUTOINCREMENT, report_type TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT, filters_json TEXT NOT NULL,
            freshness_json TEXT NOT NULL, warnings_json TEXT NOT NULL, totals_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_operations_report_runs_created
            ON operations_report_runs(created_at DESC);
    """)
    for table, columns in {
        "walmart_orders": {"channel_closed_at": "TEXT", "last_verified_at": "TEXT", "terminal_reason": "TEXT"},
        "amazon_order_history": {"local_status": "TEXT NOT NULL DEFAULT 'new'", "channel_closed_at": "TEXT", "last_verified_at": "TEXT", "terminal_reason": "TEXT", "raw_json": "TEXT", "ship_by_date": "TEXT"},
    }.items():
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def inspect(database: Path) -> dict:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing_columns = {}
        for table, required in REQUIRED_COLUMNS.items():
            existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            missing_columns[table] = sorted(required - existing)
        return {"integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
                "missing_tables": sorted(REQUIRED_TABLES - tables), "missing_columns": missing_columns}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview/apply additive Operations Reports schema")
    parser.add_argument("database", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve()
    before = inspect(database)
    print("BEFORE", before)
    if not args.apply:
        print("PREVIEW ONLY: pass --apply with a verified --backup path to make additive schema changes")
        return 0
    if args.backup is None or not args.backup.resolve().is_file():
        raise RuntimeError("--apply requires an existing verified --backup database")
    backup_state = inspect(args.backup.resolve())
    if backup_state["integrity"] != "ok":
        raise RuntimeError("Backup integrity check failed")
    connection = sqlite3.connect(database, timeout=30)
    try:
        ensure_marketplace_operations_schema(connection)
        connection.commit()
    finally:
        connection.close()
    after = inspect(database)
    print("AFTER", after)
    if after["integrity"] != "ok" or after["missing_tables"] or any(after["missing_columns"].values()):
        raise RuntimeError("Schema validation failed after apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
