# Unmatched channel mapping analysis

`analyze_unmatched_channel_lines.py` opens the database read-only and groups
unmatched Shopify, Walmart, and Amazon order lines by their reusable marketplace
identity. It inspects existing product barcodes, products, master catalog,
channel listings, channel mapping rules, Amazon listings/links, and Walmart
listings/links. It never writes a mapping.

Ranking is deliberately conservative:

1. A unique exact UPC/EAN/GTIN/barcode in `product_barcodes`.
2. A unique existing channel rule, linked marketplace listing, or listing-to-
   barcode cross-reference.
3. A unique normalized-title candidate scoring at least 0.88 with at least a
   0.08 lead over the next candidate. Human approval is always required.
4. No viable unique candidate, including conflicting exact evidence.

The projected reconciliation assumes a reviewer approves ranks 1–3. It does not
install those mappings; each affected line is evaluated with the existing
Storefront → Store Back Room → owed/replenishment policy in memory only.

Example:

```powershell
python analyze_unmatched_channel_lines.py --days 30 `
  --csv reports/unmatched-mapping.csv `
  --json reports/unmatched-mapping.json `
  --markdown reports/unmatched-mapping.md
```

CSV/JSON include alternatives and per-group projected outcomes. The Markdown
file is the human approval queue. No apply option exists.
