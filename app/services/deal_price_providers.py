"""Provider-neutral price and marketplace intelligence for Deal Scanner.

Adapters return observations; the scanner never treats a missing provider as a
zero price.  Retailer credentials and location context are intentionally
configuration concerns, not route/template concerns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


UNAVAILABLE = "UNAVAILABLE"
ELIGIBILITY_STATES = {
    "ELIGIBLE", "RESTRICTED", "ALREADY_LISTED", "NEEDS_APPROVAL",
    "UNKNOWN", "NOT_CONFIGURED",
}


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        value = float(value)
        return round(value, 2) if value >= 0 else None
    except (TypeError, ValueError):
        return None


def normalize_observation(raw: dict[str, Any], *, source: str | None = None,
                          exact_match: bool = False, local: bool = False,
                          context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one provider observation without filling unknown values."""
    price = _number(raw.get("price") or raw.get("sale_price"))
    sale_price = _number(raw.get("sale_price"))
    return {
        "source": source or raw.get("source") or "unknown",
        "retailer": raw.get("retailer") or raw.get("merchant") or source,
        "product_name": raw.get("product_name") or raw.get("title"),
        "identifier": raw.get("identifier") or raw.get("upc") or raw.get("ean"),
        "exact_match": bool(exact_match or raw.get("exact_match")),
        "match_confidence": round(float(raw.get("match_confidence", 1.0 if exact_match else 0.5)), 4),
        "price": price,
        "sale_price": sale_price,
        "unit_or_pack": raw.get("unit_or_pack") or raw.get("quantity_or_pack_count"),
        "size": raw.get("size") or raw.get("size_value"),
        "quantity_or_pack_count": raw.get("quantity_or_pack_count") or raw.get("pack_quantity"),
        "availability": raw.get("availability"),
        "local": bool(local or raw.get("local")),
        "location": context or raw.get("location"),
        "url": raw.get("url") or raw.get("link"),
        "timestamp": raw.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "shipping": raw.get("shipping") or raw.get("shipping_context"),
        "seller": raw.get("seller"),
        "channel": raw.get("channel") or ("retail" if not raw.get("marketplace") else "marketplace"),
    }


def observations_from_upc_lookup(result: dict[str, Any] | None, *, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Adapt the existing UPCitemdb/Open Facts contract into observations."""
    if not result:
        return []
    observations = []
    for offer in result.get("offers") or []:
        if not isinstance(offer, dict) or _number(offer.get("price")) is None:
            continue
        observation = normalize_observation(
            {**offer, "identifier": result.get("barcode"), "product_name": result.get("title")},
            source=result.get("source"), exact_match=True, local=False, context=location,
        )
        if _variant_conflict(result, observation):
            continue
        observations.append(observation)
    low = _number(result.get("price_low"))
    if not observations and low is not None:
        observations.append(normalize_observation(
            {"price": low, "identifier": result.get("barcode"), "product_name": result.get("title"),
             "availability": "REFERENCE_RANGE"},
            source=result.get("source"), exact_match=True, context=location,
        ))
    return observations


def _variant_conflict(identity: dict[str, Any], observation: dict[str, Any]) -> bool:
    for left_keys, right_key in (("size", "size"), ("quantity_or_pack_count", "quantity_or_pack_count")):
        left = identity.get(left_keys)
        right = observation.get(right_key)
        if left not in (None, "") and right not in (None, ""):
            if "".join(ch for ch in str(left).lower() if ch.isalnum()) != "".join(ch for ch in str(right).lower() if ch.isalnum()):
                return True
    return False


def unavailable_retailers(names: Iterable[str], *, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [{"source": name, "retailer": name, "status": UNAVAILABLE,
             "local": bool(location), "location": location, "observations": []} for name in names]


def marketplace_statuses(configured: dict[str, str] | None = None) -> list[dict[str, Any]]:
    configured = configured or {}
    return [{"channel": channel, "eligibility": configured.get(channel, "NOT_CONFIGURED"),
             "pricing_separate": True, "reason": "No eligibility integration result" if channel not in configured else None}
            for channel in ("Walmart", "Amazon", "eBay")]


def build_price_intelligence(external: dict[str, Any] | None, *, location: dict[str, Any] | None = None,
                             configured_marketplaces: dict[str, str] | None = None) -> dict[str, Any]:
    observations = observations_from_upc_lookup(external, location=location)
    return {
        "observations": observations,
        "local_observations": [item for item in observations if item["local"]],
        "online_observations": [item for item in observations if not item["local"]],
        "lowest_observed_price": min((item["sale_price"] or item["price"] for item in observations), default=None),
        "retailers": unavailable_retailers(("Walmart", "Target", "Home Depot", "Lowe's", "Walgreens/CVS"), location=location),
        "marketplaces": marketplace_statuses(configured_marketplaces),
        "location": location,
        "provider_status": "connected" if observations else "unavailable",
    }
