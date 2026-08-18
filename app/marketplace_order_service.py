from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from app.walmart_order_service import load_order_desk


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "brookshouse_store.db"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _display_datetime(value):
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value or "Unknown")
    return parsed.astimezone().strftime("%a %m/%d/%Y\n%I:%M %p")


def _age(value):
    parsed = _parse_datetime(value)
    if parsed is None:
        return ""
    now = datetime.now().astimezone()
    parsed = parsed.astimezone()
    seconds = max(0, int((now - parsed).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    return f"{days}d {hours}h old"


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _amazon_orders():
    orders = []
    with _connect() as connection:
        if not (_table_exists(connection, "amazon_order_history") and
                _table_exists(connection, "amazon_order_item_history")):
            return orders
        header_rows = connection.execute(
            """
            SELECT amazon_order_id, created_time, fulfillment_status,
                   order_total, currency_code, item_count, unit_count
            FROM amazon_order_history
            ORDER BY created_time DESC
            """
        ).fetchall()
        for header in header_rows:
            line_rows = connection.execute(
                """
                SELECT order_item_id, seller_sku, asin, title,
                       quantity_ordered, item_total, product_id
                FROM amazon_order_item_history
                WHERE amazon_order_id=?
                ORDER BY order_item_id
                """,
                (header["amazon_order_id"],),
            ).fetchall()
            lines = []
            site_names = set()
            for line in line_rows:
                product_name = None
                barcode = line["asin"] or line["seller_sku"]
                if line["product_id"] is not None:
                    product = connection.execute(
                        "SELECT product_name FROM products WHERE product_id=?",
                        (line["product_id"],),
                    ).fetchone()
                    product_name = product[0] if product else None
                    barcode_row = connection.execute(
                        """SELECT barcode FROM product_barcodes WHERE product_id=?
                           ORDER BY is_primary DESC, barcode_id LIMIT 1""",
                        (line["product_id"],),
                    ).fetchone()
                    if barcode_row:
                        barcode = barcode_row[0]
                    site_names.add("BrooksHouse match")
                lines.append({
                    "line_number": line["order_item_id"],
                    "sku": line["seller_sku"],
                    "product_barcode": barcode,
                    "item_name": product_name or line["title"] or line["asin"] or "Amazon item",
                    "quantity": int(line["quantity_ordered"] or 0),
                    "line_total": float(line["item_total"] or 0),
                    "product_id": line["product_id"],
                })
            raw_status = str(header["fulfillment_status"] or "unshipped").strip().lower()
            local_status = {
                "shipped": "shipped", "cancelled": "cancelled",
                "canceled": "cancelled", "unshipped": "new",
                "partiallyshipped": "pulling", "partially_shipped": "pulling",
            }.get(raw_status.replace(" ", ""), raw_status or "new")
            created = header["created_time"]
            orders.append({
                "channel": "Amazon",
                "channel_key": "amazon",
                "purchase_order_id": header["amazon_order_id"],
                "customer_order_id": header["amazon_order_id"],
                "order_date": created or "",
                "order_datetime": _parse_datetime(created),
                "order_date_display": _display_datetime(created),
                "order_age": _age(created),
                "item_count": int(header["item_count"] or len(lines)),
                "unit_count": int(header["unit_count"] or sum(x["quantity"] for x in lines)),
                "order_total": float(header["order_total"] or 0),
                "estimated_profit": 0.0,
                "currency": header["currency_code"] or "USD",
                "local_status": local_status,
                "marketplace_status": raw_status,
                "lines": lines,
                "site_names": sorted(site_names),
                "channel_order_url": "/reports/channel-performance/amazon-order/" + str(header["amazon_order_id"]),
            })
    return orders


def load_marketplace_orders():
    """Return one shared shape for connected fulfillment channels."""
    orders = []
    for walmart_order in load_order_desk():
        order = dict(walmart_order)
        order["channel"] = "Walmart"
        order["channel_key"] = "walmart"
        order["channel_order_url"] = "/channels/walmart/orders/" + str(order["purchase_order_id"])
        orders.append(order)
    orders.extend(_amazon_orders())

    def sort_key(item):
        parsed = item.get("order_datetime") or _parse_datetime(item.get("order_date"))
        timestamp = parsed.timestamp() if parsed is not None else 0
        return (timestamp, str(item.get("channel") or ""), str(item.get("purchase_order_id") or ""))

    orders.sort(key=sort_key, reverse=True)
    for sequence, order in enumerate(orders, start=1):
        order["marketplace_sequence"] = sequence
    return orders


def marketplace_summary(orders):
    cancelled = [o for o in orders if str(o.get("local_status") or "").lower() == "cancelled"]
    completed_statuses = {"shipped", "completed"}
    completed = [o for o in orders if str(o.get("local_status") or "").lower() in completed_statuses]
    open_orders = [o for o in orders if o not in cancelled and o not in completed]
    reportable = [o for o in orders if o not in cancelled]
    channels = sorted({o.get("channel") for o in orders if o.get("channel")})
    channel_counts = {channel: sum(1 for o in orders if o.get("channel") == channel) for channel in channels}
    return {
        "total_orders": len(orders),
        "open_orders": len(open_orders),
        "completed_orders": len(completed),
        "cancelled_orders": len(cancelled),
        "total_units": sum(int(o.get("unit_count") or 0) for o in reportable),
        "gross_sales": sum(float(o.get("order_total") or 0) for o in reportable),
        "estimated_profit": sum(float(o.get("estimated_profit") or 0) for o in reportable),
        "channels": channels,
        "channel_counts": channel_counts,
    }
