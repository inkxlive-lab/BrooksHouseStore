from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path



def fingerprint(connection: sqlite3.Connection) -> dict:
    result = {}
    for table in ("inventory", "inventory_transactions", "channel_inventory_ledger", "channel_inventory_allocations"):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            result[table] = {"count": 0, "sha256": None}
            continue
        rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        encoded = json.dumps([list(row) for row in rows], default=str, separators=(",", ":")).encode()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only SQLite backup integrity and inventory audit")
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        counts = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        inventory = fingerprint(connection)
    finally:
        connection.close()
    sha256 = hashlib.sha256(database.read_bytes()).hexdigest()
    result = {
        "database": str(database), "size_bytes": database.stat().st_size,
        "sha256": sha256, "integrity_check": integrity,
        "table_counts": counts, "inventory_fingerprints": inventory,
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
