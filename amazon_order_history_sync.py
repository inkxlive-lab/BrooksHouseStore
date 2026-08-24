#!/usr/bin/env python
"""
BrooksHouse Amazon order-history sync.

Purpose:
- Pull historical Amazon orders without changing inventory.
- Store order + item history locally for dashboard statistics.
- Include PROCEEDS so sales totals come from Amazon order data.
- Safe to rerun: rows are UPSERTED by Amazon order ID / item ID.

Default lookback: 365 days.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from app.database_resolution import configured_sqlite_path, require_application_database_match
from app.services.marketplace_order_ingestion import (
    begin_sync_run, create_picking_task, ensure_marketplace_operations_schema,
    finish_sync_run, qualify_order, reconcile_order_status, register_order_alert,
    release_cancelled_allocations, terminal_local_status,
)

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
ORDERS_URL = "https://sellingpartnerapi-na.amazon.com/orders/2026-01-01/orders"
_AMAZON_CIRCUIT_LOCK = threading.Lock()
_AMAZON_CIRCUIT = {"failures": 0, "open_until": 0.0}


class AmazonRateLimitError(RuntimeError):
    pass


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
        self._order_cache: dict[str, dict[str, Any]] = {}
        self._last_request_at = 0.0
        self.min_request_interval = max(0.0, float(os.getenv("AMAZON_REQUEST_MIN_INTERVAL_SECONDS", "0.35")))
        self.max_retries = max(0, min(6, int(os.getenv("AMAZON_HTTP_MAX_RETRIES", "3"))))
        self.circuit_failures = max(1, int(os.getenv("AMAZON_CIRCUIT_FAILURES", "3")))
        self.circuit_cooldown = max(30.0, float(os.getenv("AMAZON_CIRCUIT_COOLDOWN_SECONDS", "300")))

    def _request(self, method: str, url: str, **kwargs):
        with _AMAZON_CIRCUIT_LOCK:
            if time.monotonic() < float(_AMAZON_CIRCUIT["open_until"]):
                raise AmazonRateLimitError("Amazon circuit breaker is open; using local freshness policy")
        for attempt in range(self.max_retries + 1):
            delay = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            request_method = getattr(self.session, "request", None)
            response = (request_method(method, url, **kwargs) if request_method
                        else getattr(self.session, method.casefold())(url, **kwargs))
            self._last_request_at = time.monotonic()
            if response.status_code not in {429, 500, 502, 503, 504}:
                with _AMAZON_CIRCUIT_LOCK:
                    _AMAZON_CIRCUIT.update({"failures": 0, "open_until": 0.0})
                return response
            if attempt >= self.max_retries:
                with _AMAZON_CIRCUIT_LOCK:
                    _AMAZON_CIRCUIT["failures"] += 1
                    if _AMAZON_CIRCUIT["failures"] >= self.circuit_failures:
                        _AMAZON_CIRCUIT["open_until"] = time.monotonic() + self.circuit_cooldown
                if response.status_code == 429:
                    raise AmazonRateLimitError(f"Amazon HTTP 429 after {attempt + 1} bounded attempts")
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                wait = max(0.0, float(retry_after)) if retry_after else 0.0
            except (TypeError, ValueError):
                wait = 0.0
            if not wait:
                wait = min(30.0, (2 ** attempt) + random.uniform(0.0, 0.5))
            time.sleep(wait)

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
            "maxResultsPerPage": 100,
            "includedData": "PROCEEDS,FULFILLMENT",
        }

        if pagination_token:
            params["paginationToken"] = pagination_token

        response = self._request(
            "GET", ORDERS_URL,
            params=params,
            headers={
                "Accept": "application/json",
                "x-amz-access-token": str(self.access_token),
            },
            timeout=60,
        )

        if response.status_code == 401:
            self.access_token = None
            self.ensure_token()

            response = self._request(
                "GET", ORDERS_URL,
                params=params,
                headers={
                    "Accept": "application/json",
                    "x-amz-access-token": str(self.access_token),
                },
                timeout=60,
            )

        response.raise_for_status()
        return response.json()

    def get_order(self, order_id: str) -> dict[str, Any]:
        if order_id in self._order_cache:
            return self._order_cache[order_id]
        self.ensure_token()
        response = self._request(
            "GET", f"{ORDERS_URL}/{order_id}",
            params={"includedData": "PROCEEDS,FULFILLMENT"},
            headers={"Accept": "application/json", "x-amz-access-token": str(self.access_token)},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload.get("payload"), dict):
            payload = payload["payload"]
        elif isinstance(payload.get("order"), dict):
            payload = payload["order"]
        self._order_cache[order_id] = payload
        return payload


def extract_orders(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
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


def get_status(order: dict[str, Any]) -> str:
    fulfillment = order.get("fulfillment")

    if isinstance(fulfillment, dict):
        value = fulfillment.get("fulfillmentStatus")
        if value:
            return str(value).strip()

    return str(
        order.get("fulfillmentStatus")
        or order.get("orderStatus")
        or ""
    ).strip()


def get_fulfilled_by(order: dict[str, Any]) -> str:
    fulfillment = order.get("fulfillment")

    if isinstance(fulfillment, dict):
        value = fulfillment.get("fulfilledBy")
        if value:
            return str(value).strip()

    return str(order.get("fulfilledBy") or "").strip()


def get_marketplace(order: dict[str, Any]) -> tuple[str, str]:
    sales_channel = order.get("salesChannel")

    if isinstance(sales_channel, dict):
        return (
            str(sales_channel.get("marketplaceId") or "").strip(),
            str(sales_channel.get("marketplaceName") or "").strip(),
        )

    return "", ""


def money_amount(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")

    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def proceeds_total(proceeds: Any) -> tuple[float | None, str]:
    """Read both direct totals and the Orders v2026 breakdown format."""
    if not isinstance(proceeds, dict):
        return None, ""

    for key in ("grandTotal", "proceedsTotal", "total"):
        value = proceeds.get(key)
        if isinstance(value, dict):
            amount = money_amount(value)
            if amount is not None:
                return amount, str(value.get("currencyCode") or value.get("currency") or "").strip()

    running_total = 0.0
    found = False
    currency = ""
    breakdowns = proceeds.get("breakdowns") or []
    if isinstance(breakdowns, dict):
        breakdowns = [breakdowns]
    for breakdown in breakdowns:
        if not isinstance(breakdown, dict):
            continue
        subtotal = breakdown.get("subtotal")
        if isinstance(subtotal, dict):
            amount = money_amount(subtotal)
            if amount is not None:
                running_total += amount
                found = True
                currency = currency or str(subtotal.get("currencyCode") or subtotal.get("currency") or "").strip()
        elif subtotal not in (None, ""):
            amount = money_amount(subtotal)
            if amount is not None:
                running_total += amount
                found = True
        for detail in breakdown.get("detailedBreakdowns") or []:
            if not isinstance(detail, dict):
                continue
            value = detail.get("value")
            if isinstance(value, dict):
                currency = currency or str(value.get("currencyCode") or value.get("currency") or "").strip()
    return (round(running_total, 2), currency) if found else (None, "")


def get_order_total(order: dict[str, Any]) -> tuple[float | None, str]:
    return proceeds_total(order.get("proceeds"))


def get_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    items = order.get("orderItems")

    if isinstance(items, list):
        return [
            item
            for item in items
            if isinstance(item, dict)
        ]

    if isinstance(items, dict):
        nested = items.get("orderItem")

        if isinstance(nested, list):
            return [
                item
                for item in nested
                if isinstance(item, dict)
            ]

        if isinstance(nested, dict):
            return [nested]

    return []


def item_product(item: dict[str, Any]) -> dict[str, Any]:
    product = item.get("product")
    return product if isinstance(product, dict) else {}


def item_sku(item: dict[str, Any]) -> str:
    product = item_product(item)

    return str(
        product.get("sellerSku")
        or item.get("sellerSku")
        or item.get("sellerSKU")
        or ""
    ).strip()


def item_asin(item: dict[str, Any]) -> str:
    product = item_product(item)

    return str(
        product.get("asin")
        or item.get("asin")
        or ""
    ).strip()


def item_title(item: dict[str, Any]) -> str:
    product = item_product(item)

    return str(
        product.get("title")
        or item.get("title")
        or ""
    ).strip()


def item_quantity(item: dict[str, Any]) -> int:
    try:
        return max(
            int(float(item.get("quantityOrdered") or 0)),
            0,
        )
    except (TypeError, ValueError):
        return 0


def item_total(item: dict[str, Any]) -> tuple[float | None, str]:
    return proceeds_total(item.get("proceeds"))


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS amazon_order_history (
            amazon_order_id TEXT PRIMARY KEY,
            created_time TEXT,
            last_updated_time TEXT,
            fulfillment_status TEXT,
            fulfilled_by TEXT,
            marketplace_id TEXT,
            marketplace_name TEXT,
            order_total REAL,
            currency_code TEXT,
            item_count INTEGER NOT NULL DEFAULT 0,
            unit_count INTEGER NOT NULL DEFAULT 0,
            ship_by_date TEXT,
            raw_json TEXT,
            synced_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS amazon_order_item_history (
            amazon_order_id TEXT NOT NULL,
            order_item_id TEXT NOT NULL,
            seller_sku TEXT,
            asin TEXT,
            title TEXT,
            quantity_ordered INTEGER NOT NULL DEFAULT 0,
            item_total REAL,
            currency_code TEXT,
            product_id INTEGER,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (amazon_order_id, order_item_id)
        );

        CREATE INDEX IF NOT EXISTS
        ix_amazon_order_history_created
        ON amazon_order_history (created_time);

        CREATE INDEX IF NOT EXISTS
        ix_amazon_order_history_status
        ON amazon_order_history (fulfillment_status);

        CREATE INDEX IF NOT EXISTS
        ix_amazon_order_item_history_sku
        ON amazon_order_item_history (seller_sku);
        """
    )
    conn.commit()


