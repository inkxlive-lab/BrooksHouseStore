"""Post-apply reconciliation for the copy-only channel inventory engine."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.services.channel_inventory_engine import connect_copy, load_source_line
from app.services.channel_inventory_mapping import validate_mapping


@dataclass(frozen=True)
class AuditRow:
    marketplace: str
    order_id: str
    order_line_id: str
    marketplace_quantity: int | None
    product_id: int
    allocated_quantity: int
    deducted_quantity: int
    restored_quantity: int
    current_outstanding_allocation: int
    physical_inventory_rows_used: tuple[int, ...]
    inventory_transaction_ids: tuple[int, ...]
    mismatch_error_state: tuple[str, ...]

    def as_dict(self) -> dict:
        value = asdict(self)
        value["physical_inventory_rows_used"] = list(self.physical_inventory_rows_used)
        value["inventory_transaction_ids"] = list(self.inventory_transaction_ids)
        value["mismatch_error_state"] = list(self.mismatch_error_state)
        return value


def reconcile_copy(database: str | Path) -> dict:
    connection = connect_copy(database)
    try:
        rows = []
        for allocation in connection.execute("SELECT * FROM channel_inventory_allocations ORDER BY allocation_id"):
            errors = []
            try:
                source = load_source_line(connection, allocation["channel_name"], allocation["order_id"], allocation["order_line_id"])
                marketplace_quantity = source.quantity
                if source.product_id is not None and int(source.product_id) != int(allocation["product_id"]):
                    errors.append("source_product_differs_from_allocation")
                mapping = validate_mapping(connection, source.channel, source.product_id, source.sku, source.asin, source.mapping_status)
                if not mapping.safe:
                    errors.append(f"unsafe_mapping:{mapping.status}")
            except (ValueError, KeyError):
                marketplace_quantity = None
                errors.append("source_line_missing")
            ownership = connection.execute(
                """SELECT inventory_id,deducted_quantity,staged_quantity,reserved_quantity
                     FROM channel_inventory_allocation_inventory WHERE allocation_id=?""",
                (allocation["allocation_id"],),
            ).fetchall()
            owned_deducted = sum(int(row["deducted_quantity"]) for row in ownership)
            owned_staged = sum(int(row["staged_quantity"]) for row in ownership)
            if owned_deducted != int(allocation["deducted_quantity"]):
                errors.append("allocation_deducted_differs_from_physical_ownership")
            if owned_staged != int(allocation["staged_quantity"]):
                errors.append("allocation_staged_differs_from_physical_ownership")
            events = connection.execute(
                "SELECT event_id,event_type,quantity_change,created_at FROM channel_inventory_ledger WHERE allocation_id=? ORDER BY event_id",
                (allocation["allocation_id"],),
            ).fetchall()
            event_ids = [int(row["event_id"]) for row in events]
            links = []
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                links = connection.execute(
                    f"""SELECT cet.event_id,cet.inventory_transaction_id,cet.inventory_id,cet.quantity_change,
                                it.transaction_id,it.product_id,it.quantity_change transaction_quantity,
                                inv.product_id inventory_product_id
                           FROM channel_inventory_event_transactions cet
                           LEFT JOIN inventory_transactions it ON it.transaction_id=cet.inventory_transaction_id
                           LEFT JOIN inventory inv ON inv.inventory_id=cet.inventory_id
                          WHERE cet.event_id IN ({placeholders}) ORDER BY cet.inventory_transaction_id""",
                    event_ids,
                ).fetchall()
            by_event = {event_id: 0 for event_id in event_ids}
            for link in links:
                by_event[int(link["event_id"])] += int(link["quantity_change"])
                if link["transaction_id"] is None:
                    errors.append("linked_inventory_transaction_missing")
                elif int(link["transaction_quantity"]) != int(link["quantity_change"]):
                    errors.append("linked_transaction_quantity_mismatch")
                if link["product_id"] is not None and int(link["product_id"]) != int(allocation["product_id"]):
                    errors.append("linked_transaction_product_mismatch")
                if link["inventory_product_id"] is None or int(link["inventory_product_id"]) != int(allocation["product_id"]):
                    errors.append("physical_inventory_mismatch")
            for event in events:
                if int(event["quantity_change"]) != by_event[int(event["event_id"])]:
                    errors.append(f"ledger_transaction_net_mismatch:event:{event['event_id']}")
            event_types = [str(event["event_type"]) for event in events]
            if not event_types or event_types[0] != "sale_commitment":
                errors.append("invalid_lifecycle_sequence:missing_initial_sale")
            if len(event_types) != len(set(event_types)):
                errors.append("duplicate_events")
            deducted = -sum(min(int(link["quantity_change"]), 0) for link in links)
            restored = sum(max(int(link["quantity_change"]), 0) for link in links)
            if restored > deducted:
                errors.append("over_restock")
            physical_rows = sorted({int(row["inventory_id"]) for row in ownership} |
                                   {int(link["inventory_id"]) for link in links})
            transaction_ids = tuple(int(link["inventory_transaction_id"]) for link in links)
            outstanding = (int(allocation["deducted_quantity"]) + int(allocation["staged_quantity"]) +
                           int(allocation["unlocated_quantity"]))
            if int(allocation["unlocated_quantity"]) > 0:
                errors.append("owed_or_short_inventory")
            if outstanding > int(allocation["ordered_quantity"]):
                errors.append("allocation_ledger_disagreement")
            control_exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_inventory_engine_control'").fetchone()
            if control_exists:
                control = connection.execute("SELECT cutover_at,source_checkpoint FROM channel_inventory_engine_control WHERE scope=?",
                                             (allocation["channel_name"],)).fetchone()
                threshold = str((control[1] or control[0]) if control else "")
                if threshold and any(str(event["created_at"]) < threshold for event in events):
                    errors.append("event_older_than_cutover_or_checkpoint")
            rows.append(AuditRow(
                marketplace=str(allocation["channel_name"]), order_id=str(allocation["order_id"]),
                order_line_id=str(allocation["order_line_id"]), marketplace_quantity=marketplace_quantity,
                product_id=int(allocation["product_id"]), allocated_quantity=int(allocation["ordered_quantity"]),
                deducted_quantity=deducted, restored_quantity=restored,
                current_outstanding_allocation=outstanding,
                physical_inventory_rows_used=tuple(physical_rows), inventory_transaction_ids=transaction_ids,
                mismatch_error_state=tuple(sorted(set(errors))),
            ))
        return {"database": str(Path(database).resolve()), "row_count": len(rows),
                "mismatch_count": sum(bool(row.mismatch_error_state) for row in rows),
                "rows": [row.as_dict() for row in rows]}
    finally:
        connection.close()


def write_reconciliation_report(database: str | Path, output: str | Path) -> dict:
    report = reconcile_copy(database)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
