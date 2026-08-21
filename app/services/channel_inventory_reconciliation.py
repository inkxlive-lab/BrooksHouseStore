"""Read-only, cross-channel inventory reconciliation.

This module deliberately has no inventory mutation entry point.  It reads the
existing channel order/mapping tables and proposes one safe deduction source per
order line.  A future apply workflow must use ``channel_inventory_ledger`` (DDL
below) and perform ledger insertion plus inventory change in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS channel_inventory_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name TEXT NOT NULL,
    order_id TEXT NOT NULL,
    order_line_id TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'sale',
    product_id INTEGER NOT NULL,
    quantity_change INTEGER NOT NULL,
    inventory_id INTEGER NOT NULL,
    inventory_transaction_id INTEGER NOT NULL UNIQUE,
    source_updated_at TEXT,
    applied_at TEXT NOT NULL,
    UNIQUE(channel_name, order_id, order_line_id, event_type)
);
"""

EXCLUDED_LOCATION_TYPES = {
    "catalog", "container", "hold", "mobile_inventory", "mobile_storage",
    "reserved", "storage", "trailer", "warehouse",
}
RESERVED_LOCATION_NAME = "online orders / reserved"


@dataclass(frozen=True)
class ReconciliationRow:
    channel: str
    order_id: str
    order_line_id: str
    identifier: str
    product_id: int | None
    quantity_sold: int
    eligible_quantities: str
    deduction_inventory_id: int | None
    deduction_location: str
    deduction_container: str
    quantity_before: int | None
    quantity_after: int | None
    already_applied: bool
    match_status: str
    match_confidence: str
    lifecycle_event: str
    action: str
    review_reason: str

    def as_dict(self) -> dict:
        return asdict(self)


@contextmanager
def connect_read_only(database: str | Path):
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _text(value: object) -> str:
    return str(value or "").strip()


def _identifier(*pairs: tuple[str, object]) -> str:
    return " | ".join(f"{name}={_text(value)}" for name, value in pairs if _text(value))


def _channel_lines(connection: sqlite3.Connection, cutoff: str) -> Iterable[dict]:
    if _table_exists(connection, "shopify_sales_lines") and _table_exists(connection, "shopify_sales_orders"):
        for row in connection.execute(
            """SELECT 'shopify' channel,o.shopify_order_id order_id,
                      l.shopify_line_id order_line_id,l.sku,l.barcode,
                      l.shopify_variant_id marketplace_id,l.product_id,l.quantity,
                      l.current_quantity,l.match_status,l.match_method,
                      l.inventory_applied legacy_applied,o.cancelled_at,
                      o.financial_status,o.fulfillment_status,o.refund_amount,
                      o.processed_at source_updated_at,'' fulfilled_by,'' line_status
                 FROM shopify_sales_lines l JOIN shopify_sales_orders o
                   ON o.shopify_order_id=l.shopify_order_id
                WHERE o.test_order=0 AND o.processed_at>=?""", (cutoff,)
        ):
            yield dict(row)
    if _table_exists(connection, "amazon_order_item_history") and _table_exists(connection, "amazon_order_history"):
        for row in connection.execute(
            """SELECT 'amazon' channel,o.amazon_order_id order_id,
                      i.order_item_id order_line_id,i.seller_sku sku,'' barcode,
                      i.asin marketplace_id,i.product_id,i.quantity_ordered quantity,
                      i.quantity_ordered current_quantity,'' match_status,
                      'amazon_product_link' match_method,0 legacy_applied,
                      '' cancelled_at,'' financial_status,o.fulfillment_status,
                      0 refund_amount,o.last_updated_time source_updated_at,
                      o.fulfilled_by,'' line_status
                 FROM amazon_order_item_history i JOIN amazon_order_history o
                   ON o.amazon_order_id=i.amazon_order_id
                WHERE o.created_time>=?""", (cutoff,)
        ):
            yield dict(row)
    if _table_exists(connection, "walmart_order_lines") and _table_exists(connection, "walmart_orders"):
        for row in connection.execute(
            """SELECT 'walmart' channel,o.purchase_order_id order_id,
                      CAST(l.order_line_id AS TEXT) order_line_id,l.sku,l.upc barcode,
                      l.line_number marketplace_id,l.product_id,l.quantity,
                      l.quantity current_quantity,'' match_status,
                      'walmart_product_link' match_method,0 legacy_applied,
                      '' cancelled_at,'' financial_status,o.walmart_status fulfillment_status,
                      0 refund_amount,o.synced_at source_updated_at,'' fulfilled_by,
                      l.line_status
                 FROM walmart_order_lines l JOIN walmart_orders o
                   ON o.purchase_order_id=l.purchase_order_id
                WHERE (CASE WHEN trim(COALESCE(o.order_date,'')) GLOB '[0-9]*'
                            AND length(trim(COALESCE(o.order_date,'')))>=13
                            THEN datetime(CAST(o.order_date AS INTEGER)/1000,'unixepoch')
                            ELSE datetime(o.order_date) END)>=datetime(?)""", (cutoff,)
        ):
            yield dict(row)


def _lifecycle(row: dict) -> tuple[str, str]:
    status = " ".join((_text(row.get("fulfillment_status")), _text(row.get("line_status")))).casefold()
    if row["channel"] == "amazon" and _text(row.get("fulfilled_by")).casefold() in {"amazon", "afn", "fba"}:
        return "amazon_fulfilled", "Amazon-fulfilled inventory is not BrooksHouse stock"
    if row.get("cancelled_at") or "cancel" in status:
        return "cancellation", "cancelled line; do not deduct"
    original = max(int(row.get("quantity") or 0), 0)
    current = max(int(row.get("current_quantity") or 0), 0)
    if float(row.get("refund_amount") or 0) > 0 or current < original:
        return "refund_or_return", "refund/return requires merchandise restock confirmation"
    if any(word in status for word in ("return", "refund", "restock")):
        return "return_or_restock", "return/restock signal requires separate physical-restock review"
    return "sale", ""


