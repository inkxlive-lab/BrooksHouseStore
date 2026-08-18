"""Preview active Shopify records before importing storefront inventory."""

import json
from decimal import Decimal
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.sales_channels import (
    ChannelListing,
    SalesChannel,
)


def clean_text(value: Any) -> str:
    """Return a stripped string without raising on None."""

    return str(value or "").strip()


def normalize_exact_barcode(value: Any) -> str:
    """Keep only barcode digits."""

    return "".join(
        character
        for character in clean_text(value)
        if character.isdigit()
    )


def normalize_lookup_barcode(value: Any) -> str:
    """Remove leading zeros for fallback comparison."""

    exact = normalize_exact_barcode(value)

    if not exact:
        return ""

    return exact.lstrip("0") or "0"


def parse_source_data(
    listing: ChannelListing,
) -> dict[str, Any]:
    """Safely read the raw Shopify record."""

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


def find_shopify_unit_cost(
    listing: ChannelListing,
) -> Decimal | None:
    """Extract Shopify's per-unit cost from saved source data."""

    source = parse_source_data(listing)

    inventory_item = source.get(
        "inventoryItem"
    )

    if not isinstance(inventory_item, dict):
        return None

    unit_cost = inventory_item.get(
        "unitCost"
    )

    if isinstance(unit_cost, dict):
        amount = unit_cost.get("amount")
    else:
        amount = unit_cost

    if amount in (None, ""):
        return None

    try:
        return Decimal(str(amount))
    except Exception:
        return None


def find_image_url(
    listing: ChannelListing,
) -> str | None:
    """Find a useful Shopify image URL in source_data."""

    source = parse_source_data(listing)

    variant_image = source.get("image")

    if isinstance(variant_image, dict):
        image_url = clean_text(
            variant_image.get("url")
        )

        if image_url:
            return image_url

    product = source.get("product")

    if not isinstance(product, dict):
        return None

    featured_media = product.get(
        "featuredMedia"
    )

    if isinstance(featured_media, dict):
        image = featured_media.get("image")

        if isinstance(image, dict):
            image_url = clean_text(
                image.get("url")
            )

            if image_url:
                return image_url

    featured_image = product.get(
        "featuredImage"
    )

    if isinstance(featured_image, dict):
        image_url = clean_text(
            featured_image.get("url")
        )

        if image_url:
            return image_url

    return None


def build_storefront_import_preview(
    database: Session,
) -> dict[str, Any]:
    """Analyze active Shopify records without changing the database."""

    channel = database.scalar(
        select(SalesChannel).where(
            SalesChannel.channel_name == "Shopify"
        )
    )

    empty_summary = {
        "total_listings": 0,
        "active_listings": 0,
        "active_with_barcode": 0,
        "active_missing_barcode": 0,
        "duplicate_active_barcodes": 0,
        "active_quantity_above_zero": 0,
        "active_zero_quantity": 0,
        "safe_to_import": 0,
        "needs_review": 0,
        "safe_units": 0,
    }

    if channel is None:
        return {
            "channel_found": False,
            "summary": empty_summary,
            "safe_rows": [],
            "review_rows": [],
        }

    listings = database.scalars(
        select(ChannelListing)
        .where(
            ChannelListing.channel_id
            == channel.channel_id
        )
        .order_by(
            ChannelListing.listing_title,
            ChannelListing.listing_id,
        )
    ).all()

    active_listings = [
        listing
        for listing in listings
        if clean_text(
            listing.listing_status
        ).upper() == "ACTIVE"
    ]

    barcode_groups: dict[
        str,
        list[ChannelListing],
    ] = defaultdict(list)

    for listing in active_listings:
        barcode = normalize_exact_barcode(
            listing.barcode_exact
            or listing.barcode_raw
        )

        if barcode:
            barcode_groups[barcode].append(
                listing
            )

    duplicate_barcodes = {
        barcode
        for barcode, grouped_listings
        in barcode_groups.items()
        if len(grouped_listings) > 1
    }

    safe_rows = []
    review_rows = []

    for listing in active_listings:
        exact_barcode = normalize_exact_barcode(
            listing.barcode_exact
            or listing.barcode_raw
        )

        lookup_barcode = normalize_lookup_barcode(
            exact_barcode
        )

        raw_quantity = (
            listing.quantity_available or 0
        )

        quantity = max(
            int(raw_quantity),
            0,
        )

        unit_cost = find_shopify_unit_cost(
            listing
        )

        image_url = find_image_url(
            listing
        )

        base_row = {
            "listing_id": listing.listing_id,
            "external_product_id": (
                listing.external_product_id
            ),
            "external_variant_id": (
                listing.external_variant_id
            ),
            "title": (
                listing.listing_title
                or "Untitled Shopify product"
            ),
            "variant_title": (
                listing.variant_title
            ),
            "sku": listing.sku,
            "barcode": (
                exact_barcode or None
            ),
            "barcode_lookup": (
                lookup_barcode or None
            ),
            "quantity": quantity,
            "price": (
                listing.listed_price
            ),
            "unit_cost": unit_cost,
            "vendor": listing.vendor,
            "shopify_status": (
                listing.listing_status
            ),
            "image_url": image_url,
            "has_image": bool(image_url),
            "reason": None,
        }

        if not exact_barcode:
            base_row["reason"] = (
                "Active Shopify listing is missing a barcode."
            )

            review_rows.append(base_row)
            continue

        if exact_barcode in duplicate_barcodes:
            duplicate_count = len(
                barcode_groups[exact_barcode]
            )

            base_row["reason"] = (
                f"{duplicate_count} active Shopify listings "
                "share this barcode."
            )

            review_rows.append(base_row)
            continue

        safe_rows.append(base_row)

    safe_rows.sort(
        key=lambda row: (
            -(row["quantity"] or 0),
            row["title"].lower(),
        )
    )

    review_rows.sort(
        key=lambda row: (
            row["reason"] or "",
            row["title"].lower(),
        )
    )

    summary = {
        "total_listings": len(listings),
        "active_listings": len(
            active_listings
        ),
        "active_with_barcode": sum(
            1
            for listing in active_listings
            if normalize_exact_barcode(
                listing.barcode_exact
                or listing.barcode_raw
            )
        ),
        "active_missing_barcode": sum(
            1
            for listing in active_listings
            if not normalize_exact_barcode(
                listing.barcode_exact
                or listing.barcode_raw
            )
        ),
        "duplicate_active_barcodes": len(
            duplicate_barcodes
        ),
        "active_quantity_above_zero": sum(
            1
            for listing in active_listings
            if (
                listing.quantity_available
                or 0
            ) > 0
        ),
        "active_zero_quantity": sum(
            1
            for listing in active_listings
            if (
                listing.quantity_available
                or 0
            ) <= 0
        ),
        "safe_to_import": len(safe_rows),
        "needs_review": len(review_rows),
        "safe_units": sum(
            row["quantity"] or 0
            for row in safe_rows
        ),
    }

    return {
        "channel_found": True,
        "summary": summary,
        "safe_rows": safe_rows,
        "review_rows": review_rows,
    }


