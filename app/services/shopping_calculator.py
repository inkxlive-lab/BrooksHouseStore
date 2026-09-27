"""Deterministic, database-free shopping cart calculations.

Shopping Calculator is deliberately separate from inventory and product
models.  It accepts plain dictionaries so the browser can use it offline and
the API/tests can use the exact same money rules.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: Any) -> Decimal:
    """Convert user input to cents using decimal arithmetic."""
    if value in (None, ""):
        return ZERO
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Money values must be numeric.") from exc
    if not amount.is_finite() or amount < ZERO:
        raise ValueError("Money values must be finite and non-negative.")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _quantity(value: Any) -> int:
    try:
        quantity = int(value or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("Quantity must be a positive whole number.") from exc
    if quantity < 1:
        raise ValueError("Quantity must be a positive whole number.")
    return quantity


def calculate_cart(items: Iterable[dict[str, Any]], tax_rate: Any = 0,
                   cart_discount: Any = 0) -> dict[str, Any]:
    """Calculate a cart with line discounts and optional cart discount.

    Prices and discounts are rounded to cents half-up.  Line discount is a
    per-unit coupon, so it is multiplied by quantity and capped at the gross
    line total.  Cart discount is allocated proportionally across taxable and
    non-taxable net lines before tax, making the result deterministic.
    """
    rate = Decimal(str(tax_rate or 0))
    if not rate.is_finite() or rate < ZERO:
        raise ValueError("Tax rate must be finite and non-negative.")
    normalized: list[dict[str, Any]] = []
    gross_total = ZERO
    item_discount_total = ZERO
    taxable_before_cart = ZERO
    non_taxable_before_cart = ZERO
    item_count = 0
    for raw in items:
        unit_price = money(raw.get("unit_price", raw.get("price")))
        quantity = _quantity(raw.get("quantity", 1))
        discount_per_unit = money(raw.get("discount", 0))
        gross = (unit_price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
        item_discount = min((discount_per_unit * quantity).quantize(CENT, rounding=ROUND_HALF_UP), gross)
        net = gross - item_discount
        taxable = bool(raw.get("taxable", True))
        if taxable:
            taxable_before_cart += net
        else:
            non_taxable_before_cart += net
        gross_total += gross
        item_discount_total += item_discount
        item_count += quantity
        normalized.append({
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or "Unknown Item"),
            "unit_price": unit_price,
            "quantity": quantity,
            "discount": discount_per_unit,
            "gross_total": gross,
            "item_discount": item_discount,
            "net_total": net,
            "taxable": taxable,
        })

    requested_cart_discount = money(cart_discount)
    net_total = gross_total - item_discount_total
    applied_cart_discount = min(requested_cart_discount, net_total)
    if net_total:
        taxable_cart_discount = (applied_cart_discount * taxable_before_cart / net_total).quantize(CENT, rounding=ROUND_HALF_UP)
    else:
        taxable_cart_discount = ZERO
    taxable_subtotal = max(ZERO, taxable_before_cart - taxable_cart_discount)
    non_taxable_subtotal = max(ZERO, non_taxable_before_cart - (applied_cart_discount - taxable_cart_discount))
    subtotal = taxable_subtotal + non_taxable_subtotal
    estimated_tax = (taxable_subtotal * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    estimated_total = subtotal + estimated_tax
    return {
        "items": normalized,
        "item_count": item_count,
        "gross_total": gross_total,
        "item_discount_total": item_discount_total,
        "cart_discount": applied_cart_discount,
        "discount_total": item_discount_total + applied_cart_discount,
        "subtotal": subtotal,
        "taxable_subtotal": taxable_subtotal,
        "non_taxable_subtotal": non_taxable_subtotal,
        "tax_rate": rate,
        "estimated_tax": estimated_tax,
        "estimated_total": estimated_total,
    }


def jsonable(result: dict[str, Any]) -> dict[str, Any]:
    """Return calculation values in JSON-safe string form for API clients."""
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value.quantize(CENT, rounding=ROUND_HALF_UP))
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value
    return convert(result)
