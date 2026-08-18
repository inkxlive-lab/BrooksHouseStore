import csv
import json
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATABASE_PATH = (
    PROJECT_ROOT
    / "app"
    / "data"
    / "brookshouse_store.db"
)

OUTPUT_PATH = (
    PROJECT_ROOT
    / "shopify-to-storefront-preview.csv"
)

STOREFRONT_LOCATION_ID = 1


def normalize_barcode(value):
    if value is None:
        return ""

    return "".join(
        character
        for character in str(value).strip()
        if character.isdigit()
    )


connection = sqlite3.connect(DATABASE_PATH)
connection.row_factory = sqlite3.Row

try:
    shopify = connection.execute(
        """
        SELECT channel_id
        FROM sales_channels
        WHERE LOWER(channel_name) = 'shopify'
        LIMIT 1
        """
    ).fetchone()

    if shopify is None:
        raise RuntimeError(
            "Shopify sales channel was not found."
        )

    listings = connection.execute(
        """
        SELECT
            listing_id,
            external_variant_id,
            listing_title,
            variant_title,
            barcode_exact,
            barcode_lookup,
            quantity_available
        FROM channel_listings
        WHERE channel_id = ?
          AND COALESCE(quantity_available, 0) > 0
        ORDER BY listing_title, variant_title
        """,
        (shopify["channel_id"],),
    ).fetchall()

    barcode_rows = connection.execute(
        """
        SELECT
            pb.product_id,
            pb.barcode,
            p.product_name
        FROM product_barcodes pb
        JOIN products p
          ON p.product_id = pb.product_id
        """
    ).fetchall()

    products_by_barcode = {}

    for row in barcode_rows:
        barcode = normalize_barcode(
            row["barcode"]
        )

        if not barcode:
            continue

        products_by_barcode.setdefault(
            barcode,
            [],
        ).append(
            {
                "product_id": row["product_id"],
                "product_name": row["product_name"],
            }
        )

    preview_rows = []

    summary = {
        "positive_shopify_listings": 0,
        "safe_matches": 0,
        "missing_barcode": 0,
        "not_in_brookshouse": 0,
        "duplicate_local_match": 0,
        "already_same": 0,
        "would_set_quantity": 0,
        "shopify_units": 0,
    }

    for listing in listings:
        summary["positive_shopify_listings"] += 1

        shopify_quantity = int(
            listing["quantity_available"] or 0
        )

        summary["shopify_units"] += (
            shopify_quantity
        )

        barcode = normalize_barcode(
            listing["barcode_exact"]
            or listing["barcode_lookup"]
        )

        base_row = {
            "status": "",
            "listing_id": listing["listing_id"],
            "external_variant_id": (
                listing["external_variant_id"]
            ),
            "shopify_title": (
                listing["listing_title"] or ""
            ),
            "variant_title": (
                listing["variant_title"] or ""
            ),
            "barcode": barcode,
            "product_id": "",
            "product_name": "",
            "current_storefront_quantity": "",
            "shopify_quantity": shopify_quantity,
            "proposed_storefront_quantity": "",
            "quantity_change": "",
            "notes": "",
        }

        if not barcode:
            summary["missing_barcode"] += 1
            base_row["status"] = "missing_barcode"
            base_row["notes"] = (
                "Shopify listing has no usable barcode."
            )
            preview_rows.append(base_row)
            continue

        matches = products_by_barcode.get(
            barcode,
            [],
        )

        unique_product_ids = {
            match["product_id"]
            for match in matches
        }

        if not matches:
            summary["not_in_brookshouse"] += 1
            base_row["status"] = (
                "not_in_brookshouse"
            )
            base_row["notes"] = (
                "No BrooksHouse product matched "
                "this barcode."
            )
            preview_rows.append(base_row)
            continue

        if len(unique_product_ids) != 1:
            summary["duplicate_local_match"] += 1
            base_row["status"] = (
                "duplicate_local_match"
            )
            base_row["notes"] = (
                "Barcode matches more than one "
                "BrooksHouse product."
            )
            preview_rows.append(base_row)
            continue

        product_id = next(
            iter(unique_product_ids)
        )

        product_name = next(
            match["product_name"]
            for match in matches
            if match["product_id"] == product_id
        )

        inventory = connection.execute(
            """
            SELECT
                inventory_id,
                quantity_on_hand
            FROM inventory
            WHERE product_id = ?
              AND location_id = ?
              AND container_id = ''
            LIMIT 1
            """,
            (
                product_id,
                STOREFRONT_LOCATION_ID,
            ),
        ).fetchone()

        current_quantity = (
            int(inventory["quantity_on_hand"])
            if inventory is not None
            else 0
        )

        difference = (
            shopify_quantity
            - current_quantity
        )

        base_row.update(
            {
                "product_id": product_id,
                "product_name": product_name,
                "current_storefront_quantity": (
                    current_quantity
                ),
                "proposed_storefront_quantity": (
                    shopify_quantity
                ),
                "quantity_change": difference,
            }
        )

        summary["safe_matches"] += 1

        if difference == 0:
            summary["already_same"] += 1
            base_row["status"] = "already_same"
            base_row["notes"] = (
                "Storefront already matches Shopify."
            )
        else:
            summary["would_set_quantity"] += 1
            base_row["status"] = (
                "ready_to_initialize"
            )
            base_row["notes"] = (
                "Would set BrooksHouse Storefront "
                "to the Shopify quantity."
            )

        preview_rows.append(base_row)

    with OUTPUT_PATH.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=[
                "status",
                "listing_id",
                "external_variant_id",
                "shopify_title",
                "variant_title",
                "barcode",
                "product_id",
                "product_name",
                "current_storefront_quantity",
                "shopify_quantity",
                "proposed_storefront_quantity",
                "quantity_change",
                "notes",
            ],
        )

        writer.writeheader()
        writer.writerows(preview_rows)

    print("=" * 70)
    print("SHOPIFY TO STOREFRONT PREVIEW")
    print("=" * 70)

    for key, value in summary.items():
        print(f"{key}: {value}")

    print()
    print("Preview file:")
    print(OUTPUT_PATH)
    print()
    print(
        "No inventory was changed."
    )

finally:
    connection.close()
