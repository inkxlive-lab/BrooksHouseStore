#!/usr/bin/env python
"""Write an exact post-apply channel inventory reconciliation for a DB copy."""

import argparse
import json
from pathlib import Path

from app.services.channel_inventory_audit import write_reconciliation_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = write_reconciliation_report(args.database, args.output)
    print(json.dumps({key: report[key] for key in ("database", "row_count", "mismatch_count")}, indent=2))
    return 1 if report["mismatch_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
