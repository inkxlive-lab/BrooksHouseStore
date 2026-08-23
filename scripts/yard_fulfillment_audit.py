"""Read-only inventory invariants for the fulfillment-yard order refresh."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def main() -> int:
    database = Path(sys.argv[1] if len(sys.argv) > 1 else "app/data/brookshouse_store.db").resolve()
    backup_path = None
    if len(sys.argv) > 2 and sys.argv[2] == "--backup":
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%z")
        backup_dir = (database.parents[2] / "backups" / "fulfillment-yard").resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"brookshouse_store_pre_order_refresh_{stamp}.db"
        source = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(backup_path)
        source.backup(target)
        target.close()
        source.close()
        check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        integrity_rows = [row[0] for row in check.execute("PRAGMA integrity_check")]
        check.close()
        if integrity_rows != ["ok"]:
            raise RuntimeError(f"Backup integrity check failed: {integrity_rows}")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "inventory" not in tables:
        raise RuntimeError("inventory table not found")

    inventory_columns = [row[1] for row in connection.execute("PRAGMA table_info(inventory)")]
    order_column = "inventory_id" if "inventory_id" in inventory_columns else inventory_columns[0]
    rows = connection.execute(
        f"SELECT * FROM inventory ORDER BY {quoted(order_column)}"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        )
        digest.update(b"\n")

    result = {
        "database": str(database),
        "inventory_row_count": len(rows),
        "total_on_hand": sum(int(row["quantity_on_hand"] or 0) for row in rows),
        "total_reserved": sum(int(row["quantity_reserved"] or 0) for row in rows),
        "inventory_fingerprint_sha256": digest.hexdigest(),
        "backup_path": str(backup_path) if backup_path else None,
        "backup_integrity_check": "ok" if backup_path else None,
    }

    transaction_table = next(
        (name for name in ("inventory_transactions", "inventory_transaction") if name in tables),
        None,
    )
    if transaction_table:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted(transaction_table)})")]
        id_column = next(
            (name for name in ("transaction_id", "inventory_transaction_id", "id") if name in columns),
            columns[0],
        )
        count, high_water = connection.execute(
            f"SELECT COUNT(*), MAX({quoted(id_column)}) FROM {quoted(transaction_table)}"
        ).fetchone()
        result.update(
            {
                "inventory_transaction_table": transaction_table,
                "inventory_transaction_count": int(count),
                "inventory_transaction_high_water": high_water,
            }
        )
    else:
        result.update(
            {
                "inventory_transaction_table": None,
                "inventory_transaction_count": 0,
                "inventory_transaction_high_water": None,
            }
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
