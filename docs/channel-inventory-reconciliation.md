# Cross-channel inventory reconciliation (dry run)

`reconcile_channel_inventory.py` reads the existing Shopify, Amazon, Walmart,
mapping, pick-slot, inventory, and legacy applied-state tables through a SQLite
read-only connection. It has no apply flag and creates no database objects.

Run a bounded preview:

```powershell
python reconcile_channel_inventory.py --days 30 --csv reports/channel-inventory.csv --json reports/channel-inventory.json
```

## Proposed location priority

1. `Online Orders / Reserved`, when that product has enough quantity there.
2. The product's exact `product_pick_slots` location/container, if it is not a
   trailer, mobile, hold, or catalog location.
3. An active `store` location row with enough unreserved quantity.

One location must cover the full line. The preview does not split a line. It
never falls through to back room, warehouse, storage container, trailer,
mobile inventory, hold, damaged/returns, or catalog inventory merely because
stock exists there.

## Review and lifecycle rules

Unmatched/ambiguous lines, insufficient stock, zero quantities, duplicate/applied
lines, Amazon-fulfilled orders, cancellations, refunds, returns, and restock
signals are review items. A refund/return never proposes a positive inventory
change; physical restock confirmation must be a separate future event.

## Future idempotent apply design (not enabled)

The module exposes reviewed `channel_inventory_ledger` DDL for a later approved
migration. The unique key is `(channel_name, order_id, order_line_id, event_type)`.
A future apply path must use `BEGIN IMMEDIATE`, re-check that unique key, re-check
the source line and chosen inventory row, insert the negative inventory
transaction, update inventory, and insert the ledger record in one transaction.
Any conflict rolls back the entire operation. Returns/restocks require separate
event types and explicit physical-restock approval; they must not reuse `sale`.

This phase intentionally performs no historical application, production write,
scheduled-task change, or deployment.
