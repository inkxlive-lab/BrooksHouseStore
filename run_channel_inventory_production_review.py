#!/usr/bin/env python
import argparse
import json
from pathlib import Path

from app.database_resolution import configured_sqlite_path, require_application_database_match
from app.services.channel_inventory_production_review import write_production_review


def main() -> int:
    parser = argparse.ArgumentParser(description="Strictly read-only production-data inventory dry-run review")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--allow-fixture-database", action="store_true")
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database or configured_sqlite_path()
    if not args.allow_fixture_database:
        database = require_application_database_match(database)
    report = write_production_review(database,args.cutoff,args.output,
                                     require_application_match=not args.allow_fixture_database)
    print(json.dumps({key: report[key] for key in ("order_line_count","would_deduct_quantity","owed_quantity",
                                                    "unsafe_review_quantity","zero_mutation_verified")},indent=2))
    return 0 if report["zero_mutation_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
