#!/usr/bin/env python
"""Run a controlled post-cutover simulation on a copied BrooksHouse database."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.migrations.channel_inventory_engine_schema import apply_to_copy
from app.services.approved_mapping_application import integrity_check, inventory_fingerprint
from app.services.channel_inventory_controls import effective_control, set_copy_control
from app.services.channel_inventory_engine import apply_sale_to_copy, connect_copy
from app.services.channel_inventory_preflight import build_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    apply_to_copy(args.database)
    cutoff_dt = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = cutoff_dt.isoformat()
    after = (cutoff_dt + timedelta(seconds=1)).isoformat()
    before = (cutoff_dt - timedelta(days=1)).isoformat()
    connection = connect_copy(args.database)
    try:
        integrity_before = integrity_check(connection)
        inventory_before = inventory_fingerprint(connection)
        product = connection.execute(
            """SELECT i.product_id FROM inventory i JOIN inventory_locations l USING(location_id)
                WHERE lower(l.location_name)=lower('BrooksHouse Storefront')
                  AND i.quantity_on_hand-coalesce(i.quantity_reserved,0)>=2 ORDER BY i.product_id LIMIT 1"""
        ).fetchone()
        if not product:
            raise RuntimeError("No product with two available Storefront units for controlled simulation")
        product_id = int(product[0])
        for suffix, timestamp in (("PRE", before), ("POST", after)):
            order_id, line_id = f"CODEX-SIM-{suffix}-ORDER", f"CODEX-SIM-{suffix}-LINE"
            connection.execute(
                """INSERT INTO shopify_sales_orders(shopify_order_id,order_name,order_number,processed_at,updated_at,
                   cancelled_at,source_name,pos_location_id,financial_status,fulfillment_status,currency,subtotal_amount,
                   discount_amount,tax_amount,total_amount,refund_amount,test_order,raw_json,first_imported_at,last_imported_at)
                   VALUES(?,?,?, ?,?,NULL,'codex-copy-simulation',NULL,'PAID','UNFULFILLED','USD',1,0,0,1,0,0,'{}',?,?)""",
                (order_id, order_id, order_id, timestamp, timestamp, timestamp, timestamp))
            connection.execute(
                """INSERT INTO shopify_sales_lines(shopify_line_id,shopify_order_id,product_id,sku,title,quantity,
                   current_quantity,unit_price,discount_amount,net_amount,match_status,match_method,inventory_applied,created_at,updated_at)
                   VALUES(?,?,?,'CODEX-SIM','Controlled copy-only cohort',1,1,1,0,1,'matched','controlled_copy',0,?,?)""",
                (line_id, order_id, product_id, timestamp, timestamp))
        connection.commit()
    finally:
        connection.close()
    set_copy_control(args.database, "global", mode="enabled", paused=False, cutover_at=cutoff,
                     source_checkpoint=cutoff, reason="controlled copy simulation")
    set_copy_control(args.database, "shopify", mode="enabled", paused=False, cutover_at=cutoff,
                     source_checkpoint=cutoff, reason="controlled copy simulation")
    report = build_report(args.database, cutoff=cutoff)
    post_rows = [row for row in report["rows"] if row["order_id"].startswith("CODEX-SIM-")]
    first = apply_sale_to_copy(args.database, "shopify", "CODEX-SIM-POST-ORDER", "CODEX-SIM-POST-LINE")
    second = apply_sale_to_copy(args.database, "shopify", "CODEX-SIM-POST-ORDER", "CODEX-SIM-POST-LINE")
    set_copy_control(args.database, "shopify", mode="enabled", paused=True, cutover_at=cutoff,
                     source_checkpoint=cutoff, reason="pause behavior test")
    connection = connect_copy(args.database)
    try:
        paused = effective_control(connection, "shopify")
        integrity_after = integrity_check(connection)
        inventory_after = inventory_fingerprint(connection)
        ledger = int(connection.execute("SELECT COUNT(*) FROM channel_inventory_ledger WHERE order_id LIKE 'CODEX-SIM-%'").fetchone()[0])
    finally:
        connection.close()
    set_copy_control(args.database, "shopify", mode="dry_run", paused=False, cutover_at=cutoff,
                     source_checkpoint=cutoff, reason="dry-run behavior test")
    connection = connect_copy(args.database)
    try:
        dry_run = effective_control(connection, "shopify")
    finally:
        connection.close()
    payload = {"database": str(args.database.resolve()), "cutover": cutoff,
               "precutover_excluded": not any(row["order_id"] == "CODEX-SIM-PRE-ORDER" for row in post_rows),
               "postcutover_rows": post_rows, "first_apply": first, "idempotent_rerun": second,
               "paused_control": paused, "dry_run_control": dry_run, "controlled_ledger_events": ledger,
               "integrity_before": integrity_before, "integrity_after": integrity_after,
               "inventory_before": inventory_before, "inventory_after": inventory_after}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("cutover", "precutover_excluded", "controlled_ledger_events",
                                               "integrity_before", "integrity_after")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
