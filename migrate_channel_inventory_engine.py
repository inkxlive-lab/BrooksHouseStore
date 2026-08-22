#!/usr/bin/env python
"""Preview the production migration or apply it only to an explicit DB copy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.migrations.channel_inventory_engine_schema import apply_guarded_migration, apply_to_copy, prepare_guarded_migration, preview


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply-to-copy", action="store_true")
    parser.add_argument("--prepare-guarded", action="store_true")
    parser.add_argument("--apply-guarded", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--cutover-at")
    parser.add_argument("--shopify-checkpoint")
    parser.add_argument("--amazon-checkpoint")
    parser.add_argument("--walmart-checkpoint")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.prepare_guarded:
        if not args.backup or not args.manifest:
            parser.error("--backup and --manifest are required")
        result = prepare_guarded_migration(args.database,args.backup,cutover_at=args.cutover_at or "",
            checkpoints={"shopify":args.shopify_checkpoint or "","amazon":args.amazon_checkpoint or "","walmart":args.walmart_checkpoint or ""})
        args.manifest.write_text(json.dumps(result,indent=2),encoding="utf-8")
        print(json.dumps(result,indent=2))
    elif args.apply_guarded:
        if not args.manifest:
            parser.error("--manifest is required")
        result = apply_guarded_migration(args.database,json.loads(args.manifest.read_text(encoding="utf-8")),confirmation=args.confirm)
        print(json.dumps(result.as_dict(), indent=2))
    else:
        result = apply_to_copy(args.database) if args.apply_to_copy else preview(args.database)
        print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
