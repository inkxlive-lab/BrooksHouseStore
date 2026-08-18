import csv
import shutil
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

PREVIEW_PATH = (
    PROJECT_ROOT
    / "shopify-to-storefront-preview.csv"
)

BACKUP_DIRECTORY = (
    PROJECT_ROOT
    / "backups"
)

STOREFRONT_LOCATION_ID = 1
STOREFRONT_CONTAINER_ID = ""

TRANSACTION_TYPE = "shopify_initial_import"
REFERENCE_NUMBER = "SHOPIFY-STOREFRONT-INITIAL"


def normalize_barcode(value):
    if value is None:
        return ""

    return "".join(
        character
        for character in str(value).strip()
        if character.isdigit()
    )


if not DATABASE_PATH.exists():
    raise FileNotFoundError(
        f"Database not found: {DATABASE_PATH}"
    )

if not PREVIEW_PATH.exists():
    raise FileNotFoundError(
        f"Preview CSV not found: {PREVIEW_PATH}"
    )


timestamp = datetime.now().strftime(
    "%Y%m%d-%H%M%S"
)

BACKUP_DIRECTORY.mkdir(
    parents=True,
    exist_ok=True,
)

backup_path = (
    BACKUP_DIRECTORY
    / (
        "brookshouse_store-before-"
        "shopify-storefront-import-"
        f"{timestamp}.db"
    )
)

results_path = (
    PROJECT_ROOT
    / (
        "shopify-to-storefront-applied-"
        f"{timestamp}.csv"
    )
)


with PREVIEW_PATH.open(
    "r",
    newline="",
    encoding="utf-8-sig",
) as preview_file:
    preview_rows = list(
        csv.DictReader(preview_file)
    )


ready_rows = [
    row
    for row in preview_rows
    if row.get("status")
    == "ready_to_initialize"
]


print("=" * 70)
print("SHOPIFY TO STOREFRONT IMPORT")
print("=" * 70)
print(f"Database: {DATABASE_PATH}")
print(f"Preview: {PREVIEW_PATH}")
print(f"Ready records: {len(ready_rows)}")
print()

if not ready_rows:
    print(
        "No ready_to_initialize records "
        "were found."
    )
    raise SystemExit(0)


print(
    "This will SET BrooksHouse Storefront "
    "quantities to the reviewed Shopify quantities."
)
print()
print(
    "Type APPLY SHOPIFY STOREFRONT "
    "to continue."
)

confirmation = input("> ").strip()

if confirmation != "APPLY SHOPIFY STOREFRONT":
    print("Import cancelled.")
    raise SystemExit(0)


shutil.copy2(
    DATABASE_PATH,
    backup_path,
)

print()
print(f"Database backup created: {backup_path}")


connection = sqlite3.connect(DATABASE_PATH)
connection.row_factory = sqlite3.Row

applied_rows = []