def _match(row: dict) -> tuple[str, str, str]:
    product_id = row.get("product_id")
    stored_status = _text(row.get("match_status")).casefold()
    if product_id is None:
        return ("ambiguous" if stored_status == "ambiguous" else "unmatched", "none", "product is not uniquely matched")
    if stored_status == "ambiguous":
        return "ambiguous", "low", "stored mapping is ambiguous"
    method = _text(row.get("match_method")).casefold()
    confidence = "high" if method in {"barcode", "amazon_product_link", "walmart_product_link", "manual_queue"} else "medium"
    return "matched", confidence, ""


def _already_applied(connection: sqlite3.Connection, row: dict) -> bool:
    if int(row.get("legacy_applied") or 0):
        return True
    if not _table_exists(connection, "channel_inventory_ledger"):
        return False
    return connection.execute(
        """SELECT 1 FROM channel_inventory_ledger
            WHERE channel_name=? AND order_id=? AND order_line_id=?
              AND event_type='sale' LIMIT 1""",
        (row["channel"], _text(row["order_id"]), _text(row["order_line_id"])),
    ).fetchone() is not None


def _eligible_inventory(connection: sqlite3.Connection, product_id: int) -> list[dict]:
    pick_slot = None
    if _table_exists(connection, "product_pick_slots"):
        pick_slot = connection.execute(
            "SELECT location_id,container_id FROM product_pick_slots WHERE product_id=?",
            (product_id,),
        ).fetchone()
    rows = connection.execute(
        """SELECT i.inventory_id,i.location_id,l.location_name,l.location_type,
                  i.container_id,i.quantity_on_hand,i.quantity_reserved,l.active
             FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
            WHERE i.product_id=? ORDER BY i.inventory_id""", (product_id,)
    ).fetchall()
    eligible = []
    for item in rows:
        name = _text(item["location_name"])
        kind = _text(item["location_type"]).casefold()
        container = _text(item["container_id"])
        priority = None
        if name.casefold() == RESERVED_LOCATION_NAME:
            priority = 10
        elif pick_slot and int(item["location_id"]) == int(pick_slot["location_id"]) and container.casefold() == _text(pick_slot["container_id"]).casefold() and kind not in {"trailer", "mobile_inventory", "mobile_storage", "hold", "catalog"}:
            priority = 20
        elif kind == "store" and int(item["active"] or 0) == 1:
            priority = 30
        if priority is None or kind in EXCLUDED_LOCATION_TYPES and name.casefold() != RESERVED_LOCATION_NAME:
            continue
        on_hand = int(item["quantity_on_hand"] or 0)
        reserved = int(item["quantity_reserved"] or 0)
        available = on_hand if name.casefold() == RESERVED_LOCATION_NAME else max(on_hand - reserved, 0)
        eligible.append({
            "inventory_id": int(item["inventory_id"]), "location_id": int(item["location_id"]),
            "location": name, "container": container, "on_hand": on_hand,
            "reserved": reserved, "available": available, "priority": priority,
        })
    return sorted(eligible, key=lambda item: (item["priority"], item["inventory_id"]))


def reconcile(connection: sqlite3.Connection, cutoff: str) -> list[ReconciliationRow]:
    output = []
    for source in _channel_lines(connection, cutoff):
        quantity = max(int(source.get("quantity") or 0), 0)
        lifecycle, lifecycle_reason = _lifecycle(source)
        match_status, confidence, match_reason = _match(source)
        applied = _already_applied(connection, source)
        eligible = _eligible_inventory(connection, int(source["product_id"])) if source.get("product_id") is not None else []
        chosen = next((item for item in eligible if item["available"] >= quantity and quantity > 0), None)
        reasons = [reason for reason in (lifecycle_reason, match_reason) if reason]
        if applied:
            reasons.append("order line is already recorded as applied")
        if quantity <= 0:
            reasons.append("sale quantity is zero")
        if match_status == "matched" and lifecycle == "sale" and not applied and quantity > 0 and chosen is None:
            reasons.append("no single eligible sellable/pick location has sufficient stock")
        action = "deduct_preview" if not reasons and chosen else "review"
        output.append(ReconciliationRow(
            channel=source["channel"], order_id=_text(source["order_id"]),
            order_line_id=_text(source["order_line_id"]),
            identifier=_identifier(("sku", source.get("sku")), ("barcode", source.get("barcode")), ("marketplace_id", source.get("marketplace_id"))),
            product_id=int(source["product_id"]) if source.get("product_id") is not None else None,
            quantity_sold=quantity, eligible_quantities=json.dumps(eligible, separators=(",", ":")),
            deduction_inventory_id=chosen["inventory_id"] if chosen else None,
            deduction_location=chosen["location"] if chosen else "",
            deduction_container=chosen["container"] if chosen else "",
            quantity_before=chosen["on_hand"] if chosen else None,
            quantity_after=chosen["on_hand"] - quantity if chosen else None,
            already_applied=applied, match_status=match_status,
            match_confidence=confidence, lifecycle_event=lifecycle, action=action,
            review_reason="; ".join(reasons),
        ))
    return output
