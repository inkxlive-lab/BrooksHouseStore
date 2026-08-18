#!/usr/bin/env python
"""
BrooksHouse Amazon SP-API Open Orders -> Online Orders / Reserved sync.

Default mode is PREVIEW.
Use --apply to actually add quantities to Location 5.

Uses Amazon Orders API v2026-01-01 searchOrders.
Only merchant-fulfilled UNSHIPPED and PARTIALLY_SHIPPED orders are included.

Duplicate protection:
Amazon orderId + orderItemId is stored in amazon_order_inventory_sync.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
ORDERS_URL = "https://sellingpartnerapi-na.amazon.com/orders/2026-01-01/orders"

TARGET_LOCATION_ID = 5
TARGET_LOCATION_NAME = "Online Orders / Reserved"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AmazonClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.session = requests.Session()
        self.access_token: str | None = None
        self.expires_at = utc_now()

    def authenticate(self) -> None:
        response = self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        lifetime = int(payload.get("expires_in") or 3600)
        self.expires_at = utc_now() + timedelta(
            seconds=max(60, lifetime - 60)
        )

    def ensure_token(self) -> None:
        if not self.access_token or utc_now() >= self.expires_at:
            self.authenticate()

    def search_orders(
        self,
        *,
        marketplace_id: str,
        created_after: str,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_token()

        params: dict[str, Any] = {
            "marketplaceIds": marketplace_id,
            "createdAfter": created_after,
            "fulfillmentStatuses": "UNSHIPPED,PARTIALLY_SHIPPED",
            "fulfilledBy": "MERCHANT",
            "maxResultsPerPage": 100,
        }

        if pagination_token:
            params["paginationToken"] = pagination_token

        response = self.session.get(
            ORDERS_URL,
            params=params,
            headers={
                "Accept": "application/json",
                "x-amz-access-token": str(self.access_token),
            },
            timeout=45,
        )

        if response.status_code == 401:
            self.access_token = None
            self.ensure_token()
            response = self.session.get(
                ORDERS_URL,
                params=params,
                headers={
                    "Accept": "application/json",
                    "x-amz-access-token": str(self.access_token),
                },
                timeout=45,
            )

        response.raise_for_status()
        return response.json()


def create_sync_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS amazon_order_inventory_sync (
            sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
            amazon_order_id TEXT NOT NULL,
            order_item_id TEXT NOT NULL,
            seller_sku TEXT,
            asin TEXT,
            product_id INTEGER,
            quantity_added INTEGER NOT NULL DEFAULT 0,
            target_location_id INTEGER NOT NULL,
            fulfillment_status TEXT,
            processed_at TEXT NOT NULL,
            UNIQUE (amazon_order_id, order_item_id)
        )
        """
    )
    conn.commit()


