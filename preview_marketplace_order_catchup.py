#!/usr/bin/env python
"""Read-only preview of marketplace orders that a catch-up would import."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from amazon_order_history_sync import (
    AmazonClient, extract_orders, first_env, get_fulfilled_by, get_status, load_env,
)
from app.database_resolution import configured_sqlite_path, connect_sqlite_read_only
from app.services.marketplace_order_ingestion import qualify_order
from app.walmart_order_service import _extract_orders, _order_status, walmart_request


def main() -> int:
    load_env(Path(".env"))
    with connect_sqlite_read_only(configured_sqlite_path(), require_application_match=True) as connection:
        walmart_saved = {str(row[0]) for row in connection.execute("SELECT purchase_order_id FROM walmart_orders")}
        amazon_saved = {str(row[0]) for row in connection.execute("SELECT amazon_order_id FROM amazon_order_history")}

    start = (datetime.now().astimezone() - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    walmart = _extract_orders(walmart_request("GET", "/v3/orders", params={
        "createdStartDate": start.isoformat(), "limit": 200,
    }))
    amazon_client = AmazonClient(
        first_env("AMAZON_LWA_CLIENT_ID", "SP_API_CLIENT_ID"),
        first_env("AMAZON_LWA_CLIENT_SECRET", "SP_API_CLIENT_SECRET"),
        first_env("AMAZON_REFRESH_TOKEN", "SP_API_REFRESH_TOKEN"),
    )
    amazon, _ = extract_orders(amazon_client.search_orders(
        marketplace_id=first_env("AMAZON_MARKETPLACE_ID", "SP_API_MARKETPLACE_ID"),
        created_after=(datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ))
    rows = []
    for order in walmart:
        order_id = str(order.get("purchaseOrderId") or "")
        status = _order_status(order)
        rows.append({"channel": "walmart", "order_id": order_id, "created_at": order.get("orderDate"),
                     "status": status, "already_saved": order_id in walmart_saved,
                     "open_seller_action": qualify_order("walmart", status)})
    for order in amazon:
        order_id = str(order.get("orderId") or order.get("amazonOrderId") or "")
        status, fulfilled_by = get_status(order), get_fulfilled_by(order)
        rows.append({"channel": "amazon", "order_id": order_id, "created_at": order.get("createdTime"),
                     "status": status, "fulfilled_by": fulfilled_by,
                     "already_saved": order_id in amazon_saved,
                     "open_seller_action": qualify_order("amazon", status, fulfilled_by)})
    print(json.dumps({"database": str(configured_sqlite_path()), "read_only": True,
                      "would_import": [row for row in rows if not row["already_saved"]],
                      "already_saved": [row for row in rows if row["already_saved"]]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
