from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlencode

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from app.database_resolution import configured_sqlite_path


APP_DIR = Path(__file__).resolve().parent
DB_PATH = configured_sqlite_path()
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")
STOREFRONT_LOCATION_NAME = "BrooksHouse Storefront"
_SHOPIFY_TOKEN_CACHE: dict[str, Any] = {
    "shop": "",
    "token": "",
    "expires_at": 0.0,
}


def _load_env_file() -> None:
    """Load BrooksHouse's root .env even when Task Scheduler starts elsewhere."""
    for env_path in (APP_DIR.parent / ".env", APP_DIR / ".env"):
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if name:
                os.environ.setdefault(name, value)
        return


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_shopify_operations_tables() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS shopify_sales_orders (
                shopify_order_id TEXT PRIMARY KEY,
                order_name TEXT,
                order_number TEXT,
                processed_at TEXT,
                updated_at TEXT,
                cancelled_at TEXT,
                source_name TEXT,
                pos_location_id TEXT,
                financial_status TEXT,
                fulfillment_status TEXT,
                currency TEXT,
                subtotal_amount REAL NOT NULL DEFAULT 0,
                discount_amount REAL NOT NULL DEFAULT 0,
                tax_amount REAL NOT NULL DEFAULT 0,
                total_amount REAL NOT NULL DEFAULT 0,
                refund_amount REAL NOT NULL DEFAULT 0,
                test_order INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                first_imported_at TEXT NOT NULL,
                last_imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shopify_sales_lines (
                shopify_line_id TEXT PRIMARY KEY,
                shopify_order_id TEXT NOT NULL,
                shopify_product_id TEXT,
                shopify_variant_id TEXT,
                product_id INTEGER,
                sku TEXT,
                barcode TEXT,
                title TEXT,
                variant_title TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                current_quantity INTEGER NOT NULL DEFAULT 0,
                unit_price REAL NOT NULL DEFAULT 0,
                discount_amount REAL NOT NULL DEFAULT 0,
                net_amount REAL NOT NULL DEFAULT 0,
                match_status TEXT NOT NULL DEFAULT 'unmatched',
                match_method TEXT,
                inventory_applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(shopify_order_id)
                    REFERENCES shopify_sales_orders(shopify_order_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shopify_payment_transactions (
                shopify_transaction_id TEXT PRIMARY KEY,
                shopify_order_id TEXT NOT NULL,
                kind TEXT,
                status TEXT,
                gateway TEXT,
                payment_id TEXT,
                amount REAL NOT NULL DEFAULT 0,
                currency TEXT,
                processed_at TEXT,
                test_transaction INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                first_imported_at TEXT NOT NULL,
                last_imported_at TEXT NOT NULL,
                FOREIGN KEY(shopify_order_id)
                    REFERENCES shopify_sales_orders(shopify_order_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS operations_work_queue (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL UNIQUE,
                task_type TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'open',
                source_channel TEXT,
                source_reference TEXT,
                product_id INTEGER,
                location_id INTEGER,
                requested_quantity INTEGER,
                assigned_user_id INTEGER,
                assigned_to_name TEXT,
                due_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                completed_by TEXT,
                resolution_notes TEXT
            );

            CREATE TABLE IF NOT EXISTS product_pick_slots (
                product_id INTEGER PRIMARY KEY,
                location_id INTEGER NOT NULL,
                container_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(product_id),
                FOREIGN KEY(location_id) REFERENCES inventory_locations(location_id)
            );

            CREATE TABLE IF NOT EXISTS shopify_sales_sync_log (
                sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                requested_days INTEGER NOT NULL,
                orders_received INTEGER NOT NULL DEFAULT 0,
                orders_saved INTEGER NOT NULL DEFAULT 0,
                lines_saved INTEGER NOT NULL DEFAULT 0,
                transactions_saved INTEGER NOT NULL DEFAULT 0,
                tasks_created INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                message TEXT
            );

            CREATE INDEX IF NOT EXISTS ix_shopify_sales_orders_processed
                ON shopify_sales_orders(processed_at);
            CREATE INDEX IF NOT EXISTS ix_shopify_sales_lines_product
                ON shopify_sales_lines(product_id);
            CREATE INDEX IF NOT EXISTS ix_shopify_sales_lines_match
                ON shopify_sales_lines(match_status);
            CREATE INDEX IF NOT EXISTS ix_shopify_transactions_processed
                ON shopify_payment_transactions(processed_at);
            CREATE INDEX IF NOT EXISTS ix_operations_queue_status_priority
                ON operations_work_queue(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS ix_operations_queue_product
                ON operations_work_queue(product_id);
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(operations_work_queue)")
        }
        for column_name, column_type in (
            ("source_location_id", "INTEGER"),
            ("source_container_id", "TEXT"),
            ("destination_location_id", "INTEGER"),
            ("destination_container_id", "TEXT"),
        ):
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE operations_work_queue ADD COLUMN {column_name} {column_type}"
                )


def _env_first(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _client_credentials_token(shop: str, client_id: str, client_secret: str) -> str:
    now = time.time()
    if (
        _SHOPIFY_TOKEN_CACHE["shop"] == shop
        and _SHOPIFY_TOKEN_CACHE["token"]
        and float(_SHOPIFY_TOKEN_CACHE["expires_at"]) > now + 60
    ):
        return str(_SHOPIFY_TOKEN_CACHE["token"])

    request = UrlRequest(
        f"https://{shop}/admin/oauth/access_token",
        data=urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify rejected the client credentials (HTTP {error.code}): {detail[:500]}"
        ) from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to Shopify for authentication: {error.reason}") from error

    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Shopify authentication succeeded but returned no access token.")
    try:
        expires_in = int(payload.get("expires_in") or 86400)
    except (TypeError, ValueError):
        expires_in = 86400
    _SHOPIFY_TOKEN_CACHE.update({
        "shop": shop,
        "token": token,
        "expires_at": now + max(60, expires_in - 300),
    })
    return token


def _shopify_settings() -> tuple[str, str, str]:
    _load_env_file()
    shop = _env_first(
        "SHOPIFY_SHOP_DOMAIN", "SHOPIFY_STORE_DOMAIN",
        "SHOPIFY_STORE", "SHOPIFY_SHOP",
    )
    token = _env_first(
        "SHOPIFY_ADMIN_ACCESS_TOKEN", "SHOPIFY_ACCESS_TOKEN",
        "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_TOKEN",
    )
    version = _env_first("SHOPIFY_API_VERSION") or "2026-07"
    shop = shop.removeprefix("https://").removeprefix("http://").rstrip("/")
    if shop and "." not in shop:
        shop = f"{shop}.myshopify.com"
    if not shop:
        raise RuntimeError(
            "Shopify store was not found. Set SHOPIFY_STORE in C:\\BrooksHouseStore\\.env."
        )
    if not token:
        client_id = _env_first("SHOPIFY_CLIENT_ID")
        client_secret = _env_first("SHOPIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "Shopify credentials were not found. Set SHOPIFY_STORE, "
                "SHOPIFY_CLIENT_ID, and SHOPIFY_CLIENT_SECRET in "
                "C:\\BrooksHouseStore\\.env."
            )
        token = _client_credentials_token(shop, client_id, client_secret)
    return shop, token, version


ORDER_QUERY = """
query BrooksHouseOrders($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, query: $query,
         sortKey: PROCESSED_AT, reverse: false) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name processedAt updatedAt cancelledAt sourceName test
      displayFinancialStatus displayFulfillmentStatus
      currencyCode
      subtotalPriceSet { shopMoney { amount currencyCode } }
      totalDiscountsSet { shopMoney { amount currencyCode } }
      totalTaxSet { shopMoney { amount currencyCode } }
      totalPriceSet { shopMoney { amount currencyCode } }
      totalRefundedSet { shopMoney { amount currencyCode } }
      physicalLocation { id name }
      lineItems(first: 100) {
        nodes {
          id title variantTitle quantity currentQuantity sku
          originalUnitPriceSet { shopMoney { amount currencyCode } }
          totalDiscountSet { shopMoney { amount currencyCode } }
          discountedTotalSet { shopMoney { amount currencyCode } }
          product { id }
          variant { id barcode sku }
        }
      }
      transactions {
        id kind status gateway paymentId processedAt test
        amountSet { shopMoney { amount currencyCode } }
      }
    }
  }
}
"""


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    shop, token, version = _shopify_settings()
    request = UrlRequest(
        f"https://{shop}/admin/api/{version}/graphql.json",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Shopify returned HTTP {error.code}: {detail[:500]}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to Shopify: {error.reason}") from error
    if payload.get("errors"):
        messages = "; ".join(str(item.get("message") or item) for item in payload["errors"])
        if "access" in messages.casefold() or "scope" in messages.casefold():
            messages += " Confirm the Shopify app has read_orders; history older than 60 days also needs read_all_orders."
        raise RuntimeError(messages)
    return payload.get("data") or {}


def _money(node: dict[str, Any] | None) -> tuple[float, str]:
    money = ((node or {}).get("shopMoney") or {})
    try:
        amount = float(money.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return amount, str(money.get("currencyCode") or "")


def _normalize_barcode(value: Any) -> str:
    return "".join(char for char in str(value or "").strip() if char.isdigit())


def _line_identifiers(line: dict[str, Any]) -> tuple[str, str]:
    """Use one source of truth for both matching and stored future identifiers."""
    variant = line.get("variant") or {}
    sku = str(variant.get("sku") or line.get("sku") or "").strip()
    barcode = _normalize_barcode(variant.get("barcode") or line.get("barcode"))
    return sku, barcode


def _match_product(connection: sqlite3.Connection, line: dict[str, Any]) -> tuple[int | None, str, str, str]:
    variant = line.get("variant") or {}
    variant_id = str(variant.get("id") or "").strip()
    sku, barcode = _line_identifiers(line)

    listing = None
    if variant_id:
        listing = connection.execute(
            """
            SELECT barcode_exact, barcode_lookup, sku
            FROM channel_listings cl
            JOIN sales_channels sc ON sc.channel_id=cl.channel_id
            WHERE lower(sc.channel_name)='shopify'
              AND cl.external_variant_id=?
            LIMIT 1
            """, (variant_id,),
        ).fetchone()
    if listing:
        barcode = barcode or _normalize_barcode(listing["barcode_exact"] or listing["barcode_lookup"])
        sku = sku or str(listing["sku"] or "").strip()

    if barcode:
        candidates = connection.execute(
            """
            SELECT product_id, barcode FROM product_barcodes
            WHERE barcode=? OR ltrim(barcode,'0')=ltrim(?,'0')
            """, (barcode, barcode),
        ).fetchall()
        product_ids = sorted({int(row["product_id"]) for row in candidates})
        if len(product_ids) == 1:
            return product_ids[0], "matched", "barcode", barcode
        if len(product_ids) > 1:
            return None, "ambiguous", "duplicate_barcode", barcode

    if sku:
        candidates = connection.execute(
            """
            SELECT DISTINCT pb.product_id
            FROM channel_listings cl
            JOIN sales_channels sc ON sc.channel_id=cl.channel_id
            JOIN product_barcodes pb
              ON ltrim(pb.barcode,'0')=ltrim(cl.barcode_exact,'0')
            WHERE lower(sc.channel_name)='shopify' AND cl.sku=?
            """, (sku,),
        ).fetchall()
        product_ids = sorted({int(row["product_id"]) for row in candidates})
        if len(product_ids) == 1:
            return product_ids[0], "matched", "sku", barcode
        if len(product_ids) > 1:
            return None, "ambiguous", "duplicate_sku", barcode
    return None, "unmatched", "", barcode


def _task(connection: sqlite3.Connection, key: str, task_type: str, title: str,
          details: str, priority: str, source_reference: str = "",
          product_id: int | None = None, location_id: int | None = None,
          requested_quantity: int | None = None, source_channel: str = "shopify",
          source_location_id: int | None = None, source_container_id: str = "",
          destination_location_id: int | None = None,
          destination_container_id: str = "") -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor = connection.execute(
        """
        INSERT INTO operations_work_queue (
            task_key, task_type, title, details, priority, status,
            source_channel, source_reference, product_id, location_id,
            requested_quantity, created_at, updated_at, source_location_id,
            source_container_id, destination_location_id, destination_container_id
        ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_key) DO UPDATE SET
            task_type=excluded.task_type,
            title=excluded.title,
            details=excluded.details,
            source_channel=excluded.source_channel,
            source_reference=excluded.source_reference,
            product_id=excluded.product_id,
            location_id=excluded.location_id,
            requested_quantity=excluded.requested_quantity,
            source_location_id=excluded.source_location_id,
            source_container_id=excluded.source_container_id,
            destination_location_id=excluded.destination_location_id,
            destination_container_id=excluded.destination_container_id,
            status=CASE
                WHEN operations_work_queue.status IN ('completed','cancelled')
                     AND excluded.task_type IN ('directed_replenishment','placement_needed')
                THEN 'open' ELSE operations_work_queue.status END,
            completed_at=CASE
                WHEN operations_work_queue.status IN ('completed','cancelled')
                     AND excluded.task_type IN ('directed_replenishment','placement_needed')
                THEN NULL ELSE operations_work_queue.completed_at END,
            priority=CASE WHEN operations_work_queue.status='completed'
                          THEN operations_work_queue.priority ELSE excluded.priority END,
            updated_at=excluded.updated_at
        """,
        (key, task_type, title, details, priority, source_channel, source_reference,
         product_id, location_id, requested_quantity, now, now,
         source_location_id, source_container_id, destination_location_id,
         destination_container_id),
    )
    return 1 if cursor.rowcount else 0


def _save_orders(orders: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"orders": 0, "lines": 0, "transactions": 0, "tasks": 0}
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        for order in orders:
            order_id = str(order.get("id") or "").strip()
            if not order_id:
                continue
            subtotal, currency = _money(order.get("subtotalPriceSet"))
            discounts, _ = _money(order.get("totalDiscountsSet"))
            tax, _ = _money(order.get("totalTaxSet"))
            total, total_currency = _money(order.get("totalPriceSet"))
            refunded, _ = _money(order.get("totalRefundedSet"))
            physical = order.get("physicalLocation") or {}
            connection.execute(
                """
                INSERT INTO shopify_sales_orders VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                ON CONFLICT(shopify_order_id) DO UPDATE SET
                    order_name=excluded.order_name, processed_at=excluded.processed_at,
                    updated_at=excluded.updated_at, cancelled_at=excluded.cancelled_at,
                    source_name=excluded.source_name, pos_location_id=excluded.pos_location_id,
                    financial_status=excluded.financial_status,
                    fulfillment_status=excluded.fulfillment_status,
                    currency=excluded.currency, subtotal_amount=excluded.subtotal_amount,
                    discount_amount=excluded.discount_amount, tax_amount=excluded.tax_amount,
                    total_amount=excluded.total_amount, refund_amount=excluded.refund_amount,
                    test_order=excluded.test_order, raw_json=excluded.raw_json,
                    last_imported_at=excluded.last_imported_at
                """,
                (order_id, order.get("name"), order.get("name"), order.get("processedAt"),
                 order.get("updatedAt"), order.get("cancelledAt"), order.get("sourceName"),
                 physical.get("id"), order.get("displayFinancialStatus"),
                 order.get("displayFulfillmentStatus"), total_currency or currency,
                 subtotal, discounts, tax, total, refunded, int(bool(order.get("test"))),
                 json.dumps(order, separators=(",", ":")), now, now),
            )
            counts["orders"] += 1

            for line in ((order.get("lineItems") or {}).get("nodes") or []):
                line_id = str(line.get("id") or "").strip()
                if not line_id:
                    continue
                product_id, match_status, match_method, barcode = _match_product(connection, line)
                price, _ = _money(line.get("originalUnitPriceSet"))
                discount, _ = _money(line.get("totalDiscountSet"))
                net, _ = _money(line.get("discountedTotalSet"))
                variant = line.get("variant") or {}
                product = line.get("product") or {}
                stored_sku, stored_barcode = _line_identifiers(line)
                connection.execute(
                    """
                    INSERT INTO shopify_sales_lines VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    ON CONFLICT(shopify_line_id) DO UPDATE SET
                        product_id=excluded.product_id, sku=excluded.sku,
                        barcode=excluded.barcode, title=excluded.title,
                        variant_title=excluded.variant_title,
                        quantity=excluded.quantity, current_quantity=excluded.current_quantity,
                        unit_price=excluded.unit_price, discount_amount=excluded.discount_amount,
                        net_amount=excluded.net_amount, match_status=excluded.match_status,
                        match_method=excluded.match_method, updated_at=excluded.updated_at
                    """,
                    (line_id, order_id, product.get("id"), variant.get("id"), product_id,
                     stored_sku, stored_barcode or barcode, line.get("title"),
                     line.get("variantTitle"), int(line.get("quantity") or 0),
                     int(line.get("currentQuantity") or 0), price, discount, net,
                     match_status, match_method, 0, now, now),
                )
                counts["lines"] += 1
                if match_status != "matched":
                    counts["tasks"] += _task(
                        connection, f"shopify-line-match:{line_id}", "product_match",
                        f"Match Shopify sale item: {line.get('title') or 'Unknown item'}",
                        f"Order {order.get('name') or order_id}; barcode {barcode or 'missing'}; "
                        f"SKU {variant.get('sku') or line.get('sku') or 'missing'}.",
                        "high" if match_status == "ambiguous" else "normal",
                        order_id,
                    )

            for transaction in order.get("transactions") or []:
                transaction_id = str(transaction.get("id") or "").strip()
                if not transaction_id:
                    continue
                amount, transaction_currency = _money(transaction.get("amountSet"))
                connection.execute(
                    """
                    INSERT INTO shopify_payment_transactions VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    ON CONFLICT(shopify_transaction_id) DO UPDATE SET
                        kind=excluded.kind, status=excluded.status,
                        gateway=excluded.gateway, payment_id=excluded.payment_id,
                        amount=excluded.amount, currency=excluded.currency,
                        processed_at=excluded.processed_at,
                        test_transaction=excluded.test_transaction,
                        raw_json=excluded.raw_json, last_imported_at=excluded.last_imported_at
                    """,
                    (transaction_id, order_id, transaction.get("kind"), transaction.get("status"),
                     transaction.get("gateway"), transaction.get("paymentId"), amount,
                     transaction_currency, transaction.get("processedAt"),
                     int(bool(transaction.get("test"))),
                     json.dumps(transaction, separators=(",", ":")), now, now),
                )
                counts["transactions"] += 1
                if str(transaction.get("kind") or "").upper() == "REFUND" and str(transaction.get("status") or "").upper() == "SUCCESS":
                    counts["tasks"] += _task(
                        connection, f"shopify-refund:{transaction_id}", "refund_review",
                        f"Review Shopify refund for {order.get('name') or order_id}",
                        f"Refund {amount:.2f} {transaction_currency}; verify returned inventory condition and location.",
                        "high", order_id,
                    )
    return counts


def sync_shopify_sales(days_back: int = 60) -> dict[str, int]:
    ensure_shopify_operations_tables()
    safe_days = max(1, min(3650, int(days_back)))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=safe_days)).date().isoformat()
    orders: list[dict[str, Any]] = []
    cursor = None
    while True:
        data = _graphql(ORDER_QUERY, {
            "first": 50, "after": cursor, "query": f"processed_at:>={cutoff}",
        })
        connection = data.get("orders") or {}
        orders.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return _save_orders(orders)


def rebuild_replenishment_tasks() -> int:
    ensure_shopify_operations_tables()
    created = 0
    with _connect() as connection:
        storefront = connection.execute(
            "SELECT location_id FROM inventory_locations WHERE location_name=? LIMIT 1",
            (STOREFRONT_LOCATION_NAME,),
        ).fetchone()
        if not storefront:
            raise RuntimeError(f"Inventory location '{STOREFRONT_LOCATION_NAME}' was not found.")
        location_id = int(storefront["location_id"])
        rows = connection.execute(
            """
            SELECT i.product_id, p.product_name,
                   COALESCE(SUM(i.quantity_on_hand),0) quantity_on_hand,
                   COALESCE(MAX(i.reorder_level),2) reorder_level
            FROM inventory i
            JOIN products p ON p.product_id=i.product_id
            WHERE i.location_id=? AND COALESCE(p.active,1)=1
            GROUP BY i.product_id, p.product_name
            HAVING COALESCE(SUM(i.quantity_on_hand),0) <=
                   CASE WHEN COALESCE(MAX(i.reorder_level),2)>2
                        THEN COALESCE(MAX(i.reorder_level),2) ELSE 2 END
            """, (location_id,),
        ).fetchall()
        replenishments = []
        for row in rows:
            product_id = int(row["product_id"])
            reserve_rows = connection.execute(
                """
                SELECT i.location_id, l.location_name, l.location_type,
                       COALESCE(i.container_id,'') container_id,
                       COALESCE(i.quantity_on_hand,0) quantity_on_hand
                FROM inventory i
                JOIN inventory_locations l ON l.location_id=i.location_id
                WHERE i.product_id=? AND i.location_id<>?
                  AND COALESCE(i.quantity_on_hand,0)>0
                  AND COALESCE(l.active,1)=1
                  AND lower(COALESCE(l.location_type,'')) NOT IN ('hold','reserved','catalog','store')
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%damage%'
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%return%'
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%quarantine%'
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%missing%'
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%shrink%'
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%prob%'
                  AND lower(COALESCE(l.location_name,'')) NOT LIKE '%online orders%'
                ORDER BY CASE WHEN TRIM(COALESCE(i.container_id,''))<>'' THEN 0 ELSE 1 END,
                         i.quantity_on_hand DESC, l.location_name, i.container_id
                """, (product_id, location_id),
            ).fetchall()
            reserve_qty = sum(int(item["quantity_on_hand"] or 0) for item in reserve_rows)
            if reserve_qty <= 0:
                continue

            pick_slot = connection.execute(
                "SELECT location_id, container_id FROM product_pick_slots WHERE product_id=?",
                (product_id,),
            ).fetchone()
            if not pick_slot:
                pick_slot = connection.execute(
                    """
                    SELECT location_id, container_id
                    FROM inventory
                    WHERE product_id=? AND location_id=?
                      AND TRIM(COALESCE(container_id,''))<>''
                    ORDER BY quantity_on_hand DESC, container_id
                    LIMIT 1
                    """, (product_id, location_id),
                ).fetchone()
                if pick_slot:
                    connection.execute(
                        """INSERT INTO product_pick_slots(product_id,location_id,container_id,updated_at)
                           VALUES(?,?,?,?) ON CONFLICT(product_id) DO NOTHING""",
                        (product_id, int(pick_slot["location_id"]),
                         str(pick_slot["container_id"]).strip().upper(),
                         datetime.now(timezone.utc).isoformat()),
                    )
            replenishments.append((row, reserve_rows, reserve_qty, pick_slot))

        active_keys = {
            f"replenish:{row['product_id']}:{location_id}"
            for row, _reserve_rows, _reserve_qty, _pick_slot in replenishments
        }
        existing_open = connection.execute(
            """SELECT task_id, task_key FROM operations_work_queue
               WHERE task_type IN ('replenishment','directed_replenishment','placement_needed')
                 AND status='open' AND task_key LIKE 'replenish:%'"""
        ).fetchall()
        stale_ids = [int(row["task_id"]) for row in existing_open if row["task_key"] not in active_keys]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                f"""UPDATE operations_work_queue
                    SET status='cancelled', completed_at=?, updated_at=?,
                        resolution_notes=CASE
                            WHEN TRIM(COALESCE(resolution_notes,''))='' THEN 'Automatically closed: replenishment is no longer needed.'
                            ELSE resolution_notes || char(10) || 'Automatically closed: replenishment is no longer needed.'
                        END
                    WHERE task_id IN ({placeholders})""",
                [now, now, *stale_ids],
            )
        for row, reserve_rows, reserve_qty, pick_slot in replenishments:
            needed = max(1, max(int(row["reorder_level"] or 2), 2) - int(row["quantity_on_hand"] or 0))
            requested = min(needed, reserve_qty)
            best_source = reserve_rows[0]
            source_container = str(best_source["container_id"] or "").strip().upper()
            source_name = str(best_source["location_name"] or "Reserve")
            destination_container = (
                str(pick_slot["container_id"] or "").strip().upper()
                if pick_slot else ""
            )
            if destination_container:
                task_type = "directed_replenishment"
                title = f"Move {requested} × {row['product_name']} to {destination_container}"
                details = (
                    f"Take {requested} from {source_name}"
                    f" / {source_container or 'unlabeled stock'} and place it in "
                    f"{STOREFRONT_LOCATION_NAME} / {destination_container}. "
                    f"Storefront currently has {row['quantity_on_hand']}; reserve has {reserve_qty}."
                )
            else:
                task_type = "placement_needed"
                title = f"Assign a pickslot for {row['product_name']}"
                details = (
                    f"This product needs replenishment, but it has no destination pickslot. "
                    f"Reserve has {reserve_qty} in {source_name}"
                    f" / {source_container or 'unlabeled stock'}. Assign the shelf or pickslot first."
                )
            created += _task(
                connection, f"replenish:{row['product_id']}:{location_id}", task_type,
                title, details,
                "high" if int(row["quantity_on_hand"] or 0) <= 0 else "normal",
                source_reference=source_container or source_name,
                product_id=int(row["product_id"]), location_id=location_id,
                requested_quantity=requested, source_channel="brookshouse",
                source_location_id=int(best_source["location_id"]),
                source_container_id=source_container,
                destination_location_id=location_id,
                destination_container_id=destination_container,
            )
    return created


def install_shopify_operations(app: FastAPI) -> None:
    ensure_shopify_operations_tables()

    @app.get("/api/operations/summary")
    def operations_dashboard_summary():
        today = datetime.now().astimezone().date().isoformat()
        with _connect() as connection:
            queue = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_tasks,
                    SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) in_progress_tasks,
                    SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked_tasks
                FROM operations_work_queue
                """
            ).fetchone()
            sales = connection.execute(
                """
                SELECT COUNT(*) order_count, COALESCE(SUM(total_amount),0) sales_total
                FROM shopify_sales_orders
                WHERE test_order=0 AND substr(processed_at,1,10)=?
                """, (today,),
            ).fetchone()
            unmatched = connection.execute(
                "SELECT COUNT(*) FROM shopify_sales_lines WHERE match_status<>'matched'"
            ).fetchone()[0]
            last_sync = connection.execute(
                "SELECT MAX(last_imported_at) FROM shopify_sales_orders"
            ).fetchone()[0]
        return {
            "open_tasks": int(queue["open_tasks"] or 0),
            "in_progress_tasks": int(queue["in_progress_tasks"] or 0),
            "blocked_tasks": int(queue["blocked_tasks"] or 0),
            "today_orders": int(sales["order_count"] or 0),
            "today_sales": float(sales["sales_total"] or 0),
            "unmatched_items": int(unmatched or 0),
            "last_shopify_sync": last_sync,
        }

    @app.get("/channels/shopify/sales", response_class=HTMLResponse)
    def shopify_sales_page(request: Request):
        with _connect() as connection:
            orders = connection.execute(
                """
                SELECT o.*, COUNT(DISTINCT l.shopify_line_id) line_count,
                       SUM(CASE WHEN l.match_status<>'matched' THEN 1 ELSE 0 END) unmatched_lines
                FROM shopify_sales_orders o
                LEFT JOIN shopify_sales_lines l ON l.shopify_order_id=o.shopify_order_id
                GROUP BY o.shopify_order_id ORDER BY o.processed_at DESC LIMIT 300
                """
            ).fetchall()
            summary = connection.execute(
                """
                SELECT COUNT(*) orders, COALESCE(SUM(total_amount),0) gross_sales,
                       COALESCE(SUM(refund_amount),0) refunds
                FROM shopify_sales_orders WHERE test_order=0
                """
            ).fetchone()
        return TEMPLATES.TemplateResponse(request=request, name="shopify_sales_history.html", context={
            "orders": orders, "summary": summary,
            "message": request.query_params.get("message"), "error": request.query_params.get("error"),
        })

    @app.get("/channels/shopify/sales/details", response_class=HTMLResponse)
    def shopify_sale_details_page(request: Request, order_id: str = ""):
        order_id = str(order_id or "").strip()
        order = None
        lines = []
        transactions = []
        related_tasks = []
        location_name = ""
        if order_id:
            with _connect() as connection:
                order = connection.execute(
                    "SELECT * FROM shopify_sales_orders WHERE shopify_order_id=? LIMIT 1",
                    (order_id,),
                ).fetchone()
                if order:
                    lines = connection.execute(
                        """
                        SELECT l.*, p.product_name AS brookshouse_product_name
                        FROM shopify_sales_lines l
                        LEFT JOIN products p ON p.product_id=l.product_id
                        WHERE l.shopify_order_id=?
                        ORDER BY l.title, l.shopify_line_id
                        """,
                        (order_id,),
                    ).fetchall()
                    transactions = connection.execute(
                        """
                        SELECT * FROM shopify_payment_transactions
                        WHERE shopify_order_id=?
                        ORDER BY processed_at, shopify_transaction_id
                        """,
                        (order_id,),
                    ).fetchall()
                    related_tasks = connection.execute(
                        """
                        SELECT q.*, p.product_name, loc.location_name
                        FROM operations_work_queue q
                        LEFT JOIN products p ON p.product_id=q.product_id
                        LEFT JOIN inventory_locations loc ON loc.location_id=q.location_id
                        WHERE q.source_reference=?
                        ORDER BY q.created_at
                        """,
                        (order_id,),
                    ).fetchall()
                    try:
                        raw_order = json.loads(order["raw_json"] or "{}")
                        location_name = str((raw_order.get("physicalLocation") or {}).get("name") or "")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        location_name = ""
        error = "" if order else ("Transaction not found." if order_id else "No transaction was selected.")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="shopify_sale_details.html",
            context={
                "order": order,
                "lines": lines,
                "transactions": transactions,
                "related_tasks": related_tasks,
                "location_name": location_name,
                "error": error,
            },
        )

    @app.post("/channels/shopify/sales/sync")
    def shopify_sales_sync(days_back: int = Form(60)):
        try:
            result = sync_shopify_sales(days_back)
            replenish = rebuild_replenishment_tasks()
            message = (f"Shopify sync saved {result['orders']} orders, {result['lines']} items and "
                       f"{result['transactions']} payment transactions. Queue tasks refreshed: "
                       f"{result['tasks'] + replenish}.")
            url = "/channels/shopify/sales?message=" + quote_plus(message)
        except Exception as error:
            url = "/channels/shopify/sales?error=" + quote_plus(str(error))
        return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/operations/work-queue", response_class=HTMLResponse)
    def operations_queue_page(
        request: Request,
        task_status: str = "open",
        task_type: str = "all",
        priority: str = "all",
        assigned_to: str = "all",
        source_channel: str = "all",
        search: str = "",
        sort_by: str = "priority",
        page: int = 1,
        page_size: int = 15,
    ):
        clauses, params = [], []
        valid_statuses = {"open", "in_progress", "blocked", "completed", "cancelled", "all"}
        task_status = task_status if task_status in valid_statuses else "open"
        if task_status != "all":
            clauses.append("q.status=?")
            params.append(task_status)
        if task_type != "all":
            clauses.append("q.task_type=?")
            params.append(task_type)
        if priority != "all":
            clauses.append("q.priority=?")
            params.append(priority)
        if assigned_to == "unassigned":
            clauses.append("COALESCE(TRIM(q.assigned_to_name),'')='' AND q.assigned_user_id IS NULL")
        elif assigned_to != "all":
            clauses.append("COALESCE(q.assigned_to_name,'')=?")
            params.append(assigned_to)
        if source_channel != "all":
            clauses.append("COALESCE(q.source_channel,'')=?")
            params.append(source_channel)
        search = str(search or "").strip()
        if search:
            pattern = f"%{search}%"
            clauses.append(
                "(q.title LIKE ? OR q.details LIKE ? OR p.product_name LIKE ? "
                "OR l.location_name LIKE ? OR q.source_reference LIKE ? OR CAST(q.task_id AS TEXT) LIKE ? "
                "OR q.source_container_id LIKE ? OR q.destination_container_id LIKE ? "
                "OR sl.location_name LIKE ? OR dl.location_name LIKE ?)"
            )
            params.extend([pattern] * 10)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        page_size = page_size if page_size in {15, 25, 50} else 15
        page = max(1, int(page or 1))
        sort_sql = {
            "priority": "CASE q.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, q.created_at",
            "oldest": "q.created_at, q.task_id",
            "newest": "q.created_at DESC, q.task_id DESC",
            "location": "COALESCE(dl.location_name,l.location_name,''), COALESCE(q.destination_container_id,''), q.created_at",
        }.get(sort_by, "CASE q.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, q.created_at")
        sort_by = sort_by if sort_by in {"priority", "oldest", "newest", "location"} else "priority"
        with _connect() as connection:
            total_rows = int(connection.execute(
                f"""SELECT COUNT(*)
                    FROM operations_work_queue q
                    LEFT JOIN products p ON p.product_id=q.product_id
                    LEFT JOIN inventory_locations l ON l.location_id=q.location_id
                    LEFT JOIN inventory_locations sl ON sl.location_id=q.source_location_id
                    LEFT JOIN inventory_locations dl ON dl.location_id=q.destination_location_id
                    {where}""", params,
            ).fetchone()[0])
            total_pages = max(1, (total_rows + page_size - 1) // page_size)
            page = min(page, total_pages)
            tasks = connection.execute(
                f"""SELECT q.*, p.product_name, l.location_name,
                            sl.location_name source_location_name,
                            dl.location_name destination_location_name,
                            (SELECT pb.barcode FROM product_barcodes pb
                             WHERE pb.product_id=q.product_id
                             ORDER BY COALESCE(pb.is_primary,0) DESC, pb.barcode
                             LIMIT 1) product_barcode
                     FROM operations_work_queue q
                     LEFT JOIN products p ON p.product_id=q.product_id
                     LEFT JOIN inventory_locations l ON l.location_id=q.location_id
                     LEFT JOIN inventory_locations sl ON sl.location_id=q.source_location_id
                     LEFT JOIN inventory_locations dl ON dl.location_id=q.destination_location_id
                     {where}
                     ORDER BY {sort_sql}
                     LIMIT ? OFFSET ?""", [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            counts = {row["status"]: row["count"] for row in connection.execute(
                "SELECT status,COUNT(*) count FROM operations_work_queue GROUP BY status"
            )}
            task_types = [row[0] for row in connection.execute(
                "SELECT DISTINCT task_type FROM operations_work_queue ORDER BY task_type"
            ).fetchall()]
            sources = [row[0] for row in connection.execute(
                "SELECT DISTINCT source_channel FROM operations_work_queue WHERE source_channel IS NOT NULL AND source_channel<>'' ORDER BY source_channel"
            ).fetchall()]
            try:
                workers = connection.execute(
                    "SELECT user_id, display_name, role FROM app_users WHERE active=1 ORDER BY display_name"
                ).fetchall()
            except sqlite3.OperationalError:
                workers = []
            assigned_names = [row[0] for row in connection.execute(
                "SELECT DISTINCT assigned_to_name FROM operations_work_queue WHERE TRIM(COALESCE(assigned_to_name,''))<>'' ORDER BY assigned_to_name"
            ).fetchall()]
        filter_values = {
            "task_status": task_status, "task_type": task_type, "priority": priority,
            "assigned_to": assigned_to, "source_channel": source_channel, "search": search,
            "sort_by": sort_by, "page_size": page_size,
        }
        query_without_page = urlencode(filter_values)
        return_to = "/operations/work-queue?" + urlencode({**filter_values, "page": page})
        return TEMPLATES.TemplateResponse(request=request, name="operations_work_queue.html", context={
            "tasks": tasks, "counts": counts, "task_status": task_status, "task_type": task_type,
            "priority": priority, "assigned_to": assigned_to, "source_channel": source_channel,
            "search": search, "sort_by": sort_by, "page": page, "page_size": page_size,
            "total_rows": total_rows, "total_pages": total_pages, "query_without_page": query_without_page,
            "return_to": return_to, "workers": workers, "assigned_names": assigned_names,
            "task_types": task_types, "sources": sources,
            "message": request.query_params.get("message"), "error": request.query_params.get("error"),
        })

    @app.post("/operations/work-queue/refresh")
    def refresh_operations_queue():
        try:
            count = rebuild_replenishment_tasks()
            url = "/operations/work-queue?message=" + quote_plus(f"Work queue refreshed: {count} directed/placement replenishment task(s) checked.")
        except Exception as error:
            url = "/operations/work-queue?error=" + quote_plus(str(error))
        return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/operations/work-queue/{task_id}/assign-pickslot")
    def assign_replenishment_pickslot(
        task_id: int,
        destination_container_id: str = Form(...),
        return_to: str = Form("/operations/work-queue"),
    ):
        pickslot = str(destination_container_id or "").strip().upper()
        if not pickslot:
            redirect_base = return_to if return_to.startswith("/operations/work-queue") else "/operations/work-queue"
            separator = "&" if "?" in redirect_base else "?"
            return RedirectResponse(
                url=redirect_base + separator + "error=" + quote_plus("Scan or enter a destination pickslot first."),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        now = datetime.now(timezone.utc).isoformat()
        with _connect() as connection:
            task = connection.execute(
                """SELECT product_id, COALESCE(destination_location_id,location_id) destination_location_id
                   FROM operations_work_queue WHERE task_id=? AND task_key LIKE 'replenish:%'""",
                (task_id,),
            ).fetchone()
            if not task or not task["product_id"] or not task["destination_location_id"]:
                raise ValueError("This replenishment task cannot be assigned a pickslot.")
            connection.execute(
                """INSERT INTO product_pick_slots(product_id,location_id,container_id,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(product_id) DO UPDATE SET
                     location_id=excluded.location_id,
                     container_id=excluded.container_id,
                     updated_at=excluded.updated_at""",
                (int(task["product_id"]), int(task["destination_location_id"]), pickslot, now),
            )
        rebuild_replenishment_tasks()
        redirect_base = return_to if return_to.startswith("/operations/work-queue") else "/operations/work-queue"
        separator = "&" if "?" in redirect_base else "?"
        return RedirectResponse(
            url=redirect_base + separator + "message=" + quote_plus(
                f"Pickslot {pickslot} assigned. The task is now Directed Replenishment."
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/operations/work-queue/{task_id}/update")
    def update_operations_task(
        task_id: int,
        task_status: str = Form(...),
        assigned_to_name: str = Form(""),
        assigned_user_id: str = Form(""),
        resolution_notes: str = Form(""),
        return_to: str = Form("/operations/work-queue"),
    ):
        if task_status not in {"open", "in_progress", "blocked", "completed", "cancelled"}:
            task_status = "open"
        now = datetime.now(timezone.utc).isoformat()
        worker_id = int(assigned_user_id) if str(assigned_user_id).isdigit() else None
        with _connect() as connection:
            if worker_id:
                worker = connection.execute(
                    "SELECT display_name FROM app_users WHERE user_id=? AND active=1", (worker_id,)
                ).fetchone()
                assigned_to_name = worker["display_name"] if worker else assigned_to_name
            connection.execute(
                """UPDATE operations_work_queue SET status=?, assigned_user_id=?, assigned_to_name=?, resolution_notes=?,
                          completed_at=CASE WHEN ?='completed' THEN ? ELSE NULL END, updated_at=?
                   WHERE task_id=?""",
                (task_status, worker_id, assigned_to_name.strip(), resolution_notes.strip(), task_status, now, now, task_id),
            )
        redirect_base = return_to if str(return_to).startswith("/operations/work-queue") else "/operations/work-queue"
        separator = "&" if "?" in redirect_base else "?"
        return RedirectResponse(url=redirect_base + separator + "message=" + quote_plus("Task updated."), status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/operations/work-queue/bulk-update")
    async def bulk_update_operations_tasks(request: Request):
        form = await request.form()
        task_ids = sorted({int(value) for value in form.getlist("task_ids") if str(value).isdigit()})
        bulk_action = str(form.get("bulk_action") or "").strip()
        assigned_user_id = str(form.get("assigned_user_id") or "").strip()
        return_to = str(form.get("return_to") or "/operations/work-queue")
        if not task_ids:
            separator = "&" if "?" in return_to else "?"
            return RedirectResponse(url=return_to + separator + "error=" + quote_plus("Select at least one task."), status_code=status.HTTP_303_SEE_OTHER)
        placeholders = ",".join("?" for _ in task_ids)
        now = datetime.now(timezone.utc).isoformat()
        with _connect() as connection:
            if bulk_action in {"open", "in_progress", "blocked", "completed", "cancelled"}:
                connection.execute(
                    f"""UPDATE operations_work_queue SET status=?, updated_at=?,
                         completed_at=CASE WHEN ?='completed' THEN ? ELSE NULL END
                         WHERE task_id IN ({placeholders})""",
                    [bulk_action, now, bulk_action, now, *task_ids],
                )
            elif bulk_action == "assign" and assigned_user_id.isdigit():
                worker = connection.execute(
                    "SELECT user_id, display_name FROM app_users WHERE user_id=? AND active=1",
                    (int(assigned_user_id),),
                ).fetchone()
                if worker:
                    connection.execute(
                        f"""UPDATE operations_work_queue SET assigned_user_id=?, assigned_to_name=?, updated_at=?
                             WHERE task_id IN ({placeholders})""",
                        [worker["user_id"], worker["display_name"], now, *task_ids],
                    )
        redirect_base = return_to if return_to.startswith("/operations/work-queue") else "/operations/work-queue"
        separator = "&" if "?" in redirect_base else "?"
        return RedirectResponse(
            url=redirect_base + separator + "message=" + quote_plus(f"Updated {len(task_ids)} selected task(s)."),
            status_code=status.HTTP_303_SEE_OTHER,
        )
