"""Approved Walmart/Amazon order refresh for the fulfillment-yard report."""

from __future__ import annotations

import json
from datetime import datetime

from amazon_order_history_sync import sync_recent_orders
from app.walmart_order_service import sync_orders


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main() -> int:
    started_at = timestamp()
    walmart_started_at = timestamp()
    walmart = sync_orders(30, detailed=True)
    walmart_finished_at = timestamp()
    amazon_started_at = timestamp()
    amazon = sync_recent_orders(days=30)
    amazon_finished_at = timestamp()
    print(
        json.dumps(
            {
                "refresh_started_at": started_at,
                "walmart_started_at": walmart_started_at,
                "walmart_finished_at": walmart_finished_at,
                "walmart": walmart,
                "amazon_started_at": amazon_started_at,
                "amazon_finished_at": amazon_finished_at,
                "refresh_finished_at": timestamp(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
