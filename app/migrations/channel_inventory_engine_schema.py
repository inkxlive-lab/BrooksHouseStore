"""Idempotent, copy-only schema migration for the channel inventory engine."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path

from app.services.channel_inventory_engine import PRODUCTION_DB, ProductionWriteRefused


REQUIRED_SOURCE_COLUMNS = {
    "inventory": {"inventory_id", "product_id", "location_id", "quantity_on_hand", "quantity_reserved"},
    "inventory_locations": {"location_id", "location_name", "active"},
    "products": {"product_id"},
    "shopify_sales_orders": {"shopify_order_id", "processed_at", "last_imported_at", "cancelled_at", "financial_status", "fulfillment_status", "test_order"},
    "shopify_sales_lines": {"shopify_line_id", "shopify_order_id", "product_id", "quantity", "current_quantity", "inventory_applied", "match_status"},
    "amazon_order_history": {"amazon_order_id", "created_time", "last_updated_time", "fulfillment_status", "fulfilled_by", "synced_at"},
    "amazon_order_item_history": {"amazon_order_id", "order_item_id", "product_id", "quantity_ordered", "synced_at"},
    "walmart_orders": {"purchase_order_id", "order_date", "walmart_status", "synced_at"},
    "walmart_order_lines": {"order_line_id", "purchase_order_id", "line_number", "product_id", "quantity", "line_status"},
}

TABLES = (
    "channel_inventory_ledger", "channel_inventory_allocations",
    "channel_inventory_allocation_inventory", "channel_inventory_event_transactions",
    "channel_inventory_engine_control", "channel_inventory_run_log",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS channel_inventory_ledger (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT NOT NULL,
 order_id TEXT NOT NULL, order_line_id TEXT NOT NULL, event_type TEXT NOT NULL,
 product_id INTEGER, ordered_quantity INTEGER NOT NULL DEFAULT 0,
 quantity_change INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL,
 source_version TEXT, allocation_id INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,
 UNIQUE(channel_name, order_id, order_line_id, event_type)
);
CREATE TABLE IF NOT EXISTS channel_inventory_allocations (
 allocation_id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT NOT NULL,
 order_id TEXT NOT NULL, order_line_id TEXT NOT NULL, product_id INTEGER NOT NULL,
 ordered_quantity INTEGER NOT NULL, deducted_quantity INTEGER NOT NULL DEFAULT 0,
 staged_quantity INTEGER NOT NULL DEFAULT 0, unlocated_quantity INTEGER NOT NULL DEFAULT 0,
 restored_quantity INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, source_version TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(channel_name, order_id, order_line_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_allocation_inventory (
 allocation_inventory_id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER NOT NULL,
 inventory_id INTEGER NOT NULL, deducted_quantity INTEGER NOT NULL DEFAULT 0,
 staged_quantity INTEGER NOT NULL DEFAULT 0, reserved_quantity INTEGER NOT NULL DEFAULT 0,
 UNIQUE(allocation_id, inventory_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_event_transactions (
 event_id INTEGER NOT NULL, inventory_transaction_id INTEGER NOT NULL UNIQUE,
 inventory_id INTEGER NOT NULL, quantity_change INTEGER NOT NULL,
 PRIMARY KEY(event_id, inventory_transaction_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_engine_control (
 scope TEXT PRIMARY KEY CHECK(scope IN ('global','shopify','walmart','amazon')),
 mode TEXT NOT NULL CHECK(mode IN ('disabled','dry_run','enabled')),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
 cutover_at TEXT, source_checkpoint TEXT, reason TEXT,
 updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_inventory_run_log (
 run_id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT,
 mode TEXT NOT NULL, cutover_at TEXT, source_checkpoint TEXT,
 started_at TEXT NOT NULL, completed_at TEXT, outcome TEXT NOT NULL,
 eligible_count INTEGER NOT NULL DEFAULT 0, applied_count INTEGER NOT NULL DEFAULT 0,
 review_count INTEGER NOT NULL DEFAULT 0, error_text TEXT
);
CREATE INDEX IF NOT EXISTS ix_channel_ledger_order ON channel_inventory_ledger(channel_name,order_id,order_line_id);
CREATE INDEX IF NOT EXISTS ix_channel_ledger_created ON channel_inventory_ledger(created_at);
CREATE INDEX IF NOT EXISTS ix_channel_allocations_status ON channel_inventory_allocations(status,product_id);
CREATE INDEX IF NOT EXISTS ix_channel_allocation_inventory_allocation ON channel_inventory_allocation_inventory(allocation_id);
CREATE INDEX IF NOT EXISTS ix_channel_event_transactions_event ON channel_inventory_event_transactions(event_id);
CREATE INDEX IF NOT EXISTS ix_channel_run_log_started ON channel_inventory_run_log(started_at);
"""


@dataclass(frozen=True)
class MigrationPreview:
    database: str
    production: bool
    integrity: str
    missing_prerequisites: tuple[str, ...]
    tables_to_create: tuple[str, ...]
    tables_already_present: tuple[str, ...]
    row_counts: dict[str, int]
    safe_to_apply_to_copy: bool

    def as_dict(self):
        return asdict(self)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def preview(database: str | Path) -> MigrationPreview:
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        missing = []
        for table, columns in REQUIRED_SOURCE_COLUMNS.items():
            found = _columns(connection, table)
            if not found:
                missing.append(f"missing table {table}")
            else:
                missing.extend(f"missing column {table}.{column}" for column in sorted(columns - found))
        present = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        counts = {table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                  for table in TABLES if table in present}
        is_production = path == PRODUCTION_DB
        return MigrationPreview(str(path), is_production, integrity, tuple(missing),
                                tuple(t for t in TABLES if t not in present),
                                tuple(t for t in TABLES if t in present), counts,
                                integrity == "ok" and not missing and not is_production)
    finally:
        connection.close()


def apply_to_copy(database: str | Path) -> MigrationPreview:
    path = Path(database).resolve()
    before = preview(path)
    if before.production:
        raise ProductionWriteRefused(f"Migration refuses production database: {path}")
    if before.integrity != "ok" or before.missing_prerequisites:
        raise RuntimeError(f"Migration prerequisites failed: {before.as_dict()}")
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL + "\nCOMMIT;")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return preview(path)
