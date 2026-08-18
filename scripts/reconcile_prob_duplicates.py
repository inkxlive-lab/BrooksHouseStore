#!/usr/bin/env python3
"""Preview or clear duplicate PROB inventory already scanned into trusted locations."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


PROB_LOCATION_ID = 12
STOREFRONT_LOCATION_ID = 1
BACK_ROOM_LOCATION_ID = 2
TRUSTED_LOCATION_IDS = (STOREFRONT_LOCATION_ID, BACK_ROOM_LOCATION_ID)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def reference_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def database_from_url(value: str) -> Path | None:
    if not value.lower().startswith("sqlite"):
        return None
    parsed = urlparse(value)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"/{parsed.netloc}{path}"
    if not path:
        return None
    if value.startswith("sqlite:////"):
        return Path(path)
    return Path(path.lstrip("/"))


def looks_like_live_database(path: Path) -> bool:
    if not path.is_file():
        return False
    lowered = str(path).lower()
    if "backup" in lowered or "snapshot" in lowered:
        return False
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        connection.close()
    except sqlite3.Error:
        return False
    return {"products", "inventory", "inventory_transactions"}.issubset(tables)


def resolve_database(explicit_path: str | None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))

    for variable in (
        "BROOKSHOUSE_DATABASE_PATH",
        "DATABASE_PATH",
        "DB_PATH",
        "SQLITE_DB_PATH",
    ):
        value = os.getenv(variable)
        if value:
            candidates.append(Path(value))

    database_url = os.getenv("DATABASE_URL", "")
    url_path = database_from_url(database_url)
    if url_path is not None:
        candidates.append(url_path)

    try:
        from app.database.connection import engine

        engine_database = getattr(engine.url, "database", None)
        if engine_database:
            candidates.append(Path(str(engine_database)))
    except Exception:
        pass

    candidates.extend(
        [
            Path("/data/brookshouse_store.db"),
            Path("/app/data/brookshouse_store.db"),
            Path("/app/app/data/brookshouse_store.db"),
            Path("app/data/brookshouse_store.db"),
            Path(r"C:\BrooksHouseStore\app\data\brookshouse_store.db"),
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if looks_like_live_database(resolved):
            return resolved

    raise RuntimeError(
        "Could not locate the live BrooksHouse SQLite database. "
        "Pass its path with --database."
    )


def verify_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "inventory": {
            "inventory_id",
            "product_id",
            "location_id",
            "container_id",
            "quantity_on_hand",
            "quantity_reserved",
            "updated_at",
        },
        "inventory_transactions": {
            "product_id",
            "location_id",
            "transaction_type",
            "quantity_change",
            "unit_cost",
            "reference_number",
            "notes",
            "created_at",
            "container_id",
            "performed_by_name",
            "performed_by_role",
        },
        "products": {"product_id", "product_name", "average_cost"},
    }
    for table, required_columns in expected.items():
        actual = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        missing = required_columns - actual
        if missing:
            raise RuntimeError(
                f"{table} is missing required columns: {sorted(missing)}"
            )


TARGET_QUERY = """
SELECT
    prob.inventory_id,
    prob.product_id,
    product.product_name,
    COALESCE(prob.container_id, '') AS prob_container_id,
    COALESCE(prob.quantity_on_hand, 0) AS prob_quantity,
    COALESCE(prob.quantity_reserved, 0) AS prob_reserved,
    COALESCE(product.average_cost, 0) AS average_cost,
    COALESCE((
        SELECT SUM(storefront.quantity_on_hand)
        FROM inventory AS storefront
        WHERE storefront.product_id = prob.product_id
          AND storefront.location_id = ?
          AND COALESCE(storefront.quantity_on_hand, 0) > 0
    ), 0) AS storefront_quantity,
    COALESCE((
        SELECT SUM(back_room.quantity_on_hand)
        FROM inventory AS back_room
        WHERE back_room.product_id = prob.product_id
          AND back_room.location_id = ?
          AND COALESCE(back_room.quantity_on_hand, 0) > 0
    ), 0) AS back_room_quantity
