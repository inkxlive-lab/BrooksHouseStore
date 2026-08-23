"""Strictly read-only production-data dry-run review; never installs engine schema."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.services.approved_mapping_application import file_sha256, integrity_check, inventory_fingerprint
from app.services.channel_inventory_engine import (
    DEFAULT_ELIGIBLE_LOCATIONS, PRODUCTION_DB, _location_rows, _policy_deduction_plan,
    list_source_lines, load_source_line,
)
from app.services.channel_inventory_events import normalize_channel_event
from app.services.channel_inventory_mapping import validate_mapping


ENGINE_TABLES = ("channel_inventory_ledger", "channel_inventory_allocations",
                 "channel_inventory_allocation_inventory", "channel_inventory_event_transactions")


def _connect_read_only(database: str | Path) -> sqlite3.Connection:
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def safety_baseline(database: str | Path) -> dict:
    path = Path(database).resolve()
    connection = _connect_read_only(path)
    try:
        fingerprint = inventory_fingerprint(connection)
        locations = [dict(row) for row in connection.execute(
            """SELECT l.location_id,l.location_name,l.location_type,l.active,
                      COUNT(i.inventory_id) inventory_rows,
                      COALESCE(SUM(i.quantity_on_hand),0) quantity_on_hand,
                      COALESCE(SUM(i.quantity_reserved),0) quantity_reserved
                 FROM inventory_locations l LEFT JOIN inventory i USING(location_id)
                GROUP BY l.location_id,l.location_name,l.location_type,l.active
                ORDER BY l.location_name,l.location_id""")]
        installed = {table: _table_exists(connection, table) for table in
                     (*ENGINE_TABLES, "channel_inventory_engine_control", "channel_inventory_run_log")}
        engine_counts = {table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                         for table in ENGINE_TABLES if installed[table]}
        controls = []
        if installed["channel_inventory_engine_control"]:
            controls = [dict(row) for row in connection.execute(
                "SELECT scope,mode,paused,cutover_at,source_checkpoint,reason FROM channel_inventory_engine_control ORDER BY scope")]
        return {"captured_at": datetime.now(timezone.utc).isoformat(), "database": str(path),
                "is_expected_production_path": path == PRODUCTION_DB, "database_sha256": file_sha256(path),
                "integrity_check": integrity_check(connection), "inventory": fingerprint,
                "inventory_by_location": locations, "engine_tables_installed": installed,
                "engine_row_counts": engine_counts, "controls": controls,
                "effective_mutation_state": "disabled_uninstalled" if not all(installed.values()) else "controls_present"}
    finally:
        connection.close()


def _source_details(connection: sqlite3.Connection, channel: str, order_id: str, line_id: str) -> dict:
    if channel == "shopify":
        row = connection.execute(
            """SELECT l.barcode,o.financial_status,o.fulfillment_status,o.cancelled_at,o.processed_at,
                      l.quantity original_quantity,l.current_quantity
                 FROM shopify_sales_lines l JOIN shopify_sales_orders o USING(shopify_order_id)
                WHERE l.shopify_order_id=? AND l.shopify_line_id=?""", (order_id, line_id)).fetchone()
        state = f"{row['financial_status'] or ''}/{row['fulfillment_status'] or ''}".casefold()
        if row["cancelled_at"]: state = "cancelled"
        return {"marketplace_identifier": str(row["barcode"] or ""), "state": state,
                "source_timestamp": str(row["processed_at"] or ""),
                "quantity_discrepancy": int(row["original_quantity"] or 0) != int(row["current_quantity"] or 0)}
    if channel == "amazon":
        row = connection.execute(
            """SELECT i.asin,o.fulfillment_status,o.fulfilled_by,o.created_time
                 FROM amazon_order_item_history i JOIN amazon_order_history o USING(amazon_order_id)
                WHERE i.amazon_order_id=? AND i.order_item_id=?""", (order_id, line_id)).fetchone()
        return {"marketplace_identifier": str(row["asin"] or ""),
                "state": f"{row['fulfillment_status'] or ''} / {row['fulfilled_by'] or ''}",
                "source_timestamp": str(row["created_time"] or ""), "quantity_discrepancy": False}
    row = connection.execute(
        """SELECT l.upc,COALESCE(NULLIF(l.line_status,''),o.walmart_status) state,o.order_date
             FROM walmart_order_lines l JOIN walmart_orders o USING(purchase_order_id)
            WHERE l.purchase_order_id=? AND CAST(l.order_line_id AS TEXT)=?""", (order_id, line_id)).fetchone()
    return {"marketplace_identifier": str(row["upc"] or ""), "state": str(row["state"] or ""),
            "source_timestamp": str(row["order_date"] or ""), "quantity_discrepancy": False}


def _inventory_summary(connection: sqlite3.Connection, product_id: int | None) -> tuple[list[dict], list[dict]]:
    if product_id is None:
        return [], []
    rows = [dict(row) for row in connection.execute(
        """SELECT i.inventory_id,l.location_name,l.location_type,l.active,i.container_id,i.quantity_on_hand,i.quantity_reserved,
                  MAX(i.quantity_on_hand-COALESCE(i.quantity_reserved,0),0) available
             FROM inventory i JOIN inventory_locations l USING(location_id)
            WHERE i.product_id=? ORDER BY l.location_name,i.inventory_id""", (product_id,))]
    eligible = [row for row in rows if int(row["active"] or 0) == 1 and row["location_name"] in DEFAULT_ELIGIBLE_LOCATIONS]
    other = [row for row in rows if int(row["available"] or 0) > 0 and row not in eligible]
    return eligible, other


def _problem_category(mapping_status: str, mapping_reason: str, lifecycle_review: bool,
                      quantity_discrepancy: bool, eligible_available: int, other_available: int,
                      requested: int, allocation_possible: bool) -> str:
    if quantity_discrepancy: return "Marketplace quantity discrepancy"
    if mapping_status == "missing": return "Missing mapping"
    if mapping_status == "ambiguous": return "Ambiguous mapping"
    if mapping_status == "conflict": return "Conflicting mapping"
    if mapping_status == "stale_or_disabled" or "disabled" in mapping_status or "unsafe" in mapping_reason.casefold():
        return "Inactive/unsafe listing"
    if "product" in mapping_reason.casefold() and "missing" in mapping_reason.casefold(): return "Missing BrooksHouse product"
    if mapping_status != "matched": return "Other"
    if lifecycle_review: return "Lifecycle/event issue"
    if eligible_available == 0 and other_available > 0: return "Inventory exists only in a non-approved location"
    if eligible_available == 0: return "No eligible inventory"
    if eligible_available < requested: return "Insufficient eligible inventory"
    if not allocation_possible: return "Insufficient eligible inventory"
    return "None"


def _shopify_identifier_diagnostic(connection: sqlite3.Connection, cutoff: str) -> dict:
    counts = Counter()
    order_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(shopify_sales_orders)")}
    if "raw_json" not in order_columns:
        return {"diagnostic_unavailable": "shopify_sales_orders.raw_json is not present"}
    for order in connection.execute(
        "SELECT shopify_order_id,raw_json FROM shopify_sales_orders WHERE datetime(processed_at)>=datetime(?)",(cutoff,)):
        try:
            payload = json.loads(str(order["raw_json"] or "{}"))
        except json.JSONDecodeError:
            counts["invalid_raw_json_orders"] += 1; continue
        stored = {str(row["shopify_line_id"]):row for row in connection.execute(
            "SELECT shopify_line_id,sku,barcode FROM shopify_sales_lines WHERE shopify_order_id=?",(order["shopify_order_id"],))}
        for line in ((payload.get("lineItems") or {}).get("nodes") or []):
            counts["payload_lines"] += 1
            variant = line.get("variant") or {}
            payload_sku = str(variant.get("sku") or line.get("sku") or "").strip()
            payload_barcode = str(variant.get("barcode") or line.get("barcode") or "").strip()
            if payload_sku: counts["payload_sku_present"] += 1
            if payload_barcode: counts["payload_barcode_present"] += 1
            row = stored.get(str(line.get("id") or ""))
            if row and str(row["sku"] or "").strip(): counts["stored_sku_present"] += 1
            if row and str(row["barcode"] or "").strip(): counts["stored_barcode_present"] += 1
            if payload_sku and row and not str(row["sku"] or "").strip(): counts["sku_storage_mismatch"] += 1
            if payload_barcode and row and not str(row["barcode"] or "").strip(): counts["barcode_storage_mismatch"] += 1
            if not payload_sku and not payload_barcode: counts["source_missing_both"] += 1
    return dict(counts)


def run_production_review(database: str | Path, cutoff: str) -> dict:
    path = Path(database).resolve()
    before = safety_baseline(path)
    connection = _connect_read_only(path)
    try:
        lines = []
        for channel, order_id, line_id in list_source_lines(connection, cutoff):
            source = load_source_line(connection, channel, order_id, line_id)
            details = _source_details(connection, channel, order_id, line_id)
            product = connection.execute("SELECT product_name FROM products WHERE product_id=?",(source.product_id,)).fetchone() if source.product_id else None
            mapping = validate_mapping(connection, channel, source.product_id, source.sku, source.asin, source.mapping_status)
            normalized = normalize_channel_event(channel, "new_order", quantity=source.quantity, status=details["state"])
            eligible, other = _inventory_summary(connection, source.product_id)
            eligible_available = sum(max(int(row["available"]), 0) for row in eligible)
            other_available = sum(max(int(row["available"]), 0) for row in other)
            deductions = ()
            if mapping.safe and not normalized.requires_review and source.quantity > 0:
                deductions = _policy_deduction_plan(connection, int(source.product_id), source.quantity,
                                                     "single_location_only", DEFAULT_ELIGIBLE_LOCATIONS)
            would_deduct = sum(quantity for _, quantity in deductions)
            owed = source.quantity-would_deduct if mapping.safe and not normalized.requires_review else 0
            review_quantity = source.quantity if not mapping.safe or normalized.requires_review else 0
            category = _problem_category(mapping.status, mapping.reason, normalized.requires_review,
                                         bool(details["quantity_discrepancy"]), eligible_available,
                                         other_available, source.quantity, bool(deductions))
            selected_ids = {item[0] for item in deductions}
            lines.append({"channel": channel, "order_id": order_id, "order_line_id": line_id,
                          "marketplace_sku": source.sku, "marketplace_barcode_gtin_asin": details["marketplace_identifier"],
                          "marketplace_title": source.title,
                          "product_id": source.product_id, "mapping_validation_status": mapping.status,
                          "product_name": str(product[0] or "") if product else "",
                          "mapping_safe": mapping.safe, "requested_quantity": source.quantity,
                          "eligible_inventory": eligible_available,
                          "eligible_locations": [{"location": row["location_name"], "inventory_id": row["inventory_id"],
                                                  "container_id": row["container_id"],
                                                  "available": row["available"]} for row in eligible],
                          "allocation_policy": "single_location_only", "would_deduct_quantity": would_deduct,
                          "would_become_owed_quantity": owed, "unsafe_review_quantity": review_quantity,
                          "review_reason": mapping.reason if not mapping.safe else normalized.reason,
                          "problem_category": category,
                          "selected_inventory_rows": [{"inventory_id": row["inventory_id"], "location": row["location_name"],
                                                       "container_id": row["container_id"],
                                                       "quantity": dict(deductions)[row["inventory_id"]]}
                                                      for row in eligible if row["inventory_id"] in selected_ids],
                          "nonapproved_inventory": [{"inventory_id": row["inventory_id"], "location": row["location_name"],
                                                     "container_id": row["container_id"],
                                                     "available": row["available"]} for row in other],
                          "source_state": details["state"], "source_timestamp": details["source_timestamp"],
                          "current_open_candidate": not normalized.requires_review,
                          "mutation_permitted": False})
        shopify_diagnostic = _shopify_identifier_diagnostic(connection,cutoff)
    finally:
        connection.close()
    sku_frequency = Counter((line["channel"], line["marketplace_sku"]) for line in lines if line["marketplace_sku"])
    category_counts = defaultdict(Counter)
    for line in lines:
        category_counts[line["channel"]][line["problem_category"]] += 1
        line["affected_lines_for_sku"] = sku_frequency[(line["channel"], line["marketplace_sku"])] if line["marketplace_sku"] else 1
    queue = sorted((line for line in lines if line["problem_category"] != "None"),
                   key=lambda line: (line["current_open_candidate"], line["affected_lines_for_sku"],
                                     line["source_timestamp"]), reverse=True)
    after = safety_baseline(path)
    invariant_keys = ("database_sha256", "inventory", "engine_row_counts")
    zero_mutation = all(before[key] == after[key] for key in invariant_keys)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cutoff": cutoff,
            "strictly_read_only": True, "controls_confirmation": {
                "infrastructure_installed": all(before["engine_tables_installed"].values()),
                "effective_state": before["effective_mutation_state"], "active_deduction": False},
            "baseline_before": before, "baseline_after": after, "zero_mutation_verified": zero_mutation,
            "order_line_count": len(lines), "would_deduct_quantity": sum(x["would_deduct_quantity"] for x in lines),
            "owed_quantity": sum(x["would_become_owed_quantity"] for x in lines),
            "unsafe_review_quantity": sum(x["unsafe_review_quantity"] for x in lines),
            "problem_counts_by_channel": {channel: dict(counts) for channel, counts in category_counts.items()},
            "shopify_identifier_diagnostic": shopify_diagnostic,
            "lines": lines, "prioritized_review_queue": queue}


def write_production_review(database: str | Path, cutoff: str, output: str | Path) -> dict:
    report = run_production_review(database, cutoff)
    target = Path(output); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
