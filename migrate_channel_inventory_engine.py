#!/usr/bin/env python
"""Preview the production migration or apply it only to an explicit DB copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.migrations.channel_inventory_engine_schema import apply_to_copy, preview


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply-to-copy", action="store_true")
    args = parser.parse_args()
    result = apply_to_copy(args.database) if args.apply_to_copy else preview(args.database)
    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
