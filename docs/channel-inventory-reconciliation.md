# Cross-channel inventory reconciliation (dry run)

`reconcile_channel_inventory.py` reads the existing Shopify, Amazon, Walmart,
mapping, pick-slot, inventory, and legacy applied-state tables through a SQLite
read-only connection. It has no apply flag and creates no database objects.

Run a bounded preview:

```powershell
python reconcile_channel_inventory.py --days 30 --csv reports/channel-inventory.csv --json reports/channel-inventory.json
```

## Proposed location priority

1. `BrooksHouse Storefront`.
2. `Store Back Room`.
3. Stop and review while retaining the order as committed/owed.

One eligible physical location must cover the full line. Inventory rows within
that location are aggregated, but the preview never combines Storefront and
Back Room to satisfy one line. `Online Orders / Reserved` is allocated/staged
inventory and is not a general source for a new sale.

The workflow never automatically deducts Warehouse, Trailer, Trailer 1/2/3,
Storage Container, On-the-Road Trailer, Damaged / Returns, catalog, hold, or
review inventory. Available warehouse/trailer/container/mobile/storage stock is
reported separately as a potential replenishment source and produces a proposed
replenishment work item; it remains untouched.

## Review and lifecycle rules

Unmatched/ambiguous lines, insufficient stock, zero quantities, duplicate/applied
lines, Amazon-fulfilled orders, cancellations, refunds, returns, and restock
signals are review items. A refund/return never proposes a positive inventory
change; physical restock confirmation must be a separate future event.

## Commitment, staging, and owed inventory

Every uniquely matched active sale needs a line-level commitment even when no
eligible physical source can fulfill it. The proposed
`channel_inventory_allocations` record stores `quantity_committed`,
`quantity_staged`, and `quantity_unlocated`, keyed by channel/order/line/event.
This represents owed units without inventing physical stock.

`inventory.quantity_on_hand` in `Online Orders / Reserved` means merchandise is
physically present there. `inventory.quantity_reserved` may reserve those staged
physical units once the allocation points to that inventory row. Unlocated units
remain only in the allocation record and a work item.

- Found and staged: transfer the physical units with normal audited inventory
  transactions, attach the allocation to the staged row, and move units from
  `quantity_unlocated` to `quantity_staged`; fulfillment later consumes the
  staged units once.
- Cancelled before fulfillment: release physical `quantity_reserved`, close the
  commitment with a distinct cancellation event, and cancel its open work item.
- Quantity changed: atomically adjust the same commitment to the latest source
  quantity; release excess reservations or create an incremental owed amount.
- Returned/restocked: a refund alone changes nothing. After physical inspection,
  use a separate `restock` event and audited receiving transaction into the
  confirmed location.
- Stock never found: keep the allocation visible as unlocated until a human
  records cancellation, substitution, external procurement, or an approved
  shortage/write-off resolution. Never fabricate or silently clear inventory.

Existing physical quantity in `Online Orders / Reserved` without line-level
allocation ownership is reported as a staged-pool review. It is neither offered
to a new order nor counted as genuinely unavailable company-wide.

## Future idempotent apply design (not enabled)

The module exposes reviewed ledger/allocation DDL for a later approved migration.
Both use `(channel_name, order_id, order_line_id, event_type)` uniqueness.
A future apply path must use `BEGIN IMMEDIATE`, re-check that unique key, re-check
the source line and chosen inventory row, insert the negative inventory
transaction, update inventory, and insert the ledger record in one transaction.
Any conflict rolls back the entire operation. Returns/restocks require separate
event types and explicit physical-restock approval; they must not reuse `sale`.

This phase intentionally performs no historical application, production write,
scheduled-task change, or deployment.
