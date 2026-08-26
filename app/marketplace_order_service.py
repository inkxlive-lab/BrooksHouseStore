from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.walmart_order_service import load_order_desk
from app.database_resolution import configured_sqlite_path, require_application_database_match


DB_PATH = configured_sqlite_path()
TERMINAL_STATUSES = {"shipped", "cancelled", "canceled", "refunded", "closed", "completed"}
STALE_ORDER_HOURS = max(1, int(os.getenv("MARKETPLACE_ACTIONABLE_VERIFY_HOURS", "6")))
CENTRAL = ZoneInfo("America/Chicago")


def _connect(database=None, *, allow_fixture=False) -> sqlite3.Connection:
    target = database or DB_PATH
    if not allow_fixture:
        target = require_application_database_match(target)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
        if abs(number) >= 100_000_000_000:
            number /= 1000.0
        return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _display_datetime(value):
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value or "Unknown")
    return parsed.astimezone(CENTRAL).strftime("%a %m/%d/%Y\n%I:%M %p CT")


def _age(value):
    parsed = _parse_datetime(value)
    if parsed is None:
        return ""
    now = datetime.now(CENTRAL)
    parsed = parsed.astimezone(CENTRAL)
    seconds = max(0, int((now - parsed).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    return f"{days}d {hours}h old"


def _safe_timestamp(value, default=float("inf")):
    parsed = value if isinstance(value, datetime) else _parse_datetime(value)
    if parsed is None:
        return default
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return default


def _is_past(value) -> bool:
    timestamp = _safe_timestamp(value)
    return timestamp != float("inf") and timestamp < datetime.now().astimezone().timestamp()


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _amazon_orders(database=None, *, allow_fixture=False):
    orders = []
    with _connect(database, allow_fixture=allow_fixture) as connection:
        if not (_table_exists(connection, "amazon_order_history") and
                _table_exists(connection, "amazon_order_item_history")):
            return orders
        header_rows = connection.execute(
            """
            SELECT amazon_order_id, created_time, fulfillment_status,
                   order_total, currency_code, item_count, unit_count,
                   local_status, last_verified_at, channel_closed_at, terminal_reason, ship_by_date
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
                inventory_options = []
                if line["product_id"] is not None:
                    inventory_options = [dict(row) for row in connection.execute(
                        """SELECT i.inventory_id,i.quantity_on_hand,i.quantity_reserved,i.container_id,
                                  l.location_name,l.location_type
                           FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
                           WHERE i.product_id=? AND i.quantity_on_hand>0
                           ORDER BY l.location_name,i.container_id""", (line["product_id"],)
                    ).fetchall()]
                    for option in inventory_options:
                        option["available_quantity"] = max(0, int(option.get("quantity_on_hand") or 0) - int(option.get("quantity_reserved") or 0))
                        option["site_name"] = option.get("location_name") or "Unknown site"
                        site_names.add(option["site_name"])
                lines.append({
                    "line_number": line["order_item_id"],
                    "sku": line["seller_sku"],
                    "product_barcode": barcode,
                    "item_name": product_name or line["title"] or line["asin"] or "Amazon item",
                    "quantity": int(line["quantity_ordered"] or 0),
                    "line_total": float(line["item_total"] or 0),
                    "product_id": line["product_id"],
                    "confirmed_product_id": line["product_id"],
                    "mapping_status": "mapped" if line["product_id"] is not None else "unmatched",
                    "candidate_inventory_found": False,
                    "inventory_options": inventory_options,
                    "pulled_quantity": 0,
                    "image_url": None,
                })
            raw_status = str(header["fulfillment_status"] or "unshipped").strip().lower()
            derived_local_status = {
                "shipped": "shipped", "cancelled": "cancelled",
                "canceled": "cancelled", "unshipped": "new",
                "partiallyshipped": "pulling", "partially_shipped": "pulling",
            }.get(raw_status.replace(" ", ""), raw_status or "new")
            local_status = str(header["local_status"] or derived_local_status)
            mapped = bool(lines) and all(line["mapping_status"] == "mapped" for line in lines)
            enough_inventory = mapped and all(
                sum(int(option["available_quantity"]) for option in line["inventory_options"])
                >= int(line["quantity"] or 0)
                for line in lines
            )
            created = header["created_time"]
            orders.append({
                "channel": "Amazon",
                "channel_key": "amazon",
                "purchase_order_id": header["amazon_order_id"],
                "customer_order_id": header["amazon_order_id"],
                "order_date": created or "",
                "ship_by_date": header["ship_by_date"] or "",
                "ship_by_display": _display_datetime(header["ship_by_date"]),
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
                "last_verified_at": header["last_verified_at"],
                "channel_closed_at": header["channel_closed_at"],
                "terminal_reason": header["terminal_reason"],
                "lines": lines,
                "site_names": sorted(site_names),
                "inventory_state": "ready" if enough_inventory else ("mapping_required" if not mapped else "insufficient"),
                "stage_picked": local_status in {"picked", "packed", "staged", "shipment_submitted", "shipped"},
                "stage_packed": local_status in {"packed", "staged", "shipment_submitted", "shipped"},
                "stage_staged": local_status in {"staged", "shipment_submitted", "shipped"},
                "stage_shipped": local_status == "shipped",
                "channel_order_url": "/reports/channel-performance/amazon-order/" + str(header["amazon_order_id"]),
            })
    return orders


def load_marketplace_orders(database=None, *, allow_fixture=False):
    """Return one shared shape for connected fulfillment channels."""
    orders = []
    for walmart_order in load_order_desk(database, allow_fixture=allow_fixture):
        order = dict(walmart_order)
        order["channel"] = "Walmart"
        order["channel_key"] = "walmart"
        order["channel_order_url"] = "/channels/walmart/orders/" + str(order["purchase_order_id"])
        orders.append(order)
    orders.extend(_amazon_orders(database, allow_fixture=allow_fixture))

    with _connect(database, allow_fixture=allow_fixture) as connection:
        if _table_exists(connection, "marketplace_order_alerts"):
            alerts = {
                (str(row["channel"]), str(row["marketplace_order_id"])): dict(row)
                for row in connection.execute("SELECT * FROM marketplace_order_alerts").fetchall()
            }
            for order in orders:
                alert = alerts.get((str(order["channel_key"]), str(order["purchase_order_id"])))
                order["alert_id"] = alert.get("alert_id") if alert else None
                order["alert_state"] = alert.get("alert_state") if alert else ""
                order["is_unacknowledged"] = bool(alert and not alert.get("acknowledged_at"))

    def sort_key(item):
        parsed = item.get("order_datetime") or _parse_datetime(item.get("order_date"))
        timestamp = parsed.timestamp() if parsed is not None else 0
        return (timestamp, str(item.get("channel") or ""), str(item.get("purchase_order_id") or ""))

    orders.sort(key=sort_key, reverse=True)
    for sequence, order in enumerate(orders, start=1):
        order["marketplace_sequence"] = sequence
        local = str(order.get("local_status") or "new").casefold()
        market = str(order.get("marketplace_status") or order.get("walmart_status") or "").casefold()
        market_token = market.replace("_", "").replace(" ", "")
        order["is_terminal"] = local in TERMINAL_STATUSES or market_token in {
            "shipped", "cancelled", "canceled", "refunded", "closed", "completed", "complete", "delivered"
        }
        verified = _parse_datetime(order.get("last_verified_at") or order.get("synced_at"))
        order["verification_stale"] = bool(
            not order["is_terminal"] and (
                verified is None or datetime.now().astimezone() - verified.astimezone() > timedelta(hours=STALE_ORDER_HOURS)
            )
        )
        order["is_actionable"] = not order["is_terminal"]
        mapping_required = any(
            str(line.get("mapping_status") or "").casefold() != "mapped"
            or line.get("confirmed_product_id") is None
            for line in order.get("lines") or []
        )
        inventory_state = str(order.get("inventory_state") or "").casefold()
        # A completed scan is still an active picking session until the
        # explicit Picked action records picked_at.  Preserve legacy `pulled`
        # rows as Picking rather than inventing a persisted Picked state.
        local_stage = "pulling" if local in {"pulling", "pulled"} else local
        if mapping_required:
            workflow_state = "mapping_required"
        elif order["verification_stale"] or inventory_state in {"missing", "insufficient"}:
            workflow_state = "exception"
        elif local_stage in {"pulling", "picked", "packed", "staged"}:
            workflow_state = local_stage
        else:
            workflow_state = "ready_to_pick"
        order["mapping_required"] = mapping_required
        order["workflow_state"] = workflow_state
        order["workflow_label"] = workflow_state.replace("_", " ").title()
        order["is_exception"] = workflow_state == "exception"
        ship_by = _parse_datetime(order.get("ship_by_date"))
        order["is_overdue"] = _is_past(ship_by)
    orders.sort(key=lambda item: (
        0 if item.get("is_overdue") else 1,
        _safe_timestamp(item.get("ship_by_date")),
        _safe_timestamp(item.get("order_date")),
    ))
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
    workflow_counts = {
        state: sum(1 for order in orders if order.get("workflow_state") == state)
        for state in ("ready_to_pick", "pulling", "picked", "staged", "mapping_required", "exception")
    }
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
        "workflow_counts": workflow_counts,
    }
