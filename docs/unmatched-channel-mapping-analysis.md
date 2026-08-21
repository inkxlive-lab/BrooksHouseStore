# Unmatched channel mapping analysis

`analyze_unmatched_channel_lines.py` opens the database read-only and groups
unmatched Shopify, Walmart, and Amazon order lines by their reusable marketplace
identity. It inspects existing product barcodes, products, master catalog,
channel listings, channel mapping rules, Amazon listings/links, and Walmart
listings/links. It never writes a mapping.

Classification is deliberately conservative:

- `A_EXACT`: unique exact UPC/EAN/GTIN/barcode evidence with no conflicting
  cross-reference.
- `B_STRONG`: one unique existing channel rule, linked marketplace listing, or
  listing-to-barcode cross-reference. Human approval is still required.
- `C_CANDIDATE`: one plausible normalized-title candidate scoring at least 0.72
  and clearly leading other plausible results. Human approval is required.
- `D_NO_MATCH`: no exact/cross-reference evidence and no title result reaches
  the candidate threshold.
- `E_AMBIGUOUS`: conflicting identifiers or multiple plausible candidates.

Separate projections model approval of `A_EXACT` only and `A_EXACT` plus
`B_STRONG`. Candidate and ambiguous results remain manual work. No mappings are
installed; each affected line is evaluated with the existing Storefront → Store
Back Room → owed/replenishment policy in memory only.

Example:

```powershell
python analyze_unmatched_channel_lines.py --days 30 `
  --csv reports/unmatched-mapping.csv `
  --json reports/unmatched-mapping.json `
  --markdown reports/unmatched-mapping.md
```

Use `--cutoff <ISO timestamp>` to reproduce a previously reported rolling
cohort exactly; otherwise `--days` is measured from the current run time.

CSV/JSON include alternatives and per-group projected outcomes. The Markdown
file is the human approval queue. No apply option exists.
