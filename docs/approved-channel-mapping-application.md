# Approved channel mapping application

`apply_approved_channel_mappings.py` consumes the reviewed mapping-analysis JSON
and accepts only its 42 `B_STRONG` identities (59 lines / 92 units). Preview is
the default. Apply mode requires both `--confirm-approved-strong 42` and a new,
nonexistent `--backup` target.

The utility uses the existing BrooksHouse mapping structures:

- all channels: `channel_sales_product_rules`, mapped order-line `product_id`,
  and `channel_match_audit`;
- Walmart: `walmart_listings` and `walmart_product_links`;
- Amazon: `amazon_listings` and `amazon_product_links`;
- Shopify: existing mapping rules plus `shopify_sales_lines` match metadata.

Preflight rejects stale candidates, missing/inactive products, changed reviewed
barcodes, missing reviewed order lines, contradictory rules/links, and order
lines already mapped to another product. Conflicts are skipped rather than
overwritten.

Apply mode creates a consistent SQLite backup through the backup API, verifies
its integrity, uses savepoints per identity, and writes an audit entry for each
successful identity. It fingerprints all inventory rows plus inventory-
transaction count/max ID before and after, and fails verification if they differ.

The application report is the operational reversal manifest; combined with the
verified pre-apply database backup and `channel_match_audit`, it records source
keys, products, exact affected order lines, planned operations, and outcomes.
The utility never updates inventory or reservations and creates no schema.
