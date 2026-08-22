# Channel inventory deployment preflight

Status: design and read-only/copy-only tooling only. No production schema, engine,
inventory, reservation, schedule, sync, or deployment change has been made.

## Recommended integration boundary

Keep channel ingestion and inventory processing separate. Shopify sync, Walmart
`sync_orders(30)`, and Amazon history sync remain authoritative importers. After a
successful importer commit, a separately scheduled reconciler may discover candidates
newer than its persisted per-channel cutover/checkpoint. It must acquire a single-run
lock, re-read the source line inside its write transaction, and commit the allocation,
idempotency event, inventory transaction, and replenishment work item atomically.
Sync failure must not invoke inventory processing; inventory failure must not roll back
or disable channel ingestion.

Do not install the engine from application startup. Use the explicit migration only
during an approved maintenance window. The admin page is read-only and handles the
absent schema as a normal `not installed` state.

## Eligibility and stable identities

All channels require: a valid, unambiguous product mapping; positive line quantity;
a source business timestamp and import checkpoint at/after the channel cutover; no
legacy overlap; and no existing `(channel, order_id, line_id, event_type)` ledger event.

| Channel | Stable identity | New-sale eligible state | Change detection | Review/exclusion |
|---|---|---|---|---|
| Shopify | Shopify order GID + line-item GID | non-test, not cancelled, paid/authorized and unfulfilled | order/line `updated_at`, `current_quantity`, financial/fulfillment state | `inventory_applied`, cancelled, refunded, returned, fulfilled/partially fulfilled, zero quantity |
| Walmart | purchase order ID + persistent order-line ID (retain line number for legacy correlation) | `Created` or `Acknowledged` | imported status/quantity plus `synced_at`; hash the normalized source payload when available | legacy `walmart_order_inventory_sync.quantity_added`, shipped/delivered/cancelled, unknown/multiple terminal statuses |
| Amazon | Amazon order ID + order item ID | merchant-fulfilled `UNSHIPPED` | `last_updated_time`, item quantity and `synced_at`; hash normalized state | legacy `amazon_order_inventory_sync.quantity_added`, FBA, partially shipped, shipped/cancelled/return/refund |

Partially fulfilled/shipped lines start in review because the current imported schemas
do not consistently expose a trustworthy remaining fulfillable quantity. A refund is a
lifecycle notice only; inventory is restored only after an explicit physical restock.

## Cutover and legacy overlap

Deploy disabled. Run the final legacy channel sync/reservation reconciliation, then
record for each channel both a UTC cutover time and a source checkpoint from the last
successful importer run. Only orders whose business timestamp **and** first observed
checkpoint are at/after that boundary are candidates. Never backfill earlier orders.

Before enabling a channel, reconcile and freeze these legacy signals:

- Shopify `shopify_sales_lines.inventory_applied`.
- Walmart `walmart_order_inventory_sync` by PO + line number.
- Amazon `amazon_order_inventory_sync` by order + item.

Any overlap remains review-only. Do not translate existing reserved quantities into
new ledger events automatically. Retire the legacy reservation scripts only after a
separate approval and a zero-overlap observation period.

## Location policy

The complete line must fit in one eligible physical location: Storefront first, then
Store Back Room. Otherwise create an owed allocation using reservation semantics; do
not fabricate `quantity_on_hand` in Online Orders / Reserved. Warehouse, trailers,
containers, mobile storage, damaged/returns, and other reserve/review locations are
never automatic deduction sources. Eligible reserve stock is evidence for a directed
replenishment work item only.

## Controls and audit

Controls are global plus per-channel, with modes `disabled`, `dry_run`, and `enabled`,
a pause flag, reason, actor, timestamp, cutover, and source checkpoint. Effective mode
is the most restrictive applicable setting. Missing controls mean disabled. Every run
records mode, checkpoint, counts, outcome, and error. Changes to controls require a
separately approved owner-only mutation endpoint/CLI and their own audit event; none is
implemented or enabled in this phase.

Emergency pause blocks acquisition of new candidates but does not alter existing
allocations. Dry-run performs source reads and reporting only. Resume requires a fresh
source sync and reconciliation; it does not replay pre-cutover history.

## Migration and recovery

`migrate_channel_inventory_engine.py` previews against a read-only connection. Apply
requires `--apply-to-copy` and refuses the protected production path. The migration
validates source tables/columns and integrity, creates six tables and supporting indexes
idempotently, and performs no backfill or control initialization.

For a future approved production migration: record SHA-256 and inventory fingerprint,
make and verify a SQLite online backup, pause writers, preview, apply once, run integrity
and inventory comparisons, and leave controls disabled. On failure, keep the engine
disabled. If no engine rows exist, a separately reviewed rollback can remove only the
new objects; otherwise restore the verified backup while writers are stopped. Never
attempt an in-place partial ledger rollback.
