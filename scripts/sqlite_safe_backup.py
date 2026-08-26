"""Create and verify a transactionally consistent SQLite backup.

This utility never imports the BrooksHouse application. The source database is
opened read-only and SQLite's online backup API writes only to the destination.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    destination = args.destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"Backup integrity check failed: {result}")

    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"destination={destination}")
    print(f"sha256={digest}")
    print("integrity_check=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
