from datetime import datetime
from pathlib import Path
import shutil


MAIN_PATH = Path("app/main.py")
SERVICE_PATH = Path(
    "app/services/shopify_inventory_push.py"
)
TEMPLATE_PATH = Path(
    "app/templates/shopify_push_preview.html"
)

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

for path in [
    MAIN_PATH,
    TEMPLATE_PATH,
]:
    backup = path.with_name(
        f"{path.stem}-before-live-shopify-push-"
        f"{stamp}{path.suffix}"
    )

    shutil.copy2(path, backup)
    print("Backup:", backup)


service_code = r'''"""Safely push approved inventory quantities to Shopify."""

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
'''

SERVICE_PATH.write_text(
    service_code,
    encoding="utf-8",
)

print("Created:", SERVICE_PATH)


main_text = MAIN_PATH.read_text(
    encoding="utf-8"
)

old_import = '''from app.services.shopify_push_preview import (
    build_shopify_push_preview,
    load_shopify_push_settings,
    save_shopify_push_settings,
)'''

new_import = '''from app.services.shopify_push_preview import (
    build_shopify_push_preview,
    load_shopify_push_settings,
    save_shopify_push_settings,
)
from app.services.shopify_inventory_push import (
    push_approved_shopify_inventory,
)'''

if old_import not in main_text:
    raise RuntimeError(
        "Could not find the Shopify Push Preview "
        "import block in main.py."
    )

main_text = main_text.replace(
    old_import,
    new_import,
    1,
)


old_signature = '''def shopify_push_preview_page(
    request: Request,
    saved: str = "",
    database: Session = Depends(get_database),
):'''

new_signature = '''def shopify_push_preview_page(
    request: Request,
    saved: str = "",
    pushed: str = "",
    database: Session = Depends(get_database),
):'''

if old_signature not in main_text:
    raise RuntimeError(
        "Could not find the Shopify Push Preview "
        "page function signature."
    )

main_text = main_text.replace(
    old_signature,
    new_signature,
    1,
)


old_message = '''            "message": (
                "Shopify destination saved."
                if saved == "1"
                else None
            ),'''

new_message = '''            "message": (
                "Shopify destination saved."
                if saved == "1"
                else (
                    f"{pushed} approved inventory "
                    "record(s) were pushed to Shopify."
                    if pushed.isdigit()
                    and int(pushed) > 0
                    else None
                )
            ),'''

if old_message not in main_text:
    raise RuntimeError(
        "Could not find the Shopify preview "
        "message block."
    )

main_text = main_text.replace(
    old_message,
    new_message,
    1,
)


route_marker = '''


@app.get(
    "/channels/shopify/approve",
    response_class=HTMLResponse,
)'''

new_route = '''


@app.post(
    "/channels/shopify/push-preview/execute",
)
def execute_shopify_inventory_push(
    request: Request,
    database: Session = Depends(get_database),
):
    try:
        result = push_approved_shopify_inventory(
            database
        )

    except Exception as error:
        database.rollback()

        rows = build_shopify_push_preview(
            database
        )

        ready_count = sum(
            1
            for row in rows
            if row["status"] == "ready"
        )

        stale_count = sum(
            1
            for row in rows
            if row["status"] == "stale"
        )

        summary = {
            "total": len(rows),
            "ready": ready_count,
            "stale": stale_count,
            "blocked": (
                len(rows)
                - ready_count
                - stale_count
            ),
        }

        return templates.TemplateResponse(
            request=request,
            name="shopify_push_preview.html",
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            context={
                "rows": rows,
                "summary": summary,
                "push_settings": (
                    load_shopify_push_settings()
                ),
                "message": None,
                "error": str(error),
            },
        )

    return RedirectResponse(
        url=(
            "/channels/shopify/push-preview"
            f"?pushed={result['pushed_count']}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/channels/shopify/approve",
    response_class=HTMLResponse,
)'''

if route_marker not in main_text:
    raise RuntimeError(
        "Could not locate the insertion point "
        "before the Shopify Approval Queue."
    )

main_text = main_text.replace(
    route_marker,
    new_route,
    1,
)

MAIN_PATH.write_text(
    main_text,
    encoding="utf-8",
)

print("Patched:", MAIN_PATH)


template_text = TEMPLATE_PATH.read_text(
    encoding="utf-8"
)

old_warning = '''        <section class="shopify-setting-warning">
            <strong>Preview only:</strong>

            No API mutation or Shopify inventory update is performed by
            this page.
        </section>'''

new_warning = '''        <section class="shopify-setting-warning">
            <strong>Live Shopify inventory tool:</strong>

            Only records marked Ready will be sent. Shopify must still
            match the quantity shown under Current Shopify or the update
            will be rejected.
        </section>'''

if old_warning not in template_text:
    raise RuntimeError(
        "Could not locate the preview-only warning "
        "in the Shopify template."
    )

template_text = template_text.replace(
    old_warning,
    new_warning,
    1,
)


old_placeholder = '''        {% if summary.ready > 0 %}
        <section class="final-confirmation-placeholder">
            <strong>
                {{ summary.ready }} record(s) passed the preview.
            </strong>

            <p>
                The actual Shopify update button will be added only after
                the destination location and inventory item IDs have been
                confirmed.
            </p>
        </section>
        {% endif %}'''

new_placeholder = '''        {% if summary.ready > 0 %}
        <section class="final-confirmation-placeholder">
            <strong>
                {{ summary.ready }} record(s) passed the preview.
            </strong>

            <p>
                This will set Shopify Available inventory to the Current
                Local quantity for every Ready record.
            </p>

            <form
                action="/channels/shopify/push-preview/execute"
                method="post"
                onsubmit="
                    return confirm(
                        'Push {{ summary.ready }} approved inventory '
                        + 'quantity change(s) to Shopify?'
                    );
                "
            >
                <button
                    type="submit"
                    class="primary-button"
                >
                    Push Approved Quantities to Shopify
                </button>
            </form>
        </section>
        {% endif %}'''

if old_placeholder not in template_text:
    raise RuntimeError(
        "Could not locate the final confirmation "
        "placeholder in the Shopify template."
    )

template_text = template_text.replace(
    old_placeholder,
    new_placeholder,
    1,
)

TEMPLATE_PATH.write_text(
    template_text,
    encoding="utf-8",
)

print("Patched:", TEMPLATE_PATH)
print()
print("Live Shopify push patch completed.")
