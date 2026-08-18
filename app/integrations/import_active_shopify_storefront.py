"""Import safe ACTIVE Shopify listings into BrooksHouse Storefront."""

import json
from decimal import Decimal

from sqlalchemy import select

from app.database.connection import SessionLocal
from app.database.models import (
    Inventory,
    InventoryLocation,
    InventoryTransaction,
    Product,
    ProductBarcode,
)
from app.services.shopify_storefront_preview import (
    build_storefront_import_preview,
)


STOREFRONT_LOCATION_NAME = "BrooksHouse Storefront"
IMPORT_REFERENCE = "SHOPIFY-ACTIVE-STOREFRONT-IMPORT"


def clean_decimal(value):
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except Exception:
        return None


def extract_shopify_unit_cost(row):
    """Read Shopify unit cost from the preview row or saved source data."""

    direct_cost = clean_decimal(
        row.get("unit_cost")
        or row.get("cost")
        or row.get("average_cost")
    )

    if direct_cost is not None:
        return direct_cost

    source_data = (
        row.get("source_data")
        or row.get("raw_data")
        or row.get("shopify_data")
    )

    if isinstance(source_data, str):
        try:
            source_data = json.loads(source_data)
        except Exception:
            source_data = None

    if not isinstance(source_data, dict):
        return None

    inventory_item = (
        source_data.get("inventoryItem")
        or source_data.get("inventory_item")
        or {}
    )

    unit_cost_data = (
        inventory_item.get("unitCost")
        or inventory_item.get("unit_cost")
        or {}
    )

    if isinstance(unit_cost_data, dict):
        return clean_decimal(
            unit_cost_data.get("amount")
        )

    return clean_decimal(unit_cost_data)


def find_existing_product(database, barcode):
    """Find an existing BrooksHouse product by flexible barcode matching."""

    exact = str(barcode or "").strip()

    if not exact:
        return None

    lookup = exact.lstrip("0") or "0"

    barcode_records = database.scalars(
        select(ProductBarcode)
    ).all()

    for barcode_record in barcode_records:
        stored = str(
            barcode_record.barcode or ""
        ).strip()

        if not stored:
            continue

        if stored == exact:
            return barcode_record.product

        if (stored.lstrip("0") or "0") == lookup:
            return barcode_record.product

        if len(exact) > 1 and stored == exact[:-1]:
            return barcode_record.product

        if len(stored) > 1 and stored[:-1] == exact:
            return barcode_record.product

    return None


def main():
    created_products = 0
    updated_products = 0
    created_barcodes = 0
    created_inventory_records = 0
    updated_inventory_records = 0
    skipped_records = 0

    with SessionLocal() as database:
        storefront = database.scalar(
            select(InventoryLocation).where(
                InventoryLocation.location_name
                == STOREFRONT_LOCATION_NAME
            )
        )

        if storefront is None:
            raise RuntimeError(
                "BrooksHouse Storefront location was not found."
            )

        preview = build_storefront_import_preview(
            database
        )

        safe_rows = preview["safe_rows"]

        print()
        print("ACTIVE SHOPIFY STOREFRONT IMPORT")
        print("--------------------------------")
        print(
            f"Safe ACTIVE listings ready: "
            f"{len(safe_rows)}"
        )
        print()

        for row in safe_rows:
            barcode = row["barcode"]
            title = row["title"]
            quantity = int(
                row["quantity"] or 0
            )

            price = clean_decimal(
                row["price"]
            )

            unit_cost = extract_shopify_unit_cost(
                row
            )

            product = find_existing_product(
                database=database,
                barcode=barcode,
            )

            if product is None:
                product_values = {
                    "product_name": title,
                }

                # These fields exist in the BrooksHouse model
                # created earlier.
                if price is not None:
                    product_values["store_price"] = price

                if unit_cost is not None:
                    product_values["average_cost"] = unit_cost

                product = Product(
                    **product_values
                )

                database.add(product)
                database.flush()

                barcode_record = ProductBarcode(
                    product=product,
                    barcode=barcode,
                )

                database.add(barcode_record)

                created_products += 1
                created_barcodes += 1

            else:
                product.product_name = title

                if price is not None:
                    product.store_price = price

                if unit_cost is not None:
                    product.average_cost = unit_cost

                updated_products += 1

            inventory_record = database.scalar(
                select(Inventory).where(
                    Inventory.product_id
                    == product.product_id,
                    Inventory.location_id
                    == storefront.location_id,
                )
            )

            previous_quantity = 0

            if inventory_record is None:
                inventory_record = Inventory(
                    product=product,
                    location=storefront,
                    quantity_on_hand=0,
                    quantity_reserved=0,
                    reorder_level=0,
                )

                database.add(inventory_record)
                database.flush()

                created_inventory_records += 1

            else:
                previous_quantity = (
                    inventory_record.quantity_on_hand
                    or 0
                )

                updated_inventory_records += 1

            quantity_change = (
                quantity - previous_quantity
            )

            inventory_record.quantity_on_hand = (
                quantity
            )

            # Only create a transaction when the quantity
            # actually changed.
            if quantity_change != 0:
                transaction = InventoryTransaction(
                    product=product,
                    location=storefront,
                    transaction_type=(
                        "shopify_storefront_import"
                    ),
                    quantity_change=quantity_change,
                    unit_cost=product.average_cost,
                    reference_number=IMPORT_REFERENCE,
                    notes=(
                        "Storefront quantity synchronized "
                        "from an ACTIVE Shopify listing. "
                        f"Previous quantity: {previous_quantity}. "
                        f"Shopify quantity: {quantity}."
                    ),
                )

                database.add(transaction)

            cost_display = (
                f"${unit_cost:.2f}"
                if unit_cost is not None
                else "NO COST"
            )

            print(
                f"{quantity:>5} | "
                f"{barcode:<16} | "
                f"{cost_display:>10} | "
                f"{title}"
            )

        database.commit()

    print()
    print("ACTIVE STOREFRONT IMPORT COMPLETE")
    print("---------------------------------")
    print(f"Products created: {created_products}")
    print(f"Products updated: {updated_products}")
    print(f"Barcodes created: {created_barcodes}")
    print(
        "Inventory records created: "
        f"{created_inventory_records}"
    )
    print(
        "Inventory records updated: "
        f"{updated_inventory_records}"
    )
    print(f"Records skipped: {skipped_records}")


if __name__ == "__main__":
    main()

