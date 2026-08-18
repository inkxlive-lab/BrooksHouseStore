"""Safely push approved inventory quantities to Shopify."""

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.integrations.test_shopify import (
    clean_store_domain,
    request_access_token,
    required_setting,
)
from app.services.shopify_approval import (
    APPROVALS_PATH,
    load_approvals,
)
from app.services.shopify_push_preview import (
    build_shopify_push_preview,
    load_shopify_push_settings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_graphql_request(
    store: str,
    api_version: str,
    access_token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Send a Shopify GraphQL request with variables."""

    graphql_url = (
        f"https://{store}/admin/api/"
        f"{api_version}/graphql.json"
    )

    body = json.dumps(
        {
            "query": query,
            "variables": variables,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        graphql_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        response_text = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Shopify rejected the inventory request. "
            f"HTTP {error.code}: {response_text}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            "Could not connect to Shopify: "
            f"{error.reason}"
        ) from error

    if result.get("errors"):
        raise RuntimeError(
            "Shopify GraphQL errors: "
            + json.dumps(
                result["errors"],
                indent=2,
            )
        )

    return result


def remove_completed_approvals(
    approval_keys: list[str],
) -> None:
    """Remove approvals that were successfully pushed."""

    approvals = load_approvals()

    for approval_key in approval_keys:
        approvals.pop(
            approval_key,
            None,
        )

    APPROVALS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    APPROVALS_PATH.write_text(
        json.dumps(
            approvals,
            indent=2,
        ),
        encoding="utf-8",
    )


def push_approved_shopify_inventory(
    database: Session,
) -> dict[str, Any]:
    """Push all currently ready approval rows to Shopify."""

    rows = build_shopify_push_preview(
        database
    )

    ready_rows = [
        row
        for row in rows
        if row.get("ready") is True
        and row.get("status") == "ready"
    ]

    if not ready_rows:
        raise RuntimeError(
            "No approved Shopify inventory changes "
            "are ready to push."
        )

    settings = load_shopify_push_settings()

    location_id = str(
        settings.get(
            "shopify_location_id",
            "",
        )
    ).strip()

    location_name = str(
        settings.get(
            "shopify_location_name",
            "",
        )
    ).strip()

    if not location_id.startswith(
        "gid://shopify/Location/"
    ):
        raise RuntimeError(
            "Save a valid Shopify destination "
            "location before pushing inventory."
        )

    if len(ready_rows) > 250:
        raise RuntimeError(
            "The current push contains more than "
            "250 records. Approve and push a smaller "
            "batch."
        )

    quantities = []

    for row in ready_rows:
        inventory_item_id = str(
            row.get(
                "inventory_item_id",
                "",
            )
        ).strip()

        if not inventory_item_id.startswith(
            "gid://shopify/InventoryItem/"
        ):
            raise RuntimeError(
                f"{row['product_name']} is missing "
                "a valid Shopify Inventory Item ID."
            )

        quantity_to_send = int(
            row["quantity_to_send"]
        )

        current_shopify_quantity = int(
            row["current_shopify_quantity"]
        )

        if quantity_to_send < 0:
            raise RuntimeError(
                f"{row['product_name']} has a "
                "negative local quantity."
            )

        quantities.append(
            {
                "inventoryItemId": (
                    inventory_item_id
                ),
                "locationId": location_id,
                "quantity": quantity_to_send,
                "changeFromQuantity": (
                    current_shopify_quantity
                ),
            }
        )

    store = clean_store_domain(
        required_setting("SHOPIFY_STORE")
    )

    client_id = required_setting(
        "SHOPIFY_CLIENT_ID"
    )

    client_secret = required_setting(
        "SHOPIFY_CLIENT_SECRET"
    )

    api_version = os.getenv(
        "SHOPIFY_API_VERSION",
        "2026-07",
    ).strip()

    access_token = request_access_token(
        store=store,
        client_id=client_id,
        client_secret=client_secret,
    )

    mutation = """
    mutation BrooksHouseInventoryPush(
      $input: InventorySetQuantitiesInput!,
      $idempotencyKey: String!
    ) {
      inventorySetQuantities(input: $input)
        @idempotent(key: $idempotencyKey) {
        inventoryAdjustmentGroup {
          createdAt
          reason
          referenceDocumentUri
          changes {
            name
            delta
            quantityAfterChange
          }
        }
        userErrors {
          code
          field
          message
        }
      }
    }
    """

    reference_uri = (
        "brookshouse://inventory/shopify-push/"
        + datetime.now().strftime(
            "%Y%m%d-%H%M%S"
        )
    )

    variables = {
        "input": {
            "name": "available",
            "reason": "correction",
            "referenceDocumentUri": (
                reference_uri
            ),
            "quantities": quantities,
        },
        "idempotencyKey": str(
            uuid.uuid4()
        ),
    }

    result = run_graphql_request(
        store=store,
        api_version=api_version,
        access_token=access_token,
        query=mutation,
        variables=variables,
    )

    mutation_result = (
        result.get("data", {})
        .get(
            "inventorySetQuantities",
            {},
        )
    )

    user_errors = mutation_result.get(
        "userErrors",
        [],
    )

    if user_errors:
        messages = []

        for error in user_errors:
            code = error.get("code") or "UNKNOWN"
            message = (
                error.get("message")
                or "Unknown Shopify error"
            )

            messages.append(
                f"{code}: {message}"
            )

        raise RuntimeError(
            "Shopify did not update inventory: "
            + " | ".join(messages)
        )

    completed_keys = []

    for row in ready_rows:
        listing = database.get(
            __import__(
                "app.database.sales_channels",
                fromlist=["ChannelListing"],
            ).ChannelListing,
            row["listing_id"],
        )

        if listing is not None:
            listing.quantity_available = int(
                row["quantity_to_send"]
            )

        completed_keys.append(
            row["approval_key"]
        )

    database.commit()

    remove_completed_approvals(
        completed_keys
    )

    return {
        "pushed_count": len(ready_rows),
        "location_name": location_name,
        "location_id": location_id,
        "reference_uri": reference_uri,
    }
