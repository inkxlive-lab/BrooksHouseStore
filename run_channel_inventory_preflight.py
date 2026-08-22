#!/usr/bin/env python
"""Generate a read-only hypothetical channel inventory operator preview."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.channel_inventory_engine import PRODUCTION_DB
from app.services.channel_inventory_preflight import build_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=PRODUCTION_DB)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(args.hours, 1))).isoformat()
    report = build_report(args.database, cutoff=cutoff)
    summary = {key: report[key] for key in ("generated_at", "cutoff", "hypothetical_only",
                                             "engine", "total_order_lines", "counts", "units")}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