FROM inventory AS prob
JOIN products AS product
  ON product.product_id = prob.product_id
WHERE prob.location_id = ?
  AND COALESCE(prob.quantity_on_hand, 0) > 0
  AND EXISTS (
      SELECT 1
      FROM inventory AS trusted
      WHERE trusted.product_id = prob.product_id
        AND trusted.location_id IN (?, ?)
        AND COALESCE(trusted.quantity_on_hand, 0) > 0
  )
ORDER BY product.product_name COLLATE NOCASE, prob.inventory_id
"""


def load_targets(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        TARGET_QUERY,
        (
            STOREFRONT_LOCATION_ID,
            BACK_ROOM_LOCATION_ID,
            PROB_LOCATION_ID,
            STOREFRONT_LOCATION_ID,
            BACK_ROOM_LOCATION_ID,
        ),
    ).fetchall()


def print_preview(rows: list[sqlite3.Row], limit: int) -> None:
    unique_products = {row["product_id"] for row in rows}
    prob_units = sum(row["prob_quantity"] for row in rows)
    reserved_units = sum(row["prob_reserved"] for row in rows)
    trusted_by_product = {}
    for row in rows:
        trusted_by_product[row["product_id"]] = (
            row["storefront_quantity"],
            row["back_room_quantity"],
        )
    storefront_units = sum(values[0] for values in trusted_by_product.values())
    back_room_units = sum(values[1] for values in trusted_by_product.values())

    print("\nBROOKSHOUSE PROB DUPLICATE CLEANUP PREVIEW")
    print("=" * 48)
    print(f"Matching PROB rows:        {len(rows)}")
    print(f"Unique matched products:  {len(unique_products)}")
    print(f"PROB units to clear:      {prob_units}")
    print(f"PROB reserved to clear:   {reserved_units}")
    print(f"Storefront units present: {storefront_units}")
    print(f"Back Room units present:  {back_room_units}")

    if not rows:
        print("\nNo matching PROB inventory needs cleanup.")
        return

    print("\nInventory rows that would be cleared:")
    for row in rows[:limit]:
        print(
            f"#{row['inventory_id']} | product #{row['product_id']} | "
            f"{row['product_name']} | PROB {row['prob_quantity']} | "
            f"Storefront {row['storefront_quantity']} | "
            f"Back Room {row['back_room_quantity']} | "
            f"container {row['prob_container_id'] or '-'}"
        )
    remaining = len(rows) - min(len(rows), limit)
    if remaining:
        print(f"... plus {remaining} additional matching PROB rows")


def backup_database(connection: sqlite3.Connection, database_path: Path, reference: str) -> Path:
    backup_directory = database_path.parent / "backups" / "prob-reconciliation"
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / f"{database_path.stem}-{reference}.db"
    backup_connection = sqlite3.connect(backup_path)
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    return backup_path


def write_audit_csv(rows: list[sqlite3.Row], backup_path: Path, reference: str) -> Path:
    csv_path = backup_path.with_name(f"{reference}-cleared-rows.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "inventory_id",
                "product_id",
                "product_name",
                "prob_container_id",
                "prob_quantity_cleared",
                "prob_reserved_cleared",
                "storefront_quantity",
                "back_room_quantity",
                "reference_number",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["inventory_id"],
                    row["product_id"],
                    row["product_name"],
                    row["prob_container_id"],
                    row["prob_quantity"],
                    row["prob_reserved"],
                    row["storefront_quantity"],
                    row["back_room_quantity"],
                    reference,
                ]
            )
    return csv_path


def apply_cleanup(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    database_path: Path,
) -> None:
    if not rows:
        print("\nNothing to apply.")
        return

    reference = f"PROB-DUPLICATE-CLEAR-{reference_timestamp()}"
    backup_path = backup_database(connection, database_path, reference)
    created_at = utc_timestamp()

    cleared_rows = 0
    cleared_units = 0

    try:
        connection.execute("BEGIN IMMEDIATE")

        for preview_row in rows:
            current = connection.execute(
                """
                SELECT quantity_on_hand, quantity_reserved
                FROM inventory
                WHERE inventory_id = ?
                  AND location_id = ?
                """,
                (preview_row["inventory_id"], PROB_LOCATION_ID),
            ).fetchone()

            if current is None:
                raise RuntimeError(
                    f"PROB inventory row #{preview_row['inventory_id']} disappeared."
                )

            if current["quantity_on_hand"] != preview_row["prob_quantity"]:
                raise RuntimeError(
                    f"PROB inventory row #{preview_row['inventory_id']} changed "
                    "after preview. No cleanup was committed."
                )

            trusted_exists = connection.execute(
                """
                SELECT 1
                FROM inventory
                WHERE product_id = ?
                  AND location_id IN (?, ?)
                  AND COALESCE(quantity_on_hand, 0) > 0
                LIMIT 1
                """,
                (
                    preview_row["product_id"],
                    STOREFRONT_LOCATION_ID,
                    BACK_ROOM_LOCATION_ID,
                ),
            ).fetchone()

            if trusted_exists is None:
                raise RuntimeError(
                    f"Product #{preview_row['product_id']} no longer has trusted "
                    "Storefront or Back Room inventory. No cleanup was committed."
                )

            connection.execute(
                """
                UPDATE inventory
                SET quantity_on_hand = 0,
                    quantity_reserved = 0,
                    updated_at = ?
                WHERE inventory_id = ?
                """,
                (created_at, preview_row["inventory_id"]),
            )

            notes = (
                "Cleared stale PROB inventory because positive physical inventory "
                f"exists in Storefront ({preview_row['storefront_quantity']}) "
                f"and/or Store Back Room ({preview_row['back_room_quantity']}). "
                f"Original PROB reserved quantity: {preview_row['prob_reserved']}."
            )

            connection.execute(
                """
                INSERT INTO inventory_transactions (
                    product_id,
                    location_id,
                    transaction_type,
                    quantity_change,
                    unit_cost,
                    reference_number,
                    notes,
                    created_at,
                    container_id,
                    performed_by_user_id,
                    performed_by_name,
                    performed_by_role
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview_row["product_id"],
                    PROB_LOCATION_ID,
                    "adjustment",
                    -preview_row["prob_quantity"],
                    preview_row["average_cost"],
                    reference,
                    notes,
                    created_at,
                    preview_row["prob_container_id"],
                    None,
                    "System - PROB Reconciliation",
                    "admin",
                ),
            )

            cleared_rows += 1
            cleared_units += preview_row["prob_quantity"]

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    audit_path = write_audit_csv(rows, backup_path, reference)

    print("\nAPPLIED SUCCESSFULLY")
    print(f"Cleared PROB rows:  {cleared_rows}")
    print(f"Cleared PROB units: {cleared_units}")
    print(f"Reference:          {reference}")
    print(f"Database backup:    {backup_path}")
    print(f"Audit CSV:          {audit_path}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clear PROB inventory for products that already have positive "
            "Storefront or Store Back Room inventory."
        )
    )
    parser.add_argument("--database", help="Explicit SQLite database path")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cleanup. Without this flag, the script only previews.",
    )
    parser.add_argument(
        "--limit-display",
        type=int,
        default=100,
        help="Maximum matching rows printed during preview (default: 100)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    database_path = resolve_database(arguments.database)
    print(f"Database: {database_path}")

    connection = sqlite3.connect(database_path, timeout=60)
    connection.row_factory = sqlite3.Row

    try:
        verify_schema(connection)
        rows = load_targets(connection)
        print_preview(rows, max(0, arguments.limit_display))

        if arguments.apply:
            apply_cleanup(connection, rows, database_path)
        elif rows:
            print("\nPREVIEW ONLY — no inventory was changed.")
            print("Run again with --apply only after reviewing this preview.")
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
