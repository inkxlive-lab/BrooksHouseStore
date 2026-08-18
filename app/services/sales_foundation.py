from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = APP_DIR / "data" / "brookshouse_store.db"
CHANNELS = ("shopify", "walmart", "amazon")


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_datetime(value: Any) -> str | None:
    """Return ISO text for source timestamps, including Walmart Unix milliseconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    numeric = text.lstrip("-")
    if numeric.isdigit():
        try:
            timestamp = int(text)
            if abs(timestamp) >= 100_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sales_foundation_orders (
            sales_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sales_channel TEXT NOT NULL,
            external_order_id TEXT NOT NULL,
            order_number TEXT,
            ordered_at TEXT,
            updated_at_source TEXT,
            order_status TEXT,
            fulfillment_status TEXT,
            currency TEXT,
            gross_sales REAL NOT NULL DEFAULT 0,
            discount_amount REAL NOT NULL DEFAULT 0,
            refund_amount REAL NOT NULL DEFAULT 0,
            net_sales REAL NOT NULL DEFAULT 0,
            tax_amount REAL NOT NULL DEFAULT 0,
            shipping_amount REAL NOT NULL DEFAULT 0,
            order_total REAL NOT NULL DEFAULT 0,
            is_cancelled INTEGER NOT NULL DEFAULT 0,
            is_test INTEGER NOT NULL DEFAULT 0,
            source_table TEXT NOT NULL,
            first_normalized_at TEXT NOT NULL,
            last_normalized_at TEXT NOT NULL,
            UNIQUE(sales_channel, external_order_id)
        );

        CREATE TABLE IF NOT EXISTS sales_foundation_lines (
            sales_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sales_channel TEXT NOT NULL,
            external_order_id TEXT NOT NULL,
            external_line_id TEXT NOT NULL,
            product_id INTEGER,
            sku TEXT,
            barcode TEXT,
            product_title TEXT,
            quantity INTEGER NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            gross_sales REAL NOT NULL DEFAULT 0,
            discount_amount REAL NOT NULL DEFAULT 0,
            refund_amount REAL NOT NULL DEFAULT 0,
            net_sales REAL NOT NULL DEFAULT 0,
            unit_cost_snapshot REAL,
            estimated_cost REAL,
            estimated_gross_profit REAL,
            product_match_status TEXT NOT NULL DEFAULT 'unmatched',
            amount_quality TEXT NOT NULL DEFAULT 'exact',
            source_table TEXT NOT NULL,
            first_normalized_at TEXT NOT NULL,
            last_normalized_at TEXT NOT NULL,
            UNIQUE(sales_channel, external_order_id, external_line_id)
        );

        CREATE TABLE IF NOT EXISTS sales_foundation_refresh_log (
            refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            shopify_orders INTEGER NOT NULL DEFAULT 0,
            walmart_orders INTEGER NOT NULL DEFAULT 0,
            amazon_orders INTEGER NOT NULL DEFAULT 0,
            total_lines INTEGER NOT NULL DEFAULT 0,
            matched_lines INTEGER NOT NULL DEFAULT 0,
            message TEXT
        );

        CREATE INDEX IF NOT EXISTS ix_sales_foundation_orders_date
            ON sales_foundation_orders(ordered_at);
        CREATE INDEX IF NOT EXISTS ix_sales_foundation_orders_channel
            ON sales_foundation_orders(sales_channel);
        CREATE INDEX IF NOT EXISTS ix_sales_foundation_lines_product
            ON sales_foundation_lines(product_id);
        CREATE INDEX IF NOT EXISTS ix_sales_foundation_lines_barcode
            ON sales_foundation_lines(barcode);

        DROP VIEW IF EXISTS sales_foundation_daily;
        CREATE VIEW sales_foundation_daily AS
        SELECT
            substr(ordered_at, 1, 10) AS sales_date,
            sales_channel,
            COUNT(*) AS order_count,
            ROUND(SUM(net_sales), 2) AS net_sales,
            ROUND(SUM(order_total), 2) AS collected_total
        FROM sales_foundation_orders
        WHERE is_cancelled = 0 AND is_test = 0
        GROUP BY substr(ordered_at, 1, 10), sales_channel;

        DROP VIEW IF EXISTS sales_foundation_product_channel;
        CREATE VIEW sales_foundation_product_channel AS
        SELECT
            l.product_id,
            COALESCE(NULLIF(TRIM(p.product_name), ''), NULLIF(TRIM(l.product_title), ''),
                     NULLIF(TRIM(l.sku), ''), NULLIF(TRIM(l.barcode), ''), 'Unknown product') AS product_name,
            l.sales_channel,
            SUM(l.quantity) AS units_sold,
            ROUND(SUM(l.net_sales), 2) AS net_sales,
            ROUND(SUM(COALESCE(l.estimated_cost, 0)), 2) AS estimated_cost,
            ROUND(SUM(COALESCE(l.estimated_gross_profit, 0)), 2) AS estimated_gross_profit,
            SUM(CASE WHEN l.product_id IS NULL THEN 1 ELSE 0 END) AS unmatched_lines
        FROM sales_foundation_lines l
        LEFT JOIN products p ON p.product_id = l.product_id
        JOIN sales_foundation_orders o
          ON o.sales_channel = l.sales_channel
         AND o.external_order_id = l.external_order_id
        WHERE o.is_cancelled = 0 AND o.is_test = 0
        GROUP BY l.product_id, product_name, l.sales_channel;
        """
    )


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def product_cost(conn: sqlite3.Connection, product_id: Any) -> float | None:
    if product_id in (None, "") or "average_cost" not in columns(conn, "products"):
        return None
    row = conn.execute(
        "SELECT average_cost FROM products WHERE product_id=?", (product_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return as_float(row[0])


def match_product(conn: sqlite3.Connection, product_id: Any, barcode: Any, sku: Any) -> int | None:
    if product_id not in (None, ""):
        return as_int(product_id)
    barcode = str(barcode or "").strip()
    if barcode and table_exists(conn, "product_barcodes"):
        row = conn.execute(
            "SELECT product_id FROM product_barcodes WHERE TRIM(barcode)=? ORDER BY is_primary DESC LIMIT 1",
            (barcode,),
        ).fetchone()
        if row:
            return as_int(row[0])
    sku = str(sku or "").strip()
    product_cols = columns(conn, "products")
    if sku and "sku" in product_cols:
        row = conn.execute(
            "SELECT product_id FROM products WHERE TRIM(sku)=? LIMIT 1", (sku,)
        ).fetchone()
        if row:
            return as_int(row[0])
    return None


def upsert_order(conn: sqlite3.Connection, data: dict[str, Any], stamp: str) -> None:
    values = {**data, "stamp": stamp}
    conn.execute(
        """
        INSERT INTO sales_foundation_orders (
            sales_channel, external_order_id, order_number, ordered_at,
            updated_at_source, order_status, fulfillment_status, currency,
            gross_sales, discount_amount, refund_amount, net_sales, tax_amount,
            shipping_amount, order_total, is_cancelled, is_test, source_table,
            first_normalized_at, last_normalized_at
        ) VALUES (
            :sales_channel, :external_order_id, :order_number, :ordered_at,
            :updated_at_source, :order_status, :fulfillment_status, :currency,
            :gross_sales, :discount_amount, :refund_amount, :net_sales, :tax_amount,
            :shipping_amount, :order_total, :is_cancelled, :is_test, :source_table,
            :stamp, :stamp
        )
        ON CONFLICT(sales_channel, external_order_id) DO UPDATE SET
            order_number=excluded.order_number, ordered_at=excluded.ordered_at,
            updated_at_source=excluded.updated_at_source, order_status=excluded.order_status,
            fulfillment_status=excluded.fulfillment_status, currency=excluded.currency,
            gross_sales=excluded.gross_sales, discount_amount=excluded.discount_amount,
            refund_amount=excluded.refund_amount, net_sales=excluded.net_sales,
            tax_amount=excluded.tax_amount, shipping_amount=excluded.shipping_amount,
            order_total=excluded.order_total, is_cancelled=excluded.is_cancelled,
            is_test=excluded.is_test, source_table=excluded.source_table,
            last_normalized_at=excluded.last_normalized_at
        """,
        values,
    )


def upsert_line(conn: sqlite3.Connection, data: dict[str, Any], stamp: str) -> None:
    product_id = match_product(conn, data.get("product_id"), data.get("barcode"), data.get("sku"))
    quantity = as_int(data.get("quantity"))
    net_sales = as_float(data.get("net_sales"))
    cost_override = data.get("unit_cost_override")
    cost = as_float(cost_override) if cost_override is not None else product_cost(conn, product_id)
    estimated_cost = None if cost is None else round(cost * quantity, 4)
    profit = None if estimated_cost is None else round(net_sales - estimated_cost, 4)
    match_status = "matched" if product_id is not None else (
        "cost_rule" if cost_override is not None else "unmatched"
    )
    values = {
        **data,
        "product_id": product_id,
        "product_match_status": match_status,
        "unit_cost_snapshot": cost,
        "estimated_cost": estimated_cost,
        "estimated_gross_profit": profit,
        "stamp": stamp,
    }
    conn.execute(
        """
        INSERT INTO sales_foundation_lines (
            sales_channel, external_order_id, external_line_id, product_id, sku,
            barcode, product_title, quantity, unit_price, gross_sales,
            discount_amount, refund_amount, net_sales, unit_cost_snapshot,
            estimated_cost, estimated_gross_profit, product_match_status,
            amount_quality, source_table, first_normalized_at, last_normalized_at
        ) VALUES (
            :sales_channel, :external_order_id, :external_line_id, :product_id, :sku,
            :barcode, :product_title, :quantity, :unit_price, :gross_sales,
            :discount_amount, :refund_amount, :net_sales, :unit_cost_snapshot,
            :estimated_cost, :estimated_gross_profit, :product_match_status,
            :amount_quality, :source_table, :stamp, :stamp
        )
        ON CONFLICT(sales_channel, external_order_id, external_line_id) DO UPDATE SET
            product_id=excluded.product_id, sku=excluded.sku, barcode=excluded.barcode,
            product_title=excluded.product_title, quantity=excluded.quantity,
            unit_price=excluded.unit_price, gross_sales=excluded.gross_sales,
            discount_amount=excluded.discount_amount, refund_amount=excluded.refund_amount,
            net_sales=excluded.net_sales, unit_cost_snapshot=excluded.unit_cost_snapshot,
            estimated_cost=excluded.estimated_cost,
            estimated_gross_profit=excluded.estimated_gross_profit,
            product_match_status=excluded.product_match_status,
            amount_quality=excluded.amount_quality, source_table=excluded.source_table,
            last_normalized_at=excluded.last_normalized_at
        """,
        values,
    )


def refresh_shopify(conn: sqlite3.Connection, stamp: str) -> int:
    if not table_exists(conn, "shopify_sales_orders"):
        return 0
    for row in conn.execute("SELECT * FROM shopify_sales_orders"):
        subtotal = as_float(row["subtotal_amount"])
        discounts = as_float(row["discount_amount"])
        refunds = as_float(row["refund_amount"])
        upsert_order(conn, {
            "sales_channel": "shopify", "external_order_id": str(row["shopify_order_id"]),
            "order_number": row["order_name"] or row["order_number"], "ordered_at": row["processed_at"],
            "updated_at_source": row["updated_at"], "order_status": row["financial_status"],
            "fulfillment_status": row["fulfillment_status"], "currency": row["currency"],
            "gross_sales": subtotal + discounts, "discount_amount": discounts,
            "refund_amount": refunds, "net_sales": subtotal - refunds,
            "tax_amount": as_float(row["tax_amount"]), "shipping_amount": 0,
            "order_total": as_float(row["total_amount"]) - refunds,
            "is_cancelled": 1 if row["cancelled_at"] else 0, "is_test": as_int(row["test_order"]),
            "source_table": "shopify_sales_orders",
        }, stamp)
    if table_exists(conn, "shopify_sales_lines"):
        for row in conn.execute("SELECT * FROM shopify_sales_lines"):
            qty = as_int(row["current_quantity"] if row["current_quantity"] is not None else row["quantity"])
            gross = as_float(row["unit_price"]) * qty
            discount = as_float(row["discount_amount"])
            net = as_float(row["net_amount"])
            chosen_product_id = row["product_id"]
            if chosen_product_id is None and table_exists(conn, "shopify_exact_title_matches"):
                exact = conn.execute(
                    """SELECT product_id FROM shopify_exact_title_matches
                       WHERE normalized_title=lower(trim(?)) AND active=1 LIMIT 1""",
                    (row["title"],),
                ).fetchone()
                if exact:
                    chosen_product_id = exact[0]

            unit_cost_override = None
            if chosen_product_id is None and table_exists(conn, "shopify_quick_sale_cost_rules"):
                rule = conn.execute(
                    """SELECT cost_method, cost_value FROM shopify_quick_sale_cost_rules
                       WHERE normalized_title=lower(trim(?)) AND active=1 LIMIT 1""",
                    (row["title"],),
                ).fetchone()
                if rule and rule[0] == "fixed_per_unit":
                    unit_cost_override = as_float(rule[1])
                elif rule and rule[0] == "percent_of_sales":
                    unit_cost_override = (net * as_float(rule[1]) / 100 / qty) if qty else 0
            upsert_line(conn, {
                "sales_channel": "shopify", "external_order_id": str(row["shopify_order_id"]),
                "external_line_id": str(row["shopify_line_id"]), "product_id": chosen_product_id,
                "sku": row["sku"], "barcode": row["barcode"], "product_title": row["title"],
                "quantity": qty, "unit_price": as_float(row["unit_price"]), "gross_sales": gross,
                "discount_amount": discount, "refund_amount": max(0, gross - discount - net),
                "net_sales": net, "amount_quality": "exact", "source_table": "shopify_sales_lines",
                "unit_cost_override": unit_cost_override,
            }, stamp)
    return as_int(conn.execute("SELECT COUNT(*) FROM shopify_sales_orders").fetchone()[0])


def refresh_amazon(conn: sqlite3.Connection, stamp: str) -> int:
    if not table_exists(conn, "amazon_order_history"):
        return 0
    for row in conn.execute("SELECT * FROM amazon_order_history"):
        total = as_float(row["order_total"])
        status = str(row["fulfillment_status"] or "")
        upsert_order(conn, {
            "sales_channel": "amazon", "external_order_id": str(row["amazon_order_id"]),
            "order_number": str(row["amazon_order_id"]), "ordered_at": row["created_time"],
            "updated_at_source": row["last_updated_time"], "order_status": status,
            "fulfillment_status": status, "currency": row["currency_code"],
            "gross_sales": total, "discount_amount": 0, "refund_amount": 0,
            "net_sales": total, "tax_amount": 0, "shipping_amount": 0, "order_total": total,
            "is_cancelled": 1 if status.upper() in {"CANCELLED", "CANCELED"} else 0,
            "is_test": 0, "source_table": "amazon_order_history",
        }, stamp)
    if table_exists(conn, "amazon_order_item_history"):
        for row in conn.execute("SELECT * FROM amazon_order_item_history"):
            qty = as_int(row["quantity_ordered"])
            net = as_float(row["item_total"])
            upsert_line(conn, {
                "sales_channel": "amazon", "external_order_id": str(row["amazon_order_id"]),
                "external_line_id": str(row["order_item_id"]), "product_id": row["product_id"],
                "sku": row["seller_sku"], "barcode": None, "product_title": row["title"],
                "quantity": qty, "unit_price": net / qty if qty else 0, "gross_sales": net,
                "discount_amount": 0, "refund_amount": 0, "net_sales": net,
                "amount_quality": "exact", "source_table": "amazon_order_item_history",
            }, stamp)
    return as_int(conn.execute("SELECT COUNT(*) FROM amazon_order_history").fetchone()[0])


def refresh_walmart(conn: sqlite3.Connection, stamp: str) -> int:
    if not table_exists(conn, "walmart_orders"):
        return 0
    order_cols = columns(conn, "walmart_orders")
    for row in conn.execute("SELECT * FROM walmart_orders"):
        total = as_float(row["order_total"]) if "order_total" in order_cols else 0
        status = str(row["walmart_status"] or row["local_status"] or "")
        upsert_order(conn, {
            "sales_channel": "walmart", "external_order_id": str(row["purchase_order_id"]),
            "order_number": row["customer_order_id"] or row["purchase_order_id"],
            "ordered_at": normalize_datetime(row["order_date"]),
            "updated_at_source": row["synced_at"], "order_status": status,
            "fulfillment_status": row["local_status"],
            "currency": row["currency"] if "currency" in order_cols else None,
            "gross_sales": total, "discount_amount": 0, "refund_amount": 0, "net_sales": total,
            "tax_amount": 0, "shipping_amount": 0, "order_total": total,
            "is_cancelled": 1 if status.upper() in {"CANCELLED", "CANCELED"} else 0,
            "is_test": 0, "source_table": "walmart_orders",
        }, stamp)
    if table_exists(conn, "walmart_order_lines"):
        rows = conn.execute(
            """
            SELECT l.*, COALESCE(o.order_total, 0) AS source_order_total,
                   SUM(CASE WHEN l2.quantity > 0 THEN l2.quantity ELSE 0 END)
                       OVER (PARTITION BY l.purchase_order_id) AS source_order_units
            FROM walmart_order_lines l
            JOIN walmart_orders o ON o.purchase_order_id=l.purchase_order_id
            JOIN walmart_order_lines l2 ON l2.purchase_order_id=l.purchase_order_id
            GROUP BY l.order_line_id
            """
        ).fetchall()
        for row in rows:
            qty = as_int(row["quantity"])
            units = as_int(row["source_order_units"])
            allocated = as_float(row["source_order_total"]) * qty / units if units else 0
            upsert_line(conn, {
                "sales_channel": "walmart", "external_order_id": str(row["purchase_order_id"]),
                "external_line_id": str(row["line_number"]), "product_id": row["product_id"],
                "sku": row["sku"], "barcode": row["upc"], "product_title": row["item_name"],
                "quantity": qty, "unit_price": allocated / qty if qty else 0,
                "gross_sales": allocated, "discount_amount": 0, "refund_amount": 0,
                "net_sales": allocated, "amount_quality": "allocated_order_total",
                "source_table": "walmart_order_lines",
            }, stamp)
    return as_int(conn.execute("SELECT COUNT(*) FROM walmart_orders").fetchone()[0])


def refresh(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    stamp = now_text()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        ensure_schema(conn)
        cursor = conn.execute(
            "INSERT INTO sales_foundation_refresh_log(started_at) VALUES (?)", (stamp,)
        )
        refresh_id = cursor.lastrowid
        counts = {
            "shopify": refresh_shopify(conn, stamp),
            "walmart": refresh_walmart(conn, stamp),
            "amazon": refresh_amazon(conn, stamp),
        }
        total_lines = as_int(conn.execute("SELECT COUNT(*) FROM sales_foundation_lines").fetchone()[0])
        matched = as_int(conn.execute(
            "SELECT COUNT(*) FROM sales_foundation_lines WHERE product_id IS NOT NULL"
        ).fetchone()[0])
        conn.execute(
            """UPDATE sales_foundation_refresh_log SET completed_at=?, status='complete',
               shopify_orders=?, walmart_orders=?, amazon_orders=?, total_lines=?, matched_lines=?
               WHERE refresh_id=?""",
            (now_text(), counts["shopify"], counts["walmart"], counts["amazon"], total_lines, matched, refresh_id),
        )
        conn.commit()
        return {"orders": counts, "lines": total_lines, "matched_lines": matched}
    except Exception as exc:
        conn.rollback()
        raise RuntimeError(f"Sales Foundation refresh failed: {exc}") from exc
    finally:
        conn.close()


def print_check(db_path: Path) -> None:
    result = refresh(db_path)
    print("Sales Foundation is ready.")
    print(f"Shopify orders: {result['orders']['shopify']}")
    print(f"Walmart orders: {result['orders']['walmart']}")
    print(f"Amazon orders:  {result['orders']['amazon']}")
    print(f"Sales lines:    {result['lines']}")
    print(f"Matched lines:  {result['matched_lines']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or refresh BrooksHouse Sales Foundation")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    print_check(args.db)
