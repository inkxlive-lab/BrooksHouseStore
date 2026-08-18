"""Build and store Shopify inventory approval records."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Product
from app.database.sales_channels import ChannelListing, SalesChannel


CONFIG_DIRECTORY = (
    Path(__file__).resolve().parent.parent
    / "config"
)

LOCATION_SETTINGS_PATH = (
    CONFIG_DIRECTORY
    / "shopify_inventory_locations.json"
)

APPROVALS_PATH = (
    CONFIG_DIRECTORY
    / "shopify_inventory_approvals.json"
)


def load_selected_location_ids() -> set[int]:
    """Load locations that count toward Shopify inventory."""

    if not LOCATION_SETTINGS_PATH.exists():
        return set()

    try:
        data = json.loads(
            LOCATION_SETTINGS_PATH.read_text(
                encoding="utf-8"
            )
        )

        return {
            int(location_id)
            for location_id in data.get(
                "location_ids",
                [],
            )
        }

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return set()


def load_approvals() -> dict[str, dict[str, Any]]:
    """Load saved approval snapshots."""

    if not APPROVALS_PATH.exists():
        return {}

    try:
        data = json.loads(
            APPROVALS_PATH.read_text(
                encoding="utf-8"
            )
        )

        approvals = data.get(
            "approvals",
            {},
        )

        if isinstance(approvals, dict):
            return approvals

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        pass

    return {}


def save_approvals(
    approvals: dict[str, dict[str, Any]],
) -> None:
    """Save approval snapshots to the config folder."""

    CONFIG_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "approvals": approvals,
        "saved_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    APPROVALS_PATH.write_text(
        json.dumps(
            payload,
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_approvals() -> None:
    """Remove all saved Shopify approvals."""

    save_approvals({})


def approval_key(
    listing_id: int,
    product_id: int,
) -> str:
    return f"{listing_id}:{product_id}"


def build_approval_candidates(
    database: Session,
) -> list[dict[str, Any]]:
    """Build safe, single-listing Shopify mismatches."""

    channel = database.scalar(
        select(SalesChannel).where(
            SalesChannel.channel_name
            == "Shopify"
        )
    )

    if channel is None:
        return []

    selected_location_ids = (
        load_selected_location_ids()
    )

    if not selected_location_ids:
        return []

    products = database.scalars(
        select(Product).order_by(
            Product.product_name
        )
    ).unique().all()

    listings = database.scalars(
        select(ChannelListing)
        .where(
            ChannelListing.channel_id
            == channel.channel_id,
            ChannelListing.listing_status
            == "ACTIVE",
        )
        .order_by(
            ChannelListing.listing_title,
            ChannelListing.listing_id,
        )
    ).all()

    by_exact: dict[
        str,
        list[ChannelListing],
    ] = {}

    by_lookup: dict[
        str,
        list[ChannelListing],
    ] = {}

    for listing in listings:
        exact = (
            listing.barcode_exact or ""
        ).strip()

        lookup = (
            listing.barcode_lookup or ""
        ).strip()

        if exact:
            by_exact.setdefault(
                exact,
                [],
            ).append(listing)

        if lookup:
            by_lookup.setdefault(
                lookup,
                [],
            ).append(listing)

    saved_approvals = load_approvals()
    candidates: list[dict[str, Any]] = []

    for product in products:
        local_quantity = sum(
            record.quantity_on_hand or 0
            for record in product.inventory_records
            if record.location_id
            in selected_location_ids
        )

        product_barcodes = [
            barcode_record.barcode.strip()
            for barcode_record in product.barcodes
            if barcode_record.barcode
            and barcode_record.barcode.strip()
        ]

        matched_listings: list[
            ChannelListing
        ] = []

        matched_barcode = None
        match_method = None

        for barcode in product_barcodes:
            exact_matches = by_exact.get(
                barcode,
                [],
            )

            if exact_matches:
                matched_listings = exact_matches
                matched_barcode = barcode
                match_method = "Exact barcode"
                break

            lookup = (
                barcode.lstrip("0")
                or "0"
            )

            lookup_matches = by_lookup.get(
                lookup,
                [],
            )

            if lookup_matches:
                matched_listings = lookup_matches
                matched_barcode = barcode
                match_method = "Leading-zero match"
                break

            if len(barcode) > 1:
                without_check_digit = (
                    barcode[:-1]
                )

                check_digit_matches = (
                    by_exact.get(
                        without_check_digit,
                        [],
                    )
                )

                if check_digit_matches:
                    matched_listings = (
                        check_digit_matches
                    )

                    matched_barcode = barcode
                    match_method = (
                        "Check digit removed"
                    )

                    break

                lookup_without_check_digit = (
                    without_check_digit
                    .lstrip("0")
                    or "0"
                )

                fallback_matches = (
                    by_lookup.get(
                        lookup_without_check_digit,
                        [],
                    )
                )

                if fallback_matches:
                    matched_listings = (
                        fallback_matches
                    )

                    matched_barcode = barcode
                    match_method = (
                        "Check digit and "
                        "leading zeros removed"
                    )

                    break

        # Duplicate Shopify barcodes are deliberately
        # excluded from approval.
        if len(matched_listings) != 1:
            continue

        listing = matched_listings[0]

        shopify_quantity = (
            listing.quantity_available or 0
        )

        difference = (
            local_quantity
            - shopify_quantity
        )

        # Matching quantities do not need approval.
        if difference == 0:
            continue

        key = approval_key(
            listing.listing_id,
            product.product_id,
        )

        saved = saved_approvals.get(key)

        approved = saved is not None

        stale = False

        if saved is not None:
            stale = (
                saved.get("local_quantity")
                != local_quantity
                or saved.get(
                    "shopify_quantity"
                )
                != shopify_quantity
                or saved.get("barcode")
                != matched_barcode
            )

        status = (
            "local_higher"
            if difference > 0
            else "shopify_higher"
        )

        candidates.append(
            {
                "approval_key": key,
                "listing_id": (
                    listing.listing_id
                ),
                "product_id": (
                    product.product_id
                ),
                "product_name": (
                    product.product_name
                ),
                "barcode": matched_barcode,
                "local_quantity": (
                    local_quantity
                ),
                "shopify_quantity": (
                    shopify_quantity
                ),
                "difference": difference,
                "status": status,
                "status_label": (
                    "Local Higher"
                    if difference > 0
                    else "Shopify Higher"
                ),
                "shopify_status": (
                    listing.listing_status
                ),
                "shopify_price": (
                    listing.listed_price
                ),
                "match_method": (
                    match_method
                ),
                "approved": approved,
                "stale": stale,
                "approved_at": (
                    saved.get("approved_at")
                    if saved
                    else None
                ),
            }
        )

    candidates.sort(
        key=lambda row: (
            0 if row["stale"] else 1,
            0 if row["approved"] else 1,
            row["product_name"].lower(),
        )
    )

    return candidates


def save_selected_approvals(
    candidates: list[dict[str, Any]],
    selected_keys: set[str],
) -> int:
    """Save only currently selected, validated candidates."""

    approvals: dict[
        str,
        dict[str, Any],
    ] = {}

    approved_at = datetime.now(
        timezone.utc
    ).isoformat()

    for row in candidates:
        key = row["approval_key"]

        if key not in selected_keys:
            continue

        approvals[key] = {
            "approval_key": key,
            "listing_id": row["listing_id"],
            "product_id": row["product_id"],
            "product_name": (
                row["product_name"]
            ),
            "barcode": row["barcode"],
            "local_quantity": (
                row["local_quantity"]
            ),
            "shopify_quantity": (
                row["shopify_quantity"]
            ),
            "difference": row["difference"],
            "approved_at": approved_at,
        }

    save_approvals(approvals)

    return len(approvals)
