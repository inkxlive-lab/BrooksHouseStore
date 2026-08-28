"""Additive schema for the manual Marketplace Publish Center.

This module never resolves or opens the application database.  Callers must
provide an existing SQLite connection explicitly, which keeps schema changes
out of application startup and makes copied-database review mandatory.
"""

from __future__ import annotations

import sqlite3


REQUIRED_TABLES = {"marketplace_publish_queue", "marketplace_publish_events"}
REQUIRED_QUEUE_COLUMNS = {
    "shipping_weight_lb", "estimated_shipping_cost", "marketplace_fee_rate",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS marketplace_publish_queue (
    publish_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL CHECK(channel IN ('walmart','amazon')),
    product_id INTEGER NOT NULL,
    seller_sku TEXT NOT NULL,
    gtin TEXT NOT NULL,
    external_catalog_id TEXT,
    catalog_status TEXT NOT NULL,
    submission_type TEXT NOT NULL CHECK(submission_type IN ('offer','new_product')),
    selected_image_id INTEGER,
    proposed_price NUMERIC NOT NULL,
    proposed_quantity INTEGER NOT NULL,
    shipping_weight_lb NUMERIC,
    estimated_shipping_cost NUMERIC NOT NULL DEFAULT 6.00,
    marketplace_fee_rate NUMERIC,
    fulfillment_type TEXT NOT NULL DEFAULT 'merchant',
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    external_submission_id TEXT,
    validation_json TEXT NOT NULL DEFAULT '[]',
    request_json TEXT,
    response_json TEXT,
    error_message TEXT,
    submitted_at TEXT,
    processed_at TEXT,
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, product_id),
    UNIQUE(channel, seller_sku),
    UNIQUE(idempotency_key),
    FOREIGN KEY(product_id) REFERENCES products(product_id),
    FOREIGN KEY(selected_image_id) REFERENCES product_images(image_id),
    CHECK(proposed_quantity >= 0),
    CHECK(proposed_price >= 0),
    CHECK(shipping_weight_lb IS NULL OR shipping_weight_lb > 0),
    CHECK(estimated_shipping_cost >= 0),
    CHECK(marketplace_fee_rate IS NULL OR (marketplace_fee_rate >= 0 AND marketplace_fee_rate <= 100))
);
CREATE INDEX IF NOT EXISTS ix_marketplace_publish_queue_status
ON marketplace_publish_queue(status, channel, updated_at);
CREATE INDEX IF NOT EXISTS ix_marketplace_publish_queue_product
ON marketplace_publish_queue(product_id, channel);

CREATE TABLE IF NOT EXISTS marketplace_publish_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_id INTEGER,
    channel TEXT NOT NULL CHECK(channel IN ('walmart','amazon')),
    product_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    seller_sku TEXT,
    gtin TEXT,
    external_catalog_id TEXT,
    requested_price NUMERIC,
    requested_quantity INTEGER,
    external_submission_id TEXT,
    result TEXT NOT NULL,
    error_details TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(publish_id) REFERENCES marketplace_publish_queue(publish_id),
    FOREIGN KEY(product_id) REFERENCES products(product_id)
);
CREATE INDEX IF NOT EXISTS ix_marketplace_publish_events_publish
ON marketplace_publish_events(publish_id, event_id);

CREATE TRIGGER IF NOT EXISTS marketplace_publish_events_immutable_update
BEFORE UPDATE ON marketplace_publish_events
BEGIN
    SELECT RAISE(ABORT, 'marketplace_publish_events is immutable');
END;
CREATE TRIGGER IF NOT EXISTS marketplace_publish_events_immutable_delete
BEFORE DELETE ON marketplace_publish_events
BEGIN
    SELECT RAISE(ABORT, 'marketplace_publish_events is immutable');
END;
"""


def schema_installed(connection: sqlite3.Connection) -> bool:
    found = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            tuple(sorted(REQUIRED_TABLES)),
        )
    }
    if found != REQUIRED_TABLES:
        return False
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(marketplace_publish_queue)")
    }
    return REQUIRED_QUEUE_COLUMNS <= columns


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Install the additive schema on an explicitly selected database."""
    connection.executescript(SCHEMA_SQL)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(marketplace_publish_queue)")
    }
    for name, definition in (
        ("shipping_weight_lb", "NUMERIC"),
        ("estimated_shipping_cost", "NUMERIC NOT NULL DEFAULT 6.00"),
        ("marketplace_fee_rate", "NUMERIC"),
    ):
        if name not in columns:
            connection.execute(
                f'ALTER TABLE marketplace_publish_queue ADD COLUMN "{name}" {definition}'
            )

