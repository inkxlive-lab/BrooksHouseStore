"""Idempotent, copy-only schema migration for the channel inventory engine."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path

from app.services.channel_inventory_engine import PRODUCTION_DB, ProductionWriteRefused
from app.services.approved_mapping_application import file_sha256, integrity_check, inventory_fingerprint, verified_backup


REQUIRED_SOURCE_COLUMNS = {
    "inventory": {"inventory_id", "product_id", "location_id", "quantity_on_hand", "quantity_reserved"},
    "inventory_locations": {"location_id", "location_name", "active"},
    "products": {"product_id"},
    "shopify_sales_orders": {"shopify_order_id", "processed_at", "last_imported_at", "cancelled_at", "financial_status", "fulfillment_status", "test_order"},
    "shopify_sales_lines": {"shopify_line_id", "shopify_order_id", "product_id", "sku", "title", "quantity", "current_quantity", "inventory_applied", "match_status", "updated_at"},
    "amazon_order_history": {"amazon_order_id", "created_time", "last_updated_time", "fulfillment_status", "fulfilled_by", "synced_at"},
    "amazon_order_item_history": {"amazon_order_id", "order_item_id", "product_id", "seller_sku", "asin", "title", "quantity_ordered", "synced_at"},
    "amazon_listings": {"amazon_listing_id", "seller_sku", "asin"},
    "amazon_product_links": {"amazon_product_link_id", "amazon_listing_id", "product_id", "match_status"},
    "walmart_orders": {"purchase_order_id", "order_date", "walmart_status", "synced_at"},
    "walmart_order_lines": {"order_line_id", "purchase_order_id", "line_number", "product_id", "sku", "item_name", "quantity", "line_status"},
    "walmart_listings": {"walmart_listing_id", "seller_sku"},
    "walmart_product_links": {"walmart_product_link_id", "walmart_listing_id", "product_id", "match_status"},
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
 CHECK(channel_name IN ('shopify','amazon','walmart')), CHECK(ordered_quantity>=0),
 CHECK(length(trim(event_type))>0), UNIQUE(channel_name, order_id, order_line_id, event_type),
 FOREIGN KEY(product_id) REFERENCES products(product_id),
 FOREIGN KEY(allocation_id) REFERENCES channel_inventory_allocations(allocation_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_allocations (
 allocation_id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT NOT NULL,
 order_id TEXT NOT NULL, order_line_id TEXT NOT NULL, product_id INTEGER NOT NULL,
 ordered_quantity INTEGER NOT NULL, deducted_quantity INTEGER NOT NULL DEFAULT 0,
 staged_quantity INTEGER NOT NULL DEFAULT 0, unlocated_quantity INTEGER NOT NULL DEFAULT 0,
 restored_quantity INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, source_version TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(channel_name IN ('shopify','amazon','walmart')),
 CHECK(ordered_quantity>=0 AND deducted_quantity>=0 AND staged_quantity>=0 AND unlocated_quantity>=0 AND restored_quantity>=0),
 CHECK(status IN ('deducted','replenishment_needed','unlocated','staged','cancelled')),
 UNIQUE(channel_name, order_id, order_line_id), FOREIGN KEY(product_id) REFERENCES products(product_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_allocation_inventory (
 allocation_inventory_id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER NOT NULL,
 inventory_id INTEGER NOT NULL, deducted_quantity INTEGER NOT NULL DEFAULT 0,
 staged_quantity INTEGER NOT NULL DEFAULT 0, reserved_quantity INTEGER NOT NULL DEFAULT 0,
 CHECK(deducted_quantity>=0 AND staged_quantity>=0 AND reserved_quantity>=0),
 UNIQUE(allocation_id, inventory_id),
 FOREIGN KEY(allocation_id) REFERENCES channel_inventory_allocations(allocation_id),
 FOREIGN KEY(inventory_id) REFERENCES inventory(inventory_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_event_transactions (
 event_id INTEGER NOT NULL, inventory_transaction_id INTEGER NOT NULL UNIQUE,
 inventory_id INTEGER NOT NULL, quantity_change INTEGER NOT NULL,
 PRIMARY KEY(event_id, inventory_transaction_id),
 FOREIGN KEY(event_id) REFERENCES channel_inventory_ledger(event_id),
 FOREIGN KEY(inventory_transaction_id) REFERENCES inventory_transactions(transaction_id),
 FOREIGN KEY(inventory_id) REFERENCES inventory(inventory_id)
);
CREATE TABLE IF NOT EXISTS channel_inventory_engine_control (
 scope TEXT PRIMARY KEY CHECK(scope IN ('global','shopify','walmart','amazon')),
 mode TEXT NOT NULL CHECK(mode IN ('disabled','dry_run','enabled')),
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
 cutover_at TEXT, source_checkpoint TEXT, reason TEXT,
 allocation_policy TEXT NOT NULL DEFAULT 'single_location_only' CHECK(allocation_policy IN ('single_location_only','ordered_multi_location')),
 eligible_locations_json TEXT NOT NULL DEFAULT '["BrooksHouse Storefront","Store Back Room"]',
 updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_inventory_run_log (
 run_id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT,
 mode TEXT NOT NULL, cutover_at TEXT, source_checkpoint TEXT,
 started_at TEXT NOT NULL, completed_at TEXT, outcome TEXT NOT NULL,
 eligible_count INTEGER NOT NULL DEFAULT 0, applied_count INTEGER NOT NULL DEFAULT 0,
 review_count INTEGER NOT NULL DEFAULT 0, error_text TEXT
 ,CHECK(mode IN ('disabled','dry_run','enabled')),
 CHECK(outcome IN ('started','completed','failed','suppressed')),
 CHECK(eligible_count>=0 AND applied_count>=0 AND review_count>=0)
);
CREATE INDEX IF NOT EXISTS ix_channel_ledger_order ON channel_inventory_ledger(channel_name,order_id,order_line_id);
CREATE INDEX IF NOT EXISTS ix_channel_ledger_created ON channel_inventory_ledger(created_at);
CREATE INDEX IF NOT EXISTS ix_channel_allocations_status ON channel_inventory_allocations(status,product_id);
CREATE INDEX IF NOT EXISTS ix_channel_allocation_inventory_allocation ON channel_inventory_allocation_inventory(allocation_id);
CREATE INDEX IF NOT EXISTS ix_channel_event_transactions_event ON channel_inventory_event_transactions(event_id);
CREATE INDEX IF NOT EXISTS ix_channel_event_transactions_inventory ON channel_inventory_event_transactions(inventory_id);
CREATE INDEX IF NOT EXISTS ix_channel_allocation_inventory_inventory ON channel_inventory_allocation_inventory(inventory_id);
CREATE INDEX IF NOT EXISTS ix_channel_run_log_started ON channel_inventory_run_log(started_at);
INSERT OR IGNORE INTO channel_inventory_engine_control
 (scope,mode,paused,reason,updated_by,updated_at) VALUES
 ('global','disabled',1,'infrastructure installed disabled','migration',CURRENT_TIMESTAMP),
 ('shopify','disabled',1,'infrastructure installed disabled','migration',CURRENT_TIMESTAMP),
 ('amazon','disabled',1,'infrastructure installed disabled','migration',CURRENT_TIMESTAMP),
 ('walmart','disabled',1,'infrastructure installed disabled','migration',CURRENT_TIMESTAMP);
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
    _apply_schema(path, cutover_at=None, checkpoints={})
    return preview(path)


def _apply_schema(path: Path, cutover_at: str | None, checkpoints: dict[str, str]) -> None:
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL + "\nCOMMIT;")
        now = datetime.now(timezone.utc).isoformat()
        for scope in ("global", "shopify", "amazon", "walmart"):
            checkpoint = checkpoints.get(scope) or (cutover_at if scope != "global" else None)
            connection.execute(
                """UPDATE channel_inventory_engine_control SET mode='disabled',paused=1,cutover_at=?,
                   source_checkpoint=?,reason='infrastructure installed disabled',updated_by='migration',updated_at=?
                   WHERE scope=?""",
                (cutover_at, checkpoint, now, scope))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def prepare_guarded_migration(database: str | Path, backup: str | Path, *, cutover_at: str,
                              checkpoints: dict[str, str]) -> dict:
    path = Path(database).resolve()
    if not cutover_at or set(checkpoints) != {"shopify", "amazon", "walmart"} or not all(checkpoints.values()):
        raise ValueError("Cutover timestamp and all per-channel checkpoints are required")
    before = preview(path)
    if before.integrity != "ok" or before.missing_prerequisites:
        raise RuntimeError("Migration preflight failed")
    backup_result = verified_backup(path, backup)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        baseline = inventory_fingerprint(connection)
        if "channel_inventory_engine_control" in before.tables_already_present:
            enabled = connection.execute("SELECT COUNT(*) FROM channel_inventory_engine_control WHERE mode='enabled' AND paused=0").fetchone()[0]
            if enabled:
                raise RuntimeError("Engine must be disabled before migration")
    finally:
        connection.close()
    return {"database": str(path), "source_sha256": file_sha256(path), "integrity_check": before.integrity,
            "backup": backup_result, "inventory_baseline": baseline,
            "inventory_transaction_high_water_mark": baseline["inventory_transaction_max_id"],
            "cutover_at": cutover_at, "checkpoints": checkpoints, "engine_required_state": "disabled",
            "prepared_at": datetime.now(timezone.utc).isoformat()}


def apply_guarded_migration(database: str | Path, manifest: dict, *, confirmation: str) -> MigrationPreview:
    path = Path(database).resolve()
    if confirmation != "INSTALL DISABLED INFRASTRUCTURE ONLY":
        raise RuntimeError("Exact infrastructure-only confirmation is required")
    if str(path) != str(manifest.get("database")) or file_sha256(path) != manifest.get("source_sha256"):
        raise RuntimeError("Source path or fingerprint changed after migration preparation")
    backup = Path(str(manifest.get("backup", {}).get("path", ""))).resolve()
    if not backup.exists() or file_sha256(backup) != manifest.get("backup", {}).get("sha256"):
        raise RuntimeError("Verified backup is missing or changed")
    backup_connection = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    try:
        if integrity_check(backup_connection) != "ok":
            raise RuntimeError("Backup integrity check failed")
    finally:
        backup_connection.close()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if integrity_check(connection) != "ok" or inventory_fingerprint(connection) != manifest.get("inventory_baseline"):
            raise RuntimeError("Integrity or inventory baseline changed after preparation")
    finally:
        connection.close()
    _apply_schema(path, str(manifest["cutover_at"]), dict(manifest["checkpoints"]))
    after = preview(path)
    if any(after.row_counts.get(table, 0) for table in ("channel_inventory_ledger","channel_inventory_allocations",
                                                        "channel_inventory_allocation_inventory","channel_inventory_event_transactions")):
        raise RuntimeError("Infrastructure migration unexpectedly created historical engine records")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if inventory_fingerprint(connection) != manifest.get("inventory_baseline"):
            raise RuntimeError("Infrastructure migration changed inventory or inventory transactions")
        unsafe_controls = connection.execute(
            "SELECT COUNT(*) FROM channel_inventory_engine_control WHERE mode<>'disabled' OR paused<>1").fetchone()[0]
        if unsafe_controls:
            raise RuntimeError("Infrastructure migration did not leave every control disabled and paused")
    finally:
        connection.close()
    return after
