"""Fast, read-only alerts from locally saved Walmart opportunity data."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.marketplace_publish import walmart_opportunities


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return Decimal(default)


@dataclass(frozen=True)
class HotItemThresholds:
    minimum_price: Decimal
    hot_profit: Decimal
    money_profit: Decimal
    jackpot_profit: Decimal
    jackpot_price: Decimal
    reliable_margin: Decimal

    @classmethod
    def from_environment(cls) -> "HotItemThresholds":
        return cls(
            minimum_price=_decimal_env("HOT_ITEM_MIN_WALMART_PRICE", "15.00"),
            hot_profit=_decimal_env("HOT_ITEM_MIN_PROFIT", "5.00"),
            money_profit=_decimal_env("HOT_ITEM_MONEY_PROFIT", "10.00"),
            jackpot_profit=_decimal_env("HOT_ITEM_JACKPOT_PROFIT", "20.00"),
            jackpot_price=_decimal_env("HOT_ITEM_JACKPOT_PRICE", "30.00"),
            reliable_margin=_decimal_env("HOT_ITEM_MIN_RELIABLE_MARGIN", "20.00"),
        )


def _actions(product_id: int, barcode: str, location_id: int | None, container_id: str) -> list[dict[str, str]]:
    context: dict[str, Any] = {"barcode": barcode}
    if location_id:
        context["location_id"] = location_id
    if container_id:
        context["container_id"] = container_id
    adjust = "/inventory/adjust?" + urlencode(context)
    receive = "/inventory/receive?" + urlencode({
        "barcode": barcode, **({"location_id": location_id} if location_id else {})
    })
    return [
        {"label": "Found It — Count & Locate", "url": adjust, "kind": "primary"},
        {"label": "Assign Tote / Container", "url": adjust, "kind": "secondary"},
        {"label": "Set Quantity", "url": receive, "kind": "secondary"},
        {"label": "Open Walmart Opportunity", "url": f"/channels/publish?product_id={product_id}", "kind": "secondary"},
    ]


def evaluate_hot_item(
    connection: sqlite3.Connection,
    product_id: int,
    barcode: str,
    *,
    location_id: int | None = None,
    container_id: str = "",
    thresholds: HotItemThresholds | None = None,
) -> dict[str, Any] | None:
    """Return alert presentation data without performing writes or network calls."""
    connection.row_factory = sqlite3.Row
    rows = walmart_opportunities(connection, sort="profit", product_id=int(product_id))
    opportunity = next((row for row in rows if int(row["product_id"]) == int(product_id)), None)
    if opportunity is None:
        return None
    price = opportunity.get("walmart_price")
    final_profit = opportunity.get("profit")
    before_fee_profit = opportunity.get("before_fee_profit")
    estimated_profit = final_profit if final_profit is not None else before_fee_profit
    reliable_margin = opportunity.get("margin") if final_profit is not None else None
    config = thresholds or HotItemThresholds.from_environment()
    if price is None or estimated_profit is None:
        return None
    if price < config.minimum_price or estimated_profit < config.hot_profit:
        return None
    if reliable_margin is not None and reliable_margin < config.reliable_margin:
        return None
    if estimated_profit >= config.jackpot_profit and price >= config.jackpot_price:
        level = "JACKPOT"
    elif estimated_profit >= config.money_profit:
        level = "MONEY_ITEM"
    else:
        level = "HOT"
    defaults = {
        "HOT": "🔥 Hot Walmart item found!",
        "MONEY_ITEM": "💰 MONEY ITEM — worth a closer look!",
        "JACKPOT": "🔥 BINGO — HOT ITEM FOUND!",
    }
    title = os.getenv(f"HOT_ITEM_{level}_TITLE", defaults[level])
    inventory_hunt = not bool(opportunity.get("confirmed_stock"))
    message = (
        os.getenv("HOT_ITEM_INVENTORY_HUNT_MESSAGE", "BrooksHouse stock was not confirmed — you just found an Inventory Hunt item.")
        if inventory_hunt else
        os.getenv("HOT_ITEM_CONFIRMED_MESSAGE", "BrooksHouse already has confirmed stock for this opportunity.")
    )
    scope = None
    if location_id or container_id:
        scope = {"location_id": location_id, "container_id": container_id}
    return {
        "level": level, "title": title, "message": message,
        "instruction": os.getenv("HOT_ITEM_ACTION_MESSAGE", "Count and assign its location now."),
        "product_id": int(product_id), "barcode": str(barcode),
        "walmart_price": float(price), "estimated_profit_per_unit": float(estimated_profit),
        "margin": float(reliable_margin) if reliable_margin is not None else None,
        "economics_reliable": final_profit is not None,
        "inventory_hunt": inventory_hunt, "known_scope": scope,
        "actions": _actions(int(product_id), str(barcode), location_id, container_id),
    }


def evaluate_hot_item_path(
    database_path: str | Path, product_id: int, barcode: str, **context: Any,
) -> dict[str, Any] | None:
    connection = sqlite3.connect(Path(database_path), timeout=5)
    try:
        return evaluate_hot_item(connection, product_id, barcode, **context)
    finally:
        connection.close()
