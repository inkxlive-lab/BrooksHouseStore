"""Read-only, control-aware dry-run reporting against real channel state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.services.channel_inventory_controls import effective_control
from app.services.channel_inventory_engine import list_source_lines, preview_line


def build_dry_run(database: str | Path, cutoff: str) -> dict:
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        before_changes = int(connection.total_changes)
        rows = []
        for channel, order_id, line_id in list_source_lines(connection, cutoff):
            control = effective_control(connection, channel)
            locations = tuple(str(item) for item in control.get("eligible_locations") or ())
            policy = str(control.get("allocation_policy") or "single_location_only")
            if not locations:
                rows.append({"channel": channel, "order_id": order_id, "order_line_id": line_id,
                             "mapping_validation": "not_evaluated", "requested_quantity": None,
                             "eligible_locations": [], "physical_inventory_rows": [], "would_deduct": 0,
                             "would_restore": 0, "action": "suppressed", "reason": control["reason"],
                             "reconciliation_expectation": "no ledger, allocation, transaction, or inventory change"})
                continue
            plan = preview_line(connection, channel, order_id, line_id, policy, locations)
            rows.append({"channel": channel, "order_id": order_id, "order_line_id": line_id,
                         "product_id": plan.product_id,
                         "mapping_validation": "safe" if plan.product_id is not None else "unsafe",
                         "requested_quantity": plan.quantity, "allocation_policy": plan.allocation_policy,
                         "eligible_locations": list(plan.eligible_locations),
                         "physical_inventory_rows": [{"inventory_id": item[0], "quantity": item[1]}
                                                     for item in plan.inventory_deductions],
                         "would_deduct": sum(item[1] for item in plan.inventory_deductions),
                         "would_restore": 0, "action": plan.action, "reason": plan.reason,
                         "control_mode": control["mode"], "paused": control["paused"],
                         "reconciliation_expectation": "all selected rows must link to one allocation event and matching inventory transactions"})
        if connection.total_changes != before_changes:
            raise RuntimeError("Dry-run unexpectedly changed the database connection")
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "database": str(path),
                "cutoff": cutoff, "hypothetical_only": True, "row_count": len(rows), "rows": rows}
    finally:
        connection.close()
