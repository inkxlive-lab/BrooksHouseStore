# Cross-channel inventory engine — copied database only

The engine in `app/services/channel_inventory_engine.py` is not installed by
application startup. Every mutating entry point resolves its database path and
refuses the known production database and every path in production's `app/data`
directory. `run_channel_inventory_engine_copy.py` requires an explicit database
and cutoff; schema initialization is an explicit copy-only option.

## Minimal copy schema

- `channel_inventory_ledger`: immutable marketplace events, uniquely keyed by
  channel, order, line, and event type.
- `channel_inventory_allocations`: one mutable order obligation distinguishing
  ordered, deducted, staged, unlocated, and restored quantities.
- `channel_inventory_allocation_inventory`: exact physical inventory ownership
  for deductions, staging, reservations, and reversals.
- `channel_inventory_event_transactions`: links ledger events to the ordinary
  BrooksHouse `inventory_transactions` they created.

The schema is created only on a copied database. It is not a production
migration.

## Initial sale transaction

Under `BEGIN IMMEDIATE`, the engine reloads the marketplace line, verifies its
product mapping and source version, checks the unique ledger key, then evaluates
Storefront followed by Store Back Room. A line may use multiple inventory rows
inside one eligible location, but never both locations. Successful deductions,
ordinary inventory transactions, allocation ownership, and the ledger event
commit atomically.

When neither eligible location can cover the line, the same atomic event records
the full owed allocation without changing physical inventory. Reserve/deep-
storage rows become evidence in an existing `operations_work_queue` directed-
replenishment task; no inventory is moved. If no reserve candidate exists, an
inventory-investigation task keeps the obligation visible.

## Lifecycle

- Duplicate synchronization returns `already_applied` through both a precheck
  and the database unique constraint.
- Quantity decrease restores only inventory previously owned by that allocation.
  Quantity increase becomes unlocated owed quantity unless already deducted;
  it never silently takes from another location.
- Cancellation restores an allocation's exact deducted inventory once, releases
  its owned `quantity_reserved`, clears owed quantities, and cancels open work.
- Refund notice creates a review event with zero inventory change.
- Confirmed return/restock requires a physical destination, positive quantity,
  and unique external reference; it creates a distinct inventory transaction.
- Confirmed staging requires physical on-hand already in `Online Orders /
  Reserved`, reserves it with `quantity_reserved`, and ties it to one allocation.
  It never creates on-hand quantity and is idempotent by staging reference.
- Any mapping change between preview and apply raises `StalePreview` and rolls
  back the entire event.

The copy runner can simulate a cohort, but there is intentionally no production
apply mode, startup hook, scheduler integration, or deployment configuration.
