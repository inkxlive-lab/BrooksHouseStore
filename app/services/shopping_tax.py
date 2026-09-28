"""Read-only estimated sales-tax resolution for Shopping Calculator.

This is intentionally a boundary, not a tax-law engine.  Rates can be supplied
by a deployment-owned JSON map (for example, maintained from an approved local
source) and manual overrides are always explicit percentages.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any


def _rate(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and Decimal("0") <= result <= Decimal("100") else None


def _configured_rates() -> dict[str, Decimal]:
    raw = os.getenv("BROOKSHOUSE_TAX_RATES_JSON", "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {str(key).strip().upper(): rate for key, value in values.items() if (rate := _rate(value)) is not None}


def resolve_tax(location: dict[str, Any] | None = None, *, manual_rate: Any = None) -> dict[str, Any]:
    """Resolve a transparent estimate without network calls or data mutation."""
    location = location or {}
    if manual_rate not in (None, ""):
        rate = _rate(manual_rate)
        if rate is None:
            raise ValueError("Manual tax override must be a percentage from 0 to 100.")
        return {"rate": str(rate), "source": "manual_override", "method": "MANUAL_OVERRIDE",
                "confidence": "operator", "status": "MANUAL OVERRIDE", "manual_override": True}

    rates = _configured_rates()
    keys = [str(location.get("postal_code") or "").strip().upper(),
            str(location.get("store_name") or "").strip().upper(),
            str(location.get("state") or "").strip().upper()]
    for key in keys:
        if key and key in rates:
            return {"rate": str(rates[key]), "source": "BROOKSHOUSE_TAX_RATES_JSON",
                    "method": "CONFIGURED_JURISDICTION_ESTIMATE", "confidence": "configured",
                    "status": "AUTO", "manual_override": False}
    if location.get("postal_code") or location.get("store_name") or location.get("latitude"):
        return {"rate": "0", "source": "unavailable", "method": "NO_APPROVED_RATE_SOURCE",
                "confidence": "unknown", "status": "LOCATION RATE NEEDED", "manual_override": False,
                "note": "No approved local rate source is configured; tax remains an estimate until selected or overridden."}
    return {"rate": "0", "source": "unavailable", "method": "LOCATION_REQUIRED",
            "confidence": "unknown", "status": "LOCATION NEEDED", "manual_override": False,
            "note": "Allow location or select a store/ZIP to estimate tax."}
