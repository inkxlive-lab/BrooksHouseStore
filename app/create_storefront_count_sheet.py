import csv
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATABASE_PATH = (
    PROJECT_ROOT
    / "app"
    / "data"
    / "brookshouse_store.db"
)

timestamp = datetime.now().strftime(
    "%Y%m%d-%H%M%S"
)

OUTPUT_PATH = (
    PROJECT_ROOT
    / (
        "brookshouse-storefront-physical-count-"
        f"{timestamp}.csv"
    )
)

STOREFRONT_LOCATION_ID = 1
STOREFRONT_CONTAINER_ID = ""


connection = sqlite3.connect(DATABASE_PATH)
connection.row_factory = sqlite3.Row

try:
    rows = connection.execute(
        """
        SELECT
            p.product_id,
            p.product_name,
            COALESCE(
                MIN(pb.barcode),
                ''
            ) AS barcode,
            i.quantity_on_hand
        FROM inventory i
        JOIN products p
          ON p.product_id = i.product_id
        LEFT JOIN product_barcodes pb
          ON pb.product_id = p.product_id
        WHERE i.location_id = ?
          AND i.container_id = ?
          AND i.quantity_on_hand > 0
        GROUP BY
            p.product_id,
            p.product_name,
            i.quantity_on_hand
        ORDER BY
            p.product_name,
            p.product_id
        """,
        (
            STOREFRONT_LOCATION_ID,
            STOREFRONT_CONTAINER_ID,
        ),
    ).fetchall()

    with OUTPUT_PATH.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=[
                "product_id",
                "barcode",
                "product_name",
                "system_quantity",
                "physical_count",
                "difference",
                "action",
                "notes",
            ],
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    "product_id": (
                        row["product_id"]
                    ),
                    "barcode": (
                        row["barcode"] or ""
                    ),
                    "product_name": (
                        row["product_name"]
                    ),
                    "system_quantity": (
                        row["quantity_on_hand"]
                    ),
                    "physical_count": "",
                    "difference": "",
                    "action": "",
                    "notes": "",
                }
            )

    total_units = sum(
        int(row["quantity_on_hand"])
        for row in rows
    )

    print("=" * 70)
    print("STOREFRONT PHYSICAL COUNT SHEET")
    print("=" * 70)
    print(f"Products: {len(rows)}")
    print(f"System units: {total_units}")
    print()
    print("Count sheet created:")
    print(OUTPUT_PATH)

finally:
    connection.close()