def find_product(
    conn: sqlite3.Connection,
    seller_sku: str,
    asin: str,
) -> int | None:
    if seller_sku:
        row = conn.execute(
            """
            SELECT apl.product_id
            FROM amazon_product_links apl
            JOIN amazon_listings al
              ON al.amazon_listing_id = apl.amazon_listing_id
            WHERE TRIM(al.seller_sku) = ?
              AND LOWER(COALESCE(apl.match_status, '')) IN ('linked','matched')
              AND apl.product_id IS NOT NULL
            ORDER BY al.amazon_listing_id
            LIMIT 1
            """,
            (seller_sku,),
        ).fetchone()

        if row is not None:
            return int(row[0])

    if asin:
        row = conn.execute(
            """
            SELECT apl.product_id
            FROM amazon_product_links apl
            JOIN amazon_listings al
              ON al.amazon_listing_id = apl.amazon_listing_id
            WHERE TRIM(al.asin) = ?
              AND LOWER(COALESCE(apl.match_status, '')) IN ('linked','matched')
              AND apl.product_id IS NOT NULL
            ORDER BY al.amazon_listing_id
            LIMIT 1
            """,
            (asin,),
        ).fetchone()

        if row is not None:
            return int(row[0])

    return None


def upsert_order(
    conn: sqlite3.Connection,
    order: dict[str, Any],
) -> tuple[int, int, float]:
    order_id = str(
        order.get("orderId")
        or order.get("amazonOrderId")
        or ""
    ).strip()

    if not order_id:
        return 0, 0, 0.0

    items = get_items(order)

    marketplace_id, marketplace_name = get_marketplace(order)
    order_total, currency_code = get_order_total(order)

    created_time = str(order.get("createdTime") or "").strip()
    last_updated_time = str(order.get("lastUpdatedTime") or "").strip()
    status = get_status(order)
    fulfilled_by = get_fulfilled_by(order)
    timestamp = now_text()
    ship_by_date = str(order.get("latestShipDate") or order.get("earliestShipDate") or order.get("shipByDate") or "").strip()

    unit_count = sum(
        item_quantity(item)
        for item in items
    )

    conn.execute(
        """
        INSERT INTO amazon_order_history (
            amazon_order_id,
            created_time,
            last_updated_time,
            fulfillment_status,
            fulfilled_by,
            marketplace_id,
            marketplace_name,
            order_total,
            currency_code,
            item_count,
            unit_count,
            ship_by_date,
            raw_json,
            synced_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(amazon_order_id)
        DO UPDATE SET
            created_time = excluded.created_time,
            last_updated_time = excluded.last_updated_time,
            fulfillment_status = excluded.fulfillment_status,
            fulfilled_by = excluded.fulfilled_by,
            marketplace_id = excluded.marketplace_id,
            marketplace_name = excluded.marketplace_name,
            order_total = excluded.order_total,
            currency_code = excluded.currency_code,
            item_count = excluded.item_count,
            unit_count = excluded.unit_count,
            ship_by_date = excluded.ship_by_date,
            raw_json = excluded.raw_json,
            synced_at = excluded.synced_at
        """,
        (
            order_id,
            created_time,
            last_updated_time,
            status,
            fulfilled_by,
            marketplace_id,
            marketplace_name,
            order_total,
            currency_code,
            len(items),
            unit_count,
            ship_by_date,
            json.dumps(order, default=str, separators=(",", ":")),
            timestamp,
        ),
    )

    linked_items = 0

    for item in items:
        order_item_id = str(
            item.get("orderItemId")
            or item.get("orderItemID")
            or ""
        ).strip()

        if not order_item_id:
            continue

        sku = item_sku(item)
        asin = item_asin(item)
        title = item_title(item)
        quantity = item_quantity(item)
        total, item_currency = item_total(item)
        product_id = find_product(conn, sku, asin)

        if product_id is not None:
            linked_items += 1

        conn.execute(
            """
            INSERT INTO amazon_order_item_history (
                amazon_order_id,
                order_item_id,
                seller_sku,
                asin,
                title,
                quantity_ordered,
                item_total,
                currency_code,
                product_id,
                synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(amazon_order_id, order_item_id)
            DO UPDATE SET
                seller_sku = excluded.seller_sku,
                asin = excluded.asin,
                title = excluded.title,
                quantity_ordered = excluded.quantity_ordered,
                item_total = excluded.item_total,
                currency_code = excluded.currency_code,
                product_id = excluded.product_id,
                synced_at = excluded.synced_at
            """,
            (
                order_id,
                order_item_id,
                sku,
                asin,
                title,
                quantity,
                total,
                item_currency or currency_code,
                product_id,
                timestamp,
            ),
        )

    return len(items), linked_items, float(order_total or 0.0)


