#!/usr/bin/env python
"""Generate grouped, read-only mapping proposals for unmatched channel lines."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.channel_inventory_reconciliation import connect_read_only
from app.services.channel_mapping_analysis import analyze_unmatched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="app/data/brookshouse_store.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--csv")
    parser.add_argument("--json")
    parser.add_argument("--markdown")
    args = parser.parse_args()
    if not 1 <= args.days <= 180:
        parser.error("--days must be between 1 and 180")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    with connect_read_only(args.database) as connection:
        proposals, summary = analyze_unmatched(connection, cutoff)
    rows = [proposal.as_dict() for proposal in proposals]
    if args.csv and rows:
        target = Path(args.csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"summary": summary, "proposals": rows}, indent=2), encoding="utf-8")
    if args.markdown:
        target = Path(args.markdown)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Unmatched channel mapping review", "",
            f"- Unmatched order lines: {summary['unmatched_lines']}",
            f"- Grouped marketplace identities: {summary['group_count']}",
            f"- Lines resolvable if rank 1–3 proposals are approved: {summary['resolvable_lines_if_rank_1_to_3_approved']}",
            f"- Units resolved: {summary['resolvable_units_if_rank_1_to_3_approved']}", "",
        ]
        for channel in ("shopify", "walmart", "amazon"):
            lines.extend([f"## {channel.title()}", "", "| Rank | Marketplace item | Candidate | Evidence | Lines | Units |", "|---:|---|---|---|---:|---:|"])
            for row in (item for item in rows if item["channel"] == channel):
                identifiers = json.loads(row["marketplace_identifiers"])
                identity = "; ".join(f"{key}={value}" for key, value in identifiers.items() if value) or row["source_key"]
                candidate = f"{row['candidate_product_id']} — {row['candidate_product_name']}" if row["candidate_product_id"] else "No viable candidate"
                safe_title = row["marketplace_title"].replace("|", "\\|")
                safe_evidence = row["evidence"].replace("|", "\\|")
                safe_identity = identity.replace("|", "\\|")
                safe_candidate = candidate.replace("|", "\\|")
                lines.append(f"| {row['confidence_rank']} | {safe_title}<br>{safe_identity} | {safe_candidate} | {safe_evidence} | {row['order_line_count']} | {row['unit_count']} |")
            lines.append("")
        lines.extend(["## Projected reconciliation if rank 1–3 proposals are approved", ""])
        for category, count in summary["projected_reconciliation_categories"].items():
            lines.append(f"- {category}: {count}")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
