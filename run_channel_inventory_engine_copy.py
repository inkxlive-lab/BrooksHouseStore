#!/usr/bin/env python
"""Preview or exercise the cross-channel inventory engine on a copied DB only."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from app.services.approved_mapping_application import integrity_check, inventory_fingerprint
from app.services.channel_inventory_engine import (
    apply_sale_to_copy, assert_copy_database, connect_copy, initialize_copy_schema,
    list_source_lines, preview_line,
)
from app.services.channel_inventory_controls import effective_control


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--initialize-copy-schema", action="store_true")
    parser.add_argument("--apply-to-copy", action="store_true")
    args = parser.parse_args()
    database = assert_copy_database(args.database)
    if args.initialize_copy_schema:
        initialize_copy_schema(database)
    connection = connect_copy(database)
    try:
        before_integrity = integrity_check(connection)
        before_inventory = inventory_fingerprint(connection)
        keys = list_source_lines(connection, args.cutoff)
        plans = [preview_line(connection, *key) for key in keys]
        controls = {channel: effective_control(connection, channel) for channel in ("shopify", "amazon", "walmart")}
    finally:
        connection.close()
    results = []
    if args.apply_to_copy:
        for plan in plans:
            control = controls[plan.channel]
            if control["paused"] or control["mode"] != "enabled":
                results.append({"status": "suppressed_by_control", "channel": plan.channel,
                                "order_id": plan.order_id, "order_line_id": plan.order_line_id,
                                "control": control})
                continue
            # Each event re-previews under BEGIN IMMEDIATE because earlier events
            # in this copied-DB simulation legitimately change later availability.
            results.append(apply_sale_to_copy(database, plan.channel, plan.order_id, plan.order_line_id))
    connection = connect_copy(database)
    try:
        after_integrity = integrity_check(connection)
        after_inventory = inventory_fingerprint(connection)
        ledger_count = connection.execute("SELECT COUNT(*) FROM channel_inventory_ledger").fetchone()[0]
        allocation_count = connection.execute("SELECT COUNT(*) FROM channel_inventory_allocations").fetchone()[0]
    finally:
        connection.close()
    payload = {
        "mode": "apply_to_copy" if args.apply_to_copy else "preview", "database": str(database),
        "cutoff": args.cutoff, "integrity_before": before_integrity, "integrity_after": after_integrity,
        "inventory_before": before_inventory, "inventory_after": after_inventory,
        "plans": [plan.as_dict() for plan in plans], "results": results,
        "plan_actions": dict(Counter(plan.action for plan in plans)),
        "controls": controls,
        "result_statuses": dict(Counter(result["status"] for result in results)),
        "ledger_count": int(ledger_count), "allocation_count": int(allocation_count),
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("mode", "plan_actions", "result_statuses", "ledger_count", "allocation_count")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