try:
    connection.execute("BEGIN IMMEDIATE")

    shopify_channel = connection.execute(
        """
        SELECT channel_id
        FROM sales_channels
        WHERE LOWER(channel_name) = 'shopify'
        LIMIT 1
        """
    ).fetchone()

    if shopify_channel is None:
        raise RuntimeError(
            "Shopify sales channel was not found."
        )

    shopify_channel_id = int(
        shopify_channel["channel_id"]
    )

    for row in ready_rows:
        listing_id = int(
            row["listing_id"]
        )

        product_id = int(
            row["product_id"]
        )

        preview_barcode = normalize_barcode(
            row["barcode"]
        )

        preview_current_quantity = int(
            row["current_storefront_quantity"]
        )

        preview_shopify_quantity = int(
            row["shopify_quantity"]
        )

        preview_proposed_quantity = int(
            row["proposed_storefront_quantity"]
        )

        if (
            preview_shopify_quantity
            != preview_proposed_quantity
        ):
            raise RuntimeError(
                f"Listing {listing_id}: the preview "
                "Shopify and proposed quantities "
                "do not match."
            )

        listing = connection.execute(
            """
            SELECT
                listing_id,
                channel_id,
                barcode_exact,
                barcode_lookup,
                quantity_available
            FROM channel_listings
            WHERE listing_id = ?
            LIMIT 1
            """,
            (listing_id,),
        ).fetchone()

        if listing is None:
            raise RuntimeError(
                f"Shopify listing {listing_id} "
                "no longer exists."
            )

        if (
            int(listing["channel_id"])
            != shopify_channel_id
        ):
            raise RuntimeError(
                f"Listing {listing_id} is not "
                "a Shopify listing."
            )

        current_shopify_quantity = int(
            listing["quantity_available"] or 0
        )

        if (
            current_shopify_quantity
            != preview_shopify_quantity
        ):
            raise RuntimeError(
                f"Listing {listing_id}: Shopify "
                "quantity changed after preview. "
                f"Preview={preview_shopify_quantity}, "
                f"Current={current_shopify_quantity}."
            )

        current_listing_barcode = (
            normalize_barcode(
                listing["barcode_exact"]
                or listing["barcode_lookup"]
            )
        )

        if (
            current_listing_barcode
            != preview_barcode
        ):
            raise RuntimeError(
                f"Listing {listing_id}: barcode "
                "changed after preview."
            )

        product = connection.execute(
            """
            SELECT product_id, product_name
            FROM products
            WHERE product_id = ?
            LIMIT 1
            """,
            (product_id,),
        ).fetchone()

        if product is None:
            raise RuntimeError(
                f"Product {product_id} "
                "no longer exists."
            )

        inventory = connection.execute(
            """
            SELECT
                inventory_id,
                quantity_on_hand
            FROM inventory
            WHERE product_id = ?
              AND location_id = ?
              AND container_id = ?
            LIMIT 1
            """,
            (
                product_id,
                STOREFRONT_LOCATION_ID,
                STOREFRONT_CONTAINER_ID,
            ),
        ).fetchone()

        current_storefront_quantity = (
            int(inventory["quantity_on_hand"])
            if inventory is not None
            else 0
        )

        if (
            current_storefront_quantity
            != preview_current_quantity
        ):
            raise RuntimeError(
                f"Product {product_id}: Storefront "
                "quantity changed after preview. "
                f"Preview={preview_current_quantity}, "
                f"Current={current_storefront_quantity}."
            )

        quantity_change = (
            preview_proposed_quantity
            - current_storefront_quantity
        )

        if quantity_change == 0:
            continue

        if inventory is None:
            connection.execute(
                """
                INSERT INTO inventory (
                    product_id,
                    location_id,
                    container_id,
                    quantity_on_hand,
                    quantity_reserved,
                    reorder_level,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 0, 0, CURRENT_TIMESTAMP)
                """,
                (
                    product_id,
                    STOREFRONT_LOCATION_ID,
                    STOREFRONT_CONTAINER_ID,
                    preview_proposed_quantity,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE inventory
                SET
                    quantity_on_hand = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE inventory_id = ?
                """,
                (
                    preview_proposed_quantity,
                    inventory["inventory_id"],
                ),
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
                container_id
            )
            VALUES (
                ?,
                ?,
                ?,
                ?,
                NULL,
                ?,
                ?,
                CURRENT_TIMESTAMP,
                ?
            )
            """,
            (
                product_id,
                STOREFRONT_LOCATION_ID,
                TRANSACTION_TYPE,
                quantity_change,
                REFERENCE_NUMBER,
                (
                    "Initialized BrooksHouse Storefront "
                    f"from Shopify listing {listing_id}. "
                    f"Previous quantity: "
                    f"{current_storefront_quantity}. "
                    f"Shopify quantity: "
                    f"{preview_proposed_quantity}."
                ),
                STOREFRONT_CONTAINER_ID,
            ),
        )

        applied_rows.append(
            {
                "listing_id": listing_id,
                "product_id": product_id,
                "product_name": (
                    product["product_name"]
                ),
                "barcode": preview_barcode,
                "previous_storefront_quantity": (
                    current_storefront_quantity
                ),
                "shopify_quantity": (
                    preview_shopify_quantity
                ),
                "new_storefront_quantity": (
                    preview_proposed_quantity
                ),
                "quantity_change": quantity_change,
                "location_id": (
                    STOREFRONT_LOCATION_ID
                ),
                "location_name": (
                    "BrooksHouse Storefront"
                ),
                "container_id": "",
                "reference_number": (
                    REFERENCE_NUMBER
                ),
            }
        )

    connection.commit()

except Exception:
    connection.rollback()
    raise

finally:
    connection.close()


fieldnames = [
    "listing_id",
    "product_id",
    "product_name",
    "barcode",
    "previous_storefront_quantity",
    "shopify_quantity",
    "new_storefront_quantity",
    "quantity_change",
    "location_id",
    "location_name",
    "container_id",
    "reference_number",
]

with results_path.open(
    "w",
    newline="",
    encoding="utf-8-sig",
) as results_file:
    writer = csv.DictWriter(
        results_file,
        fieldnames=fieldnames,
    )

    writer.writeheader()
    writer.writerows(applied_rows)


total_quantity_change = sum(
    row["quantity_change"]
    for row in applied_rows
)


print()
print("=" * 70)
print("IMPORT COMPLETED")
print("=" * 70)
print(
    f"Products updated: {len(applied_rows)}"
)
print(
    f"Net Storefront quantity change: "
    f"{total_quantity_change:+d}"
)
print(f"Database backup: {backup_path}")
print(f"Applied-results CSV: {results_path}")
print()
print(
    "BrooksHouse Storefront now reflects "
    "the reviewed Shopify quantities."
)
