#!/usr/bin/env python
import argparse
import json
from pathlib import Path

from app.database_resolution import configured_sqlite_path, require_application_database_match
from app.services.channel_inventory_dry_run import build_dry_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only channel inventory dry-run")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--allow-fixture-database", action="store_true")
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database or configured_sqlite_path()
    if not args.allow_fixture_database:
        database = require_application_database_match(database)
    report = build_dry_run(database, args.cutoff)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"row_count": report["row_count"], "hypothetical_only": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
