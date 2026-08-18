import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import delete, select

from app.database.connection import (
    Base,
    SessionLocal,
    engine,
)
from app.database.sales_channels import (
    ChannelListing,
    SalesChannel,
)
from app.integrations.test_shopify import (
    clean_store_domain,
    request_access_token,
    required_setting,
    run_graphql_query,
)


CHANNEL_NAME = "Shopify"
PAGE_SIZE = 100


def normalize_barcode(
    value: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    if value is None:
        return None, None

    digits = "".join(
        character
        for character in value.strip()
        if character.isdigit()
    )

    if not digits:
        return None, None

    lookup = digits.lstrip("0") or "0"

    return digits, lookup


def parse_price(
    value: Optional[str],
) -> Optional[Decimal]:
    if value is None:
        return None

    cleaned = str(value).strip()

    if not cleaned:
        return None

    try:
        return Decimal(cleaned)

    except InvalidOperation:
        return None


def get_or_create_channel(
    database,
) -> SalesChannel:
    channel = database.scalar(
        select(SalesChannel).where(
            SalesChannel.channel_name == CHANNEL_NAME
        )
    )

    if channel is None:
        channel = SalesChannel(
            channel_name=CHANNEL_NAME,
            active=True,
        )

        database.add(channel)
        database.commit()
        database.refresh(channel)

    return channel


def shopify_variant_query(
    cursor: Optional[str],
) -> str:
    after_value = (
        json.dumps(cursor)
        if cursor is not None
        else "null"
    )

    return f"""
    query BrooksHouseShopifyImport {{
      productVariants(
        first: {PAGE_SIZE},
        after: {after_value}
      ) {{
        nodes {{
          id
          title
          sku
          barcode
          price
          inventoryQuantity
          inventoryItem {{
            unitCost {{
              amount
              currencyCode
            }}
          }}
          updatedAt
          product {{
            id
            title
            status
            vendor
          }}
        }}
        pageInfo {{
          hasNextPage
          endCursor
        }}
      }}
    }}
    """

def import_shopify() -> None:
    store = clean_store_domain(
        required_setting("SHOPIFY_STORE")
    )

    client_id = required_setting(
        "SHOPIFY_CLIENT_ID"
    )

    client_secret = required_setting(
        "SHOPIFY_CLIENT_SECRET"
    )

    api_version = required_setting(
        "SHOPIFY_API_VERSION"
    )

    print("Requesting Shopify access token...")

    access_token = request_access_token(
        store=store,
        client_id=client_id,
        client_secret=client_secret,
    )

    print("Shopify access token received.")

    Base.metadata.create_all(bind=engine)

    imported_variants: list[dict] = []
    cursor: Optional[str] = None
    page_number = 0

    while True:
        page_number += 1

        result = run_graphql_query(
            store=store,
            api_version=api_version,
            access_token=access_token,
            query=shopify_variant_query(cursor),
        )

        connection = (
            result["data"]["productVariants"]
        )

        variants = connection["nodes"]
        page_info = connection["pageInfo"]

        imported_variants.extend(variants)

        print(
            f"Page {page_number}: "
            f"{len(variants)} variants received — "
            f"{len(imported_variants):,} total"
        )

        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]

        if not cursor:
            raise RuntimeError(
                "Shopify indicated another page exists "
                "but did not return a cursor."
            )

    now = datetime.now()

    with SessionLocal() as database:
        channel = get_or_create_channel(database)

        existing_listings = database.scalars(
            select(ChannelListing).where(
                ChannelListing.channel_id
                == channel.channel_id
            )
        ).all()

        existing_by_variant_id = {
            listing.external_variant_id: listing
            for listing in existing_listings
        }

        imported_variant_ids: set[str] = set()

        created_count = 0
        updated_count = 0
        missing_barcode_count = 0

        for variant in imported_variants:
            product = variant["product"]

            variant_id = variant["id"]
            product_id = product["id"]

            imported_variant_ids.add(variant_id)

            barcode_raw = variant.get("barcode")

            (
                barcode_exact,
                barcode_lookup,
            ) = normalize_barcode(barcode_raw)

            if barcode_exact is None:
                missing_barcode_count += 1

            listing = existing_by_variant_id.get(
                variant_id
            )

            if listing is None:
                listing = ChannelListing(
                    channel_id=channel.channel_id,
                    external_variant_id=variant_id,
                    first_imported_at=now,
                )

                database.add(listing)
                created_count += 1

            else:
                updated_count += 1

            listing.external_product_id = product_id
            listing.listing_title = product.get(
                "title"
            )
            listing.variant_title = variant.get(
                "title"
            )
            listing.sku = (
                variant.get("sku") or None
            )
            listing.barcode_raw = (
                barcode_raw or None
            )
            listing.barcode_exact = barcode_exact
            listing.barcode_lookup = barcode_lookup
            listing.listing_status = product.get(
                "status"
            )
            listing.listed_price = parse_price(
                variant.get("price")
            )
            listing.quantity_available = (
                variant.get("inventoryQuantity")
            )
            listing.vendor = (
                product.get("vendor") or None
            )
            listing.source_data = json.dumps(
                variant,
                separators=(",", ":"),
            )
            listing.last_imported_at = now

        stale_ids = [
            listing.listing_id
            for listing in existing_listings
            if listing.external_variant_id
            not in imported_variant_ids
        ]

        removed_count = len(stale_ids)

        if stale_ids:
            database.execute(
                delete(ChannelListing).where(
                    ChannelListing.listing_id.in_(
                        stale_ids
                    )
                )
            )

        database.commit()

    print("")
    print("Shopify import complete.")
    print(
        f"Variants received: "
        f"{len(imported_variants):,}"
    )
    print(
        f"Listings created: "
        f"{created_count:,}"
    )
    print(
        f"Listings updated: "
        f"{updated_count:,}"
    )
    print(
        f"Old listings removed: "
        f"{removed_count:,}"
    )
    print(
        f"Variants without barcodes: "
        f"{missing_barcode_count:,}"
    )


if __name__ == "__main__":
    import_shopify()