def sync_recent_orders(*, days: int = 3, database: str | Path | None = None,
                       allow_fixture: bool = False, client: AmazonClient | None = None,
                       marketplace_id: str | None = None) -> dict[str, Any]:
    """Import recent merchant orders without changing marketplace or inventory state."""
    load_env(Path(".env"))
    target = (Path(database).expanduser().resolve() if database is not None else configured_sqlite_path())
    if not allow_fixture:
        target = require_application_database_match(target)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    create_tables(connection)
    ensure_marketplace_operations_schema(connection)
    run_id = begin_sync_run(connection, "amazon")
    try:
        marketplace_id = marketplace_id or first_env("AMAZON_MARKETPLACE_ID", "SP_API_MARKETPLACE_ID")
        client = client or AmazonClient(
            first_env("AMAZON_LWA_CLIENT_ID", "SP_API_CLIENT_ID"),
            first_env("AMAZON_LWA_CLIENT_SECRET", "SP_API_CLIENT_SECRET"),
            first_env("AMAZON_REFRESH_TOKEN", "SP_API_REFRESH_TOKEN"),
        )
        if not marketplace_id:
            raise RuntimeError("Amazon marketplace is not configured")
        existing = {str(row[0]) for row in connection.execute(
            "SELECT amazon_order_id FROM amazon_order_history"
        ).fetchall()}
        actionable_ids = [str(row["amazon_order_id"]) for row in connection.execute(
            """SELECT amazon_order_id,local_status,fulfillment_status FROM amazon_order_history
               WHERE lower(COALESCE(local_status,'new')) NOT IN
                 ('shipped','cancelled','refunded','closed','completed')"""
        ).fetchall() if not terminal_local_status(row["fulfillment_status"])]
        token = None
        discovered = new_count = updated_count = line_count = 0
        cancelled_ids: set[str] = set()
        refreshed_ids: set[str] = set()
        while True:
            payload = client.search_orders(
                marketplace_id=marketplace_id,
                created_after=(utc_now() - timedelta(days=max(1, min(30, int(days))))).strftime("%Y-%m-%dT%H:%M:%SZ"),
                pagination_token=token,
            )
            orders, token = extract_orders(payload)
            discovered += len(orders)
            for order in orders:
                order_id = str(order.get("orderId") or order.get("amazonOrderId") or "").strip()
                if not order_id:
                    continue
                refreshed_ids.add(order_id)
                is_new = order_id not in existing
                new_count += int(is_new)
                updated_count += int(not is_new)
                status = get_status(order)
                fulfilled_by = get_fulfilled_by(order)
                items = get_items(order)
                upsert_order(connection, order)
                reconciliation = reconcile_order_status(
                    connection, channel="amazon", order_id=order_id,
                    marketplace_status=status, channel_response=order, sync_run_id=run_id,
                )
                if reconciliation.get("local_status") in {"cancelled", "refunded"}:
                    cancelled_ids.add(order_id)
                line_count += len(items)
                if qualify_order("amazon", status, fulfilled_by) and not terminal_local_status(status):
                    for item in items:
                        line_id = str(item.get("orderItemId") or item.get("orderItemID") or "").strip()
                        if line_id:
                            create_picking_task(
                                connection, channel="amazon", order_id=order_id, line_id=line_id,
                                sku=item_sku(item), quantity=item_quantity(item), product_id=None,
                            )
                    if is_new:
                        register_order_alert(connection, "amazon", order_id, status)
            connection.commit()
            if not token:
                break
        # The created-after import cannot see old orders. Verify every local order
        # still considered actionable using the single-order endpoint.
        for order_id in (value for value in actionable_ids if value not in refreshed_ids) if hasattr(client, "get_order") else []:
            current = client.get_order(order_id)
            if not isinstance(current, dict):
                continue
            upsert_order(connection, current)
            status = get_status(current)
            reconciliation = reconcile_order_status(
                connection, channel="amazon", order_id=order_id,
                marketplace_status=status, channel_response=current, sync_run_id=run_id,
            )
            if reconciliation.get("local_status") in {"cancelled", "refunded"}:
                cancelled_ids.add(order_id)
            updated_count += 1
        connection.commit()
        for cancelled_id in sorted(cancelled_ids):
            release_cancelled_allocations(target, "amazon", cancelled_id)
        finish_sync_run(connection, run_id, success=True, orders_discovered=discovered,
                        new_orders_inserted=new_count, orders_updated=updated_count,
                        lines_processed=line_count)
        return {"success": True, "orders_discovered": discovered,
                "new_orders_inserted": new_count, "orders_updated": updated_count,
                "lines_processed": line_count}
    except Exception as error:
        connection.rollback()
        finish_sync_run(connection, run_id, success=False,
                        error_message=f"{type(error).__name__}: {error}")
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        default=str(configured_sqlite_path()),
    )
    parser.add_argument(
        "--env",
        default=".env",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
    )
    args = parser.parse_args()

    if not 1 <= args.days <= 3650:
        raise RuntimeError(
            "--days must be between 1 and 3650."
        )

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
            "Missing Amazon .env setting(s): "
            + ", ".join(missing)
        )

    conn = sqlite3.connect(
        args.database,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    create_tables(conn)

    created_after = (
        utc_now() - timedelta(days=args.days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = AmazonClient(
        client_id,
        client_secret,
        refresh_token,
    )

    print("Amazon Order History Sync")
    print("-------------------------")
    print(f"Marketplace: {marketplace_id}")
    print(f"Lookback:    {args.days} days")
    print(f"Created after: {created_after}")
    print()

    total_orders = 0
    total_items = 0
    linked_items = 0
    total_sales = 0.0
    pages = 0
    next_token = None

    while True:
        pages += 1

        payload = client.search_orders(
            marketplace_id=marketplace_id,
            created_after=created_after,
            pagination_token=next_token,
        )

        orders, next_token = extract_orders(payload)

        print(
            f"Page {pages}: {len(orders)} order(s)"
        )

        for order in orders:
            item_count, linked_count, sales = upsert_order(
                conn,
                order,
            )

            total_orders += 1
            total_items += item_count
            linked_items += linked_count
            total_sales += sales

        conn.commit()

        if not next_token:
            break

        if pages >= 100:
            print(
                "Stopped at 100 pages for safety. "
                "Rerun with a shorter --days range if needed."
            )
            break

    status_rows = conn.execute(
        """
        SELECT
            COALESCE(fulfillment_status, '(blank)') AS status,
            COUNT(*) AS orders,
            COALESCE(SUM(unit_count), 0) AS units,
            COALESCE(SUM(order_total), 0) AS sales
        FROM amazon_order_history
        GROUP BY COALESCE(fulfillment_status, '(blank)')
        ORDER BY orders DESC
        """
    ).fetchall()

    print()
    print("Sync summary")
    print("------------")
    print(f"Orders received this run: {total_orders}")
    print(f"Order items this run:     {total_items}")
    print(f"Linked item lines:        {linked_items}")
    print(f"Pages:                    {pages}")
    print(f"Order value this run:     ${total_sales:,.2f}")
    print()

    print("Local Amazon history by status")
    print("------------------------------")

    for row in status_rows:
        print(
            f"{row['status']:<22} "
            f"orders {row['orders']:>5} | "
            f"units {row['units']:>6} | "
            f"${float(row['sales'] or 0):>12,.2f}"
        )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
