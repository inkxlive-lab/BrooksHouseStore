#!/usr/bin/env python
"""Generate a read-only Shopify/Walmart/Amazon inventory reconciliation."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.channel_inventory_reconciliation import connect_read_only, reconcile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="app/data/brookshouse_store.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--csv")
    parser.add_argument("--json")
    args = parser.parse_args()
    if not 1 <= args.days <= 180:
        parser.error("--days must be between 1 and 180")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    with connect_read_only(args.database) as connection:
        rows = [row.as_dict() for row in reconcile(connection, cutoff)]
    if args.csv:
        target = Path(args.csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    summary = {"lines": len(rows), "deduct_preview": sum(r["action"] == "deduct_preview" for r in rows), "review": sum(r["action"] == "review" for r in rows)}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
