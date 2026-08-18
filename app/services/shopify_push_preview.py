"""Build a safe preview of approved Shopify inventory changes."""

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Product
from app.database.sales_channels import ChannelListing
from app.services.shopify_approval import (
    approval_key,
    load_approvals,
    load_selected_location_ids,
)


CONFIG_DIRECTORY = (
    Path(__file__).resolve().parent.parent
    / "config"
)

SHOPIFY_PUSH_SETTINGS_PATH = (
    CONFIG_DIRECTORY
    / "shopify_push_settings.json"
)


def load_shopify_push_settings() -> dict[str, Any]:
    """Load the selected Shopify destination location."""

    if not SHOPIFY_PUSH_SETTINGS_PATH.exists():
        return {
            "shopify_location_id": "",
            "shopify_location_name": "",
        }

    try:
        data = json.loads(
            SHOPIFY_PUSH_SETTINGS_PATH.read_text(
                encoding="utf-8"
            )
        )

        return {
            "shopify_location_id": str(
                data.get(
                    "shopify_location_id",
                    "",
                )
            ),
            "shopify_location_name": str(
                data.get(
                    "shopify_location_name",
                    "",
                )
            ),
        }

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return {
            "shopify_location_id": "",
            "shopify_location_name": "",
        }


def save_shopify_push_settings(
    shopify_location_id: str,
    shopify_location_name: str,
) -> None:
    """Save the selected Shopify location."""

    CONFIG_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "shopify_location_id": (
            shopify_location_id.strip()
        ),
        "shopify_location_name": (
            shopify_location_name.strip()
        ),
    }

    SHOPIFY_PUSH_SETTINGS_PATH.write_text(
        json.dumps(
            payload,
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_source_data(
    listing: ChannelListing,
) -> dict[str, Any]:
    """Safely parse the original Shopify listing data."""

    source_data = listing.source_data

    if isinstance(source_data, dict):
        return source_data

    if not source_data:
        return {}

    try:
        parsed = json.loads(source_data)

        if isinstance(parsed, dict):
            return parsed

    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        pass

    return {}


def find_inventory_item_id(
    listing: ChannelListing,
) -> str | None:
    """Find Shopify's inventoryItem GraphQL ID."""

    direct_fields = [
        "inventory_item_id",
        "inventoryItemId",
    ]

    for field_name in direct_fields:
        value = getattr(
            listing,
            field_name,
            None,
        )

        if value:
            return str(value)

    source_data = parse_source_data(
        listing
    )

    inventory_item = source_data.get(
        "inventoryItem"
    )

    if isinstance(inventory_item, dict):
        inventory_item_id = (
            inventory_item.get("id")
        )

        if inventory_item_id:
            return str(inventory_item_id)

    inventory_item_id = source_data.get(
        "inventoryItemId"
    )

    if inventory_item_id:
        return str(inventory_item_id)

    return None


def build_shopify_push_preview(
    database: Session,
) -> list[dict[str, Any]]:
    """Validate saved approvals and create a push preview."""

    approvals = load_approvals()

    selected_location_ids = (
        load_selected_location_ids()
    )

    preview_rows: list[
        dict[str, Any]
    ] = []

    for saved_key, approval in approvals.items():
        listing_id = approval.get(
            "listing_id"
        )

        product_id = approval.get(
            "product_id"
        )

        listing = database.get(
            ChannelListing,
            listing_id,
        )

        product = database.get(
            Product,
            product_id,
        )

        base_row = {
            "approval_key": saved_key,
            "listing_id": listing_id,
            "product_id": product_id,
            "product_name": approval.get(
                "product_name",
                "Unknown product",
            ),
            "barcode": approval.get(
                "barcode"
            ),
            "approved_local_quantity": (
                approval.get(
                    "local_quantity"
                )
            ),
            "approved_shopify_quantity": (
                approval.get(
                    "shopify_quantity"
                )
            ),
            "approved_at": approval.get(
                "approved_at"
            ),
            "current_local_quantity": None,
            "current_shopify_quantity": None,
            "quantity_to_send": None,
            "difference": None,
            "inventory_item_id": None,
            "ready": False,
            "status": "blocked",
            "status_label": "Blocked",
            "details": None,
        }

        if product is None:
            base_row["details"] = (
                "The BrooksHouse product no longer exists."
            )

            preview_rows.append(base_row)
            continue

        if listing is None:
            base_row["details"] = (
                "The imported Shopify listing no longer exists."
            )

            preview_rows.append(base_row)
            continue

        current_local_quantity = sum(
            record.quantity_on_hand or 0
            for record in product.inventory_records
            if record.location_id
            in selected_location_ids
        )

        current_shopify_quantity = (
            listing.quantity_available or 0
        )

        inventory_item_id = (
            find_inventory_item_id(
                listing
            )
        )

        base_row.update(
            {
                "product_name": (
                    product.product_name
                ),
                "barcode": (
                    listing.barcode_exact
                    or approval.get(
                        "barcode"
                    )
                ),
                "current_local_quantity": (
                    current_local_quantity
                ),
                "current_shopify_quantity": (
                    current_shopify_quantity
                ),
                "quantity_to_send": (
                    current_local_quantity
                ),
                "difference": (
                    current_local_quantity
                    - current_shopify_quantity
                ),
                "inventory_item_id": (
                    inventory_item_id
                ),
            }
        )

        expected_key = approval_key(
            listing.listing_id,
            product.product_id,
        )

        if saved_key != expected_key:
            base_row["details"] = (
                "The saved approval key does not match "
                "the current product and Shopify listing."
            )

            preview_rows.append(base_row)
            continue

        approval_is_stale = (
            approval.get("local_quantity")
            != current_local_quantity
            or approval.get(
                "shopify_quantity"
            )
            != current_shopify_quantity
            or approval.get("barcode")
            != base_row["barcode"]
        )

        if approval_is_stale:
            base_row["status"] = "stale"
            base_row["status_label"] = (
                "Stale Approval"
            )

            base_row["details"] = (
                "The local quantity, Shopify quantity, "
                "or barcode changed after approval."
            )

            preview_rows.append(base_row)
            continue

        if not inventory_item_id:
            base_row["status"] = (
                "missing_inventory_item"
            )

            base_row["status_label"] = (
                "Missing Inventory Item ID"
            )

            base_row["details"] = (
                "The Shopify import did not save the "
                "inventoryItem ID required for an update."
            )

            preview_rows.append(base_row)
            continue

        if (
            current_local_quantity
            == current_shopify_quantity
        ):
            base_row["status"] = (
                "already_matches"
            )

            base_row["status_label"] = (
                "Already Matches"
            )

            base_row["details"] = (
                "No Shopify update is needed."
            )

            preview_rows.append(base_row)
            continue

        base_row["ready"] = True
        base_row["status"] = "ready"
        base_row["status_label"] = (
            "Ready for Final Confirmation"
        )

        base_row["details"] = (
            "Approval and quantities are current."
        )

        preview_rows.append(base_row)

    priority = {
        "stale": 1,
        "missing_inventory_item": 2,
        "blocked": 3,
        "ready": 4,
        "already_matches": 5,
    }

    preview_rows.sort(
        key=lambda row: (
            priority.get(
                row["status"],
                99,
            ),
            row["product_name"].lower(),
        )
    )

    return preview_rows
