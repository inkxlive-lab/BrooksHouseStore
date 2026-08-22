"""Read-only production preflight for future channel inventory processing."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.services.channel_inventory_engine import (
    BACK_ROOM, PRODUCTION_DB, STOREFRONT, _deduction_plan,
    _location_rows, _replenishment_candidates,
)

TERMINAL_TOKENS = ("cancel", "refund", "return", "delivered")


@dataclass(frozen=True)
class PreviewRow:
    channel: str
    order_id: str
    order_line_id: str
    product_id: int | None
    sku: str
    quantity: int
    source_status: str
    source_version: str
    category: str
    proposed_action: str
    location_name: str
    legacy_overlap: str
    replenishment: tuple[dict, ...]
    reason: str

    def as_dict(self):
        return asdict(self)


def connect_read_only(database: str | Path = PRODUCTION_DB) -> sqlite3.Connection:
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def engine_install_state(connection: sqlite3.Connection) -> dict:
    required = ("channel_inventory_ledger", "channel_inventory_allocations",
                "channel_inventory_allocation_inventory", "channel_inventory_event_transactions",
                "channel_inventory_engine_control", "channel_inventory_run_log")
    found = tuple(table for table in required if _table_exists(connection, table))
    return {"installed": len(found) == len(required), "found": found,
            "missing": tuple(table for table in required if table not in found)}


def _legacy_overlap(connection: sqlite3.Connection, channel: str, order_id: str,
                    line_id: str, line_number: str, inventory_applied: int) -> str:
    if channel == "shopify" and inventory_applied:
        return "shopify_inventory_applied"
    if channel == "amazon" and _table_exists(connection, "amazon_order_inventory_sync"):
        row = connection.execute(
            "SELECT quantity_added FROM amazon_order_inventory_sync WHERE amazon_order_id=? AND order_item_id=?",
            (order_id, line_id)).fetchone()
        if row and int(row[0] or 0) > 0:
            return f"amazon_reserved:{int(row[0])}"
    if channel == "walmart" and _table_exists(connection, "walmart_order_inventory_sync"):
        row = connection.execute(
            "SELECT quantity_added FROM walmart_order_inventory_sync WHERE purchase_order_id=? AND line_number=?",
            (order_id, line_number)).fetchone()
        if row and int(row[0] or 0) > 0:
            return f"walmart_reserved:{int(row[0])}"
    return ""


def _source_rows(connection: sqlite3.Connection, cutoff: str):
    yield from connection.execute(
        """SELECT 'shopify' channel,o.shopify_order_id order_id,l.shopify_line_id line_id,
                  l.shopify_line_id line_number,l.product_id,l.sku,l.current_quantity quantity,
                  coalesce(o.financial_status,'')||' / '||coalesce(o.fulfillment_status,'') status,
                  coalesce(l.updated_at,o.updated_at,o.last_imported_at,'') source_version,
                  o.cancelled_at,l.inventory_applied,l.match_status,o.test_order
             FROM shopify_sales_lines l JOIN shopify_sales_orders o USING(shopify_order_id)
            WHERE datetime(o.processed_at)>=datetime(?) AND datetime(o.last_imported_at)>=datetime(?)""", (cutoff, cutoff))
    yield from connection.execute(
        """SELECT 'amazon',o.amazon_order_id,i.order_item_id,i.order_item_id,i.product_id,
                  i.seller_sku,i.quantity_ordered,coalesce(o.fulfillment_status,'')||' / '||coalesce(o.fulfilled_by,''),
                  coalesce(o.last_updated_time,i.synced_at,o.synced_at,''),NULL,0,
                  CASE WHEN i.product_id IS NULL THEN 'unmatched' ELSE 'matched' END,0
             FROM amazon_order_item_history i JOIN amazon_order_history o USING(amazon_order_id)
            WHERE datetime(o.created_time)>=datetime(?) AND datetime(o.synced_at)>=datetime(?)""", (cutoff, cutoff))
    yield from connection.execute(
        """SELECT 'walmart',o.purchase_order_id,cast(l.order_line_id as text),l.line_number,
                  l.product_id,l.sku,l.quantity,coalesce(nullif(l.line_status,''),o.walmart_status,''),
                  coalesce(o.synced_at,''),NULL,0,
                  CASE WHEN l.product_id IS NULL THEN 'unmatched' ELSE 'matched' END,0
             FROM walmart_order_lines l JOIN walmart_orders o USING(purchase_order_id)
            WHERE (CASE WHEN trim(coalesce(o.order_date,'')) GLOB '[0-9]*' AND length(trim(coalesce(o.order_date,'')))>=13
                        THEN datetime(cast(o.order_date AS INTEGER)/1000,'unixepoch') ELSE datetime(o.order_date) END)>=datetime(?)
              AND datetime(o.synced_at)>=datetime(?)""", (cutoff, cutoff))


def _classify(connection: sqlite3.Connection, row: sqlite3.Row) -> PreviewRow:
    channel, order_id, line_id = str(row[0]), str(row[1]), str(row[2])
    product_id = int(row[4]) if row[4] is not None else None
    quantity = max(int(row[6] or 0), 0)
    status = str(row[7] or "").strip()
    overlap = _legacy_overlap(connection, channel, order_id, line_id, str(row[3]), int(row[10] or 0))
    base = dict(channel=channel, order_id=order_id, order_line_id=line_id,
                product_id=product_id, sku=str(row[5] or ""), quantity=quantity,
                source_status=status, source_version=str(row[8] or ""), legacy_overlap=overlap)
    status_lower = status.casefold()
    status_parts = [part.strip() for part in status_lower.split("/")]
    eligible_state = (
        (channel == "shopify" and len(status_parts) >= 2
         and status_parts[0] in {"paid", "authorized", "partially_paid"}
         and status_parts[1] == "unfulfilled")
        or (channel == "amazon" and "unshipped" in status_lower and "merchant" in status_lower)
        or (channel == "walmart" and status_lower in {"created", "acknowledged"})
    )
    if row[9] or int(row[12] or 0) or not eligible_state or any(token in status_lower for token in TERMINAL_TOKENS):
        return PreviewRow(**base, category="lifecycle_review", proposed_action="review", location_name="",
                          replenishment=(), reason="Terminal/cancel/refund/fulfilled lifecycle state is never a new-sale deduction.")
    if overlap:
        return PreviewRow(**base, category="legacy_overlap", proposed_action="review", location_name="",
                          replenishment=(), reason="Legacy reservation/application marker overlaps this identity.")
    if product_id is None or str(row[11] or "").casefold() in {"unmatched", "ambiguous", "conflict"}:
        return PreviewRow(**base, category="unmatched_ambiguous", proposed_action="review", location_name="",
                          replenishment=(), reason="No safe BrooksHouse product mapping.")
    if quantity <= 0:
        return PreviewRow(**base, category="lifecycle_review", proposed_action="review", location_name="",
                          replenishment=(), reason="Non-positive current quantity requires lifecycle review.")
    product = connection.execute("SELECT 1 FROM products WHERE product_id=?", (product_id,)).fetchone()
    if not product:
        return PreviewRow(**base, category="stale_mapping", proposed_action="review", location_name="",
                          replenishment=(), reason="Mapped product no longer exists.")
    if _deduction_plan(_location_rows(connection, product_id, STOREFRONT), quantity):
        return PreviewRow(**base, category="storefront_fulfillable", proposed_action="deduct", location_name=STOREFRONT,
                          replenishment=(), reason="One eligible physical location covers the complete line.")
    if _deduction_plan(_location_rows(connection, product_id, BACK_ROOM), quantity):
        return PreviewRow(**base, category="back_room_fulfillable", proposed_action="deduct", location_name=BACK_ROOM,
                          replenishment=(), reason="One eligible physical location covers the complete line.")
    replenishment = _replenishment_candidates(connection, product_id)
    category = "replenishment_available" if sum(int(x["quantity_available"] or 0) for x in replenishment) >= quantity else "unavailable_company_wide"
    return PreviewRow(**base, category=category, proposed_action="reserve_owed", location_name="Online Orders / Reserved",
                      replenishment=replenishment,
                      reason="No eligible physical location covers the complete line; allocation remains owed, without fabricating on-hand stock.")


def build_report(database: str | Path = PRODUCTION_DB, *, cutoff: str) -> dict:
    connection = connect_read_only(database)
    try:
        rows = [_classify(connection, row) for row in _source_rows(connection, cutoff)]
        counts: dict[str, int] = {}
        units: dict[str, int] = {}
        for row in rows:
            counts[row.category] = counts.get(row.category, 0) + 1
            units[row.category] = units.get(row.category, 0) + row.quantity
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "cutoff": cutoff,
                "hypothetical_only": True, "engine": engine_install_state(connection),
                "total_order_lines": len(rows), "counts": counts, "units": units,
                "rows": [row.as_dict() for row in rows]}
    finally:
        connection.close()