def validate_target_location(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT location_name
        FROM inventory_locations
        WHERE location_id = ?
        """,
        (TARGET_LOCATION_ID,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Location 5 does not exist.")

    actual = str(row["location_name"] or "").strip()
    if actual.casefold() != TARGET_LOCATION_NAME.casefold():
        raise RuntimeError(
            f"Location 5 is named {actual!r}, not {TARGET_LOCATION_NAME!r}. "
            "Nothing was changed."
        )


def extract_orders(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    # v2026-01-01 usually returns orders plus nextToken.
    orders = payload.get("orders")
    if not isinstance(orders, list):
        inner = payload.get("payload")
        if isinstance(inner, dict):
            orders = inner.get("orders")
        else:
            orders = []

    next_token = payload.get("nextToken")
    if not next_token and isinstance(payload.get("pagination"), dict):
        next_token = payload["pagination"].get("nextToken")
    if not next_token and isinstance(payload.get("payload"), dict):
        next_token = payload["payload"].get("nextToken")

    return (
        [row for row in orders if isinstance(row, dict)],
        str(next_token) if next_token else None,
    )


def order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    items = order.get("orderItems")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if isinstance(items, dict):
        nested = items.get("orderItem")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            return [nested]
    return []


def item_sku(item: dict[str, Any]) -> str:
    return str(
        item.get("sellerSku")
        or item.get("sellerSKU")
        or item.get("sku")
        or ""
    ).strip()


def item_asin(item: dict[str, Any]) -> str:
    product = item.get("product")
    if isinstance(product, dict):
        asin = product.get("asin")
        if asin:
            return str(asin).strip()
    return str(item.get("asin") or "").strip()


def item_quantity(item: dict[str, Any]) -> int:
    candidates = (
        item.get("quantityOrdered"),
        item.get("quantity"),
        item.get("quantityRemaining"),
    )
    for value in candidates:
        try:
            if value is not None:
                return max(int(float(value)), 0)
        except (TypeError, ValueError):
            pass
    return 0


def find_product(
    conn: sqlite3.Connection,
    seller_sku: str,
    asin: str,
):
    if seller_sku:
        row = conn.execute(
            """
            SELECT
                apl.product_id,
                p.product_name
            FROM amazon_product_links AS apl
            JOIN amazon_listings AS al
              ON al.amazon_listing_id = apl.amazon_listing_id
            JOIN products AS p
              ON p.product_id = apl.product_id
            WHERE TRIM(al.seller_sku) = ?
              AND LOWER(COALESCE(apl.match_status, '')) = 'linked'
            ORDER BY al.amazon_listing_id
            LIMIT 1
            """,
            (seller_sku,),
        ).fetchone()
        if row is not None:
            return row

    if asin:
        row = conn.execute(
            """
            SELECT
                apl.product_id,
                p.product_name
            FROM amazon_product_links AS apl
            JOIN amazon_listings AS al
              ON al.amazon_listing_id = apl.amazon_listing_id
            JOIN products AS p
              ON p.product_id = apl.product_id
            WHERE TRIM(al.asin) = ?
              AND LOWER(COALESCE(apl.match_status, '')) = 'linked'
            ORDER BY al.amazon_listing_id
            LIMIT 1
            """,
            (asin,),
        ).fetchone()
        if row is not None:
            return row

    return None


def already_processed(
    conn: sqlite3.Connection,
    amazon_order_id: str,
    order_item_id: str,
) -> bool:
    return conn.execute(
        """
        SELECT 1
        FROM amazon_order_inventory_sync
        WHERE amazon_order_id = ?
          AND order_item_id = ?
        LIMIT 1
        """,
        (amazon_order_id, order_item_id),
    ).fetchone() is not None


def apply_line(
    conn: sqlite3.Connection,
    *,
    amazon_order_id: str,
    order_item_id: str,
    seller_sku: str,
    asin: str,
    product_id: int,
    quantity: int,
    status: str,
) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT inventory_id, quantity_on_hand
        FROM inventory
        WHERE product_id = ?
          AND location_id = ?
        LIMIT 1
        """,
        (product_id, TARGET_LOCATION_ID),
    ).fetchone()

    previous = int(row["quantity_on_hand"] or 0) if row else 0
    new_quantity = previous + quantity
    timestamp = now_text()

    if row:
        conn.execute(
            """
            UPDATE inventory
            SET quantity_on_hand = ?,
                updated_at = ?
            WHERE inventory_id = ?
            """,
            (new_quantity, timestamp, row["inventory_id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO inventory (
                product_id,
                location_id,
                quantity_on_hand,
                quantity_reserved,
                reorder_level,
                updated_at
            )
            VALUES (?, ?, ?, 0, 0, ?)
            """,
            (product_id, TARGET_LOCATION_ID, quantity, timestamp),
        )

    reference = f"AMAZON-{amazon_order_id}-{order_item_id}"[:100]

    notes = (
        "Open Amazon order added to Online Orders / Reserved. "
        f"Amazon order: {amazon_order_id}. "
        f"Order item: {order_item_id}. "
        f"Seller SKU: {seller_sku}. ASIN: {asin}. "
        f"Status: {status}. Quantity added: {quantity}. "
        f"Previous location quantity: {previous}. "
        f"New location quantity: {new_quantity}."
    )

    conn.execute(
        """
        INSERT INTO inventory_transactions (
            product_id,
            location_id,
            transaction_type,
            quantity_change,
            unit_cost,
            reference_number,
            notes,
            created_at
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            product_id,
            TARGET_LOCATION_ID,
            "amazon_open_order_add",
            quantity,
            reference,
            notes,
            timestamp,
        ),
    )

    conn.execute(
        """
        INSERT INTO amazon_order_inventory_sync (
            amazon_order_id,
            order_item_id,
            seller_sku,
            asin,
            product_id,
            quantity_added,
            target_location_id,
            fulfillment_status,
            processed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            amazon_order_id,
            order_item_id,
            seller_sku,
            asin,
            product_id,
            quantity,
            TARGET_LOCATION_ID,
            status,
            timestamp,
        ),
    )

    return previous, new_quantity


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="app/data/brookshouse_store.db")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.days <= 180:
        raise RuntimeError("--days must be between 1 and 180.")

    load_env(Path(args.env))

    client_id = first_env(
        "AMAZON_LWA_CLIENT_ID",
        "SP_API_CLIENT_ID",
        "LWA_CLIENT_ID",
    )
    client_secret = first_env(
        "AMAZON_LWA_CLIENT_SECRET",
        "SP_API_CLIENT_SECRET",
        "LWA_CLIENT_SECRET",
    )
    refresh_token = first_env(
        "AMAZON_REFRESH_TOKEN",
        "SP_API_REFRESH_TOKEN",
        "LWA_REFRESH_TOKEN",
    )
    marketplace_id = first_env(
        "AMAZON_MARKETPLACE_ID",
        "SP_API_MARKETPLACE_ID",
    )

    missing = []
    if not client_id:
        missing.append("AMAZON_LWA_CLIENT_ID")
    if not client_secret:
        missing.append("AMAZON_LWA_CLIENT_SECRET")
    if not refresh_token:
        missing.append("AMAZON_REFRESH_TOKEN")
    if not marketplace_id:
        missing.append("AMAZON_MARKETPLACE_ID")

    if missing:
        raise RuntimeError(
            "Missing Amazon .env setting(s): " + ", ".join(missing)
        )

    conn = sqlite3.connect(args.database, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    create_sync_table(conn)
    validate_target_location(conn)

    created_after = (
        utc_now() - timedelta(days=args.days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = AmazonClient(
        client_id,
        client_secret,
        refresh_token,
    )

    print(f"Target: Location 5 - {TARGET_LOCATION_NAME}")
    print("Mode:", "APPLY" if args.apply else "PREVIEW")
    print(f"Marketplace: {marketplace_id}")
    print(f"Created after: {created_after}")
    print()

    all_orders: list[dict[str, Any]] = []
    token = None
    page = 0

    while True:
        page += 1
        payload = client.search_orders(
            marketplace_id=marketplace_id,
            created_after=created_after,
            pagination_token=token,
        )
        orders, token = extract_orders(payload)
        all_orders.extend(orders)
        print(f"Amazon page {page}: {len(orders)} order(s)")
        if not token or page >= 20:
            break

    open_orders = len(all_orders)
    order_lines = matched = duplicates = units = 0
    unmatched = []

    for order in all_orders:
        order_id = str(
            order.get("orderId")
            or order.get("amazonOrderId")
            or ""
        ).strip()

        status = str(
            order.get("fulfillmentStatus")
            or order.get("orderStatus")
            or ""
        ).strip()

        items = order_items(order)

        if not items:
            unmatched.append(
                (
                    order_id,
                    "",
                    "",
                    "",
                    "Amazon returned order without orderItems",
                )
            )
            continue

        for item in items:
            order_lines += 1

            order_item_id = str(
                item.get("orderItemId")
                or item.get("orderItemID")
                or ""
            ).strip()

            sku = item_sku(item)
            asin = item_asin(item)
            quantity = item_quantity(item)

            if quantity <= 0:
                continue

            if not order_id or not order_item_id:
                unmatched.append(
                    (
                        order_id,
                        order_item_id,
                        sku,
                        asin,
                        "missing Amazon order/item ID",
                    )
                )
                continue

            if already_processed(conn, order_id, order_item_id):
                duplicates += 1
                print(
                    f"SKIP already processed | "
                    f"{order_id} | {order_item_id} | {sku}"
                )
                continue

            product = find_product(conn, sku, asin)

            if product is None:
                unmatched.append(
                    (
                        order_id,
                        order_item_id,
                        sku,
                        asin,
                        "no BrooksHouse Amazon link",
                    )
                )
                print(
                    f"UNMATCHED | {order_id} | "
                    f"item {order_item_id} | SKU {sku} | ASIN {asin}"
                )
                continue

            matched += 1
            units += quantity

            if not args.apply:
                print(
                    f"PREVIEW +{quantity} | {product['product_name']} | "
                    f"product {product['product_id']} | "
                    f"Amazon {order_id} | item {order_item_id}"
                )
                continue

            previous, new_quantity = apply_line(
                conn,
                amazon_order_id=order_id,
                order_item_id=order_item_id,
                seller_sku=sku,
                asin=asin,
                product_id=int(product["product_id"]),
                quantity=quantity,
                status=status,
            )
            conn.commit()

            print(
                f"ADDED +{quantity} | {product['product_name']} | "
                f"Location 5: {previous} -> {new_quantity} | "
                f"Amazon {order_id}"
            )

    print()
    print("Amazon open-order sync summary")
    print("------------------------------")
    print(f"Open orders returned: {open_orders}")
    print(f"Order lines:          {order_lines}")
    print(f"Matched new lines:    {matched}")
    print(f"Already processed:    {duplicates}")
    print(f"Units to Location 5:  {units}")
    print(f"Unmatched:            {len(unmatched)}")

    if not args.apply:
        print()
        print("PREVIEW ONLY - no inventory quantities were changed.")
        print("If the matches look right, rerun with --apply.")

    if unmatched:
        print()
        print("Unmatched Amazon order lines")
        for order_id, item_id, sku, asin, reason in unmatched:
            print(
                f"{order_id} | item {item_id} | "
                f"SKU {sku} | ASIN {asin} | {reason}"
            )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
