import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(r"C:\BrooksHouseStore")

DATABASE_PATH = (
    PROJECT_ROOT
    / "app"
    / "data"
    / "brookshouse_store.db"
)

REPORT_PATH = (
    PROJECT_ROOT
    / "imports"
    / "amazon_inventory_report.txt"
)


def parse_quantity(value):
    value = str(value or "").strip()

    if value == "":
        return None

    return int(float(value))


def parse_price(value):
    value = str(value or "").strip()

    if value == "":
        return None

    return float(value)


def inventory_status(quantity):
    if quantity is None:
        return "quantity_unknown"

    if quantity > 0:
        return "in_stock"

    return "out_of_stock"


def main():
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(
            f"Database not found: {DATABASE_PATH}"
        )

    if not REPORT_PATH.exists():
        raise FileNotFoundError(
            f"Amazon report not found: {REPORT_PATH}"
        )

    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row

    imported_at = datetime.now(
        timezone.utc
    ).isoformat()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS amazon_listings (
            amazon_listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_sku TEXT NOT NULL UNIQUE,
            asin TEXT NOT NULL,
            amazon_price REAL,
            amazon_quantity INTEGER,
            approval_status TEXT NOT NULL DEFAULT 'approved',
            inventory_status TEXT NOT NULL,
            source_file TEXT,
            first_seen_at TEXT NOT NULL,
            last_imported_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS
            ix_amazon_listings_asin
        ON amazon_listings (asin);

        CREATE TABLE IF NOT EXISTS amazon_product_links (
            amazon_product_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            amazon_listing_id INTEGER NOT NULL UNIQUE,
            product_id INTEGER,
            match_status TEXT NOT NULL DEFAULT 'unmatched',
            match_method TEXT,
            match_value TEXT,
            linked_at TEXT
        );
        """
    )

    rows_read = 0
    rows_imported = 0
    in_stock = 0
    out_of_stock = 0
    quantity_unknown = 0

    with REPORT_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as report_file:
        reader = csv.DictReader(
            report_file,
            delimiter="\t",
        )

        print()
        print("AMAZON INVENTORY IMPORT")
        print("-----------------------")

        for row in reader:
            rows_read += 1

            seller_sku = str(
                row.get("sku") or ""
            ).strip()

            asin = str(
                row.get("asin") or ""
            ).strip()

            if not seller_sku or not asin:
                print(
                    f"SKIPPED row {rows_read}: "
                    "missing SKU or ASIN"
                )
                continue

            price = parse_price(
                row.get("price")
            )

            quantity = parse_quantity(
                row.get("quantity")
            )

            status = inventory_status(
                quantity
            )

            connection.execute(
                """
                INSERT INTO amazon_listings (
                    seller_sku,
                    asin,
                    amazon_price,
                    amazon_quantity,
                    approval_status,
                    inventory_status,
                    source_file,
                    first_seen_at,
                    last_imported_at
                )
                VALUES (?, ?, ?, ?, 'approved', ?, ?, ?, ?)
                ON CONFLICT(seller_sku)
                DO UPDATE SET
                    asin = excluded.asin,
                    amazon_price = excluded.amazon_price,
                    amazon_quantity = excluded.amazon_quantity,
                    approval_status = 'approved',
                    inventory_status = excluded.inventory_status,
                    source_file = excluded.source_file,
                    last_imported_at = excluded.last_imported_at
                """,
                (
                    seller_sku,
                    asin,
                    price,
                    quantity,
                    status,
                    REPORT_PATH.name,
                    imported_at,
                    imported_at,
                ),
            )

            listing = connection.execute(
                """
                SELECT amazon_listing_id
                FROM amazon_listings
                WHERE seller_sku = ?
                """,
                (seller_sku,),
            ).fetchone()

            connection.execute(
                """
                INSERT INTO amazon_product_links (
                    amazon_listing_id,
                    product_id,
                    match_status,
                    match_method,
                    match_value,
                    linked_at
                )
                VALUES (?, NULL, 'unmatched', NULL, ?, NULL)
                ON CONFLICT(amazon_listing_id)
                DO NOTHING
                """,
                (
                    listing["amazon_listing_id"],
                    seller_sku,
                ),
            )

            rows_imported += 1

            if status == "in_stock":
                in_stock += 1
            elif status == "out_of_stock":
                out_of_stock += 1
            else:
                quantity_unknown += 1

            print(
                f"{seller_sku:<16} | "
                f"{asin:<12} | "
                f"Qty: {str(quantity):>4} | "
                f"{status}"
            )

    connection.commit()
    connection.close()

    print()
    print("AMAZON IMPORT COMPLETE")
    print("----------------------")
    print(f"Rows read: {rows_read}")
    print(f"Rows imported: {rows_imported}")
    print(f"In stock: {in_stock}")
    print(f"Out of stock: {out_of_stock}")
    print(
        f"Quantity unknown: "
        f"{quantity_unknown}"
    )
    print()
    print(
        "All imported rows are marked "
        "Amazon approved/listed."
    )
    print(
        "No BrooksHouse inventory quantities "
        "were changed."
    )


if __name__ == "__main__":
    main()
