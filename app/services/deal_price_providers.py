"""Read-only, provider-neutral price and marketplace intelligence.

This module adapts existing BrooksHouse data and the existing UPC lookup. It
never treats a missing provider as a zero price and never upgrades a generic
online observation into a local-store observation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import inspect, text

PROVIDER_STATES = {"AVAILABLE", "NOT_CONFIGURED", "AUTH_REQUIRED", "UNSUPPORTED", "NO_MATCH", "NO_PRICE", "PROVIDER_ERROR"}
SELLABILITY_STATES = {"ELIGIBLE", "LIKELY_ELIGIBLE", "RESTRICTED", "NOT_ELIGIBLE", "ALREADY_LISTED", "AUTH_REQUIRED", "NOT_CONFIGURED", "UNKNOWN"}
LOCATION_LOCAL = "LOCAL_STORE"
LOCATION_ONLINE = "ONLINE"
LOCATION_MARKETPLACE = "MARKETPLACE"
LOCATION_UNKNOWN = "UNKNOWN_LOCATION"


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        amount = float(value)
        return round(amount, 2) if amount >= 0 else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merchant_marketplace(raw: dict[str, Any]) -> str | None:
    value = " ".join(str(raw.get(key) or "") for key in ("marketplace", "merchant", "seller", "url", "link")).casefold()
    for name in ("walmart", "amazon", "ebay"):
        if name in value:
            return name.title()
    return None


def normalize_observation(raw: dict[str, Any], *, source: str | None = None,
                          exact_match: bool = False, local: bool = False,
                          context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one observation while preserving unknown location/price."""
    marketplace = _merchant_marketplace(raw)
    explicit_location = _text(raw.get("location_type"))
    if explicit_location in {LOCATION_LOCAL, LOCATION_ONLINE, LOCATION_MARKETPLACE, LOCATION_UNKNOWN}:
        location_type = explicit_location
    elif marketplace:
        location_type = LOCATION_MARKETPLACE
    elif local:
        location_type = LOCATION_LOCAL
    else:
        location_type = LOCATION_ONLINE
    return {
        "retailer": _text(raw.get("retailer") or raw.get("merchant") or marketplace or source),
        "price": _number(raw.get("price")),
        "sale_price": _number(raw.get("sale_price")),
        "currency": _text(raw.get("currency") or raw.get("price_currency")),
        "availability": _text(raw.get("availability")),
        "location_type": location_type,
        "local": location_type == LOCATION_LOCAL,
        "store_name": _text(raw.get("store_name")),
        "store_id": _text(raw.get("store_id")),
        "postal_code": _text(raw.get("postal_code") or (context or {}).get("postal_code")),
        "url": _text(raw.get("url") or raw.get("link")),
        "observed_at": _text(raw.get("observed_at") or raw.get("timestamp")) or _now(),
        "source": _text(source or raw.get("source")) or "unknown",
        "confidence": round(float(raw.get("confidence", raw.get("match_confidence", 1.0 if exact_match else 0.5))), 4),
        "product_name": _text(raw.get("product_name") or raw.get("title")),
        "identifier": _text(raw.get("identifier") or raw.get("upc") or raw.get("ean")),
        "exact_match": bool(exact_match or raw.get("exact_match")),
        "unit_or_pack": raw.get("unit_or_pack") or raw.get("quantity_or_pack_count"),
        "size": raw.get("size") or raw.get("size_value"),
        "quantity_or_pack_count": raw.get("quantity_or_pack_count") or raw.get("pack_quantity"),
        "shipping": raw.get("shipping") or raw.get("shipping_context"),
        "seller": _text(raw.get("seller")),
        "channel": "marketplace" if location_type == LOCATION_MARKETPLACE else "retail",
    }


def _variant_conflict(identity: dict[str, Any], observation: dict[str, Any]) -> bool:
    for key in ("size", "quantity_or_pack_count"):
        left, right = identity.get(key), observation.get(key)
        if left not in (None, "") and right not in (None, ""):
            clean = lambda value: "".join(ch for ch in str(value).casefold() if ch.isalnum())
            if clean(left) != clean(right):
                return True
    return False


def observations_from_upc_lookup(result: dict[str, Any] | None, *, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Adapt the existing UPCitemdb/Open Facts contract into observations."""
    if not result:
        return []
    observations: list[dict[str, Any]] = []
    for offer in result.get("offers") or []:
        if not isinstance(offer, dict) or _number(offer.get("price")) is None:
            continue
        observation = normalize_observation(
            {**offer, "identifier": result.get("barcode"), "product_name": result.get("title"),
             "currency": offer.get("currency") or result.get("currency")},
            source=result.get("source"), exact_match=True, context=location,
        )
        if not _variant_conflict(result, observation):
            observations.append(observation)
    low = _number(result.get("price_low"))
    if not observations and low is not None:
        observations.append(normalize_observation(
            {"price": low, "identifier": result.get("barcode"), "product_name": result.get("title"),
             "availability": "REFERENCE_RANGE", "currency": result.get("currency")},
            source=result.get("source"), exact_match=True, context=location,
        ))
    return observations


def _table_columns(database, table: str) -> set[str]:
    try:
        return {column["name"] for column in inspect(database.get_bind()).get_columns(table)}
    except Exception:
        return set()


def _configured(*names: str) -> bool:
    return all(os.getenv(name, "").strip() for name in names)


def _walmart_catalog_observations(database, identifier: str, *, location: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
    table = "walmart_catalog_matches"
    columns = _table_columns(database, table)
    if not {"barcode_lookup", "match_status"}.issubset(columns):
        return [], "NOT_CONFIGURED", None
    selected = {name: name if name in columns else "NULL" for name in ("match_status", "walmart_item_id", "title", "brand", "price_amount", "price_currency", "checked_at", "updated_at", "error_message")}
    exact, lookup = str(identifier).strip(), str(identifier).strip().lstrip("0") or "0"
    query = text(f"""SELECT {selected['match_status']} match_status, {selected['walmart_item_id']} walmart_item_id,
        {selected['title']} title, {selected['brand']} brand, {selected['price_amount']} price_amount,
        {selected['price_currency']} price_currency, {selected['checked_at']} checked_at,
        {selected['updated_at']} updated_at, {selected['error_message']} error_message FROM {table}
        WHERE TRIM(CAST(barcode_lookup AS TEXT)) IN (:exact, :lookup)
        ORDER BY {selected['updated_at']} DESC LIMIT 1""")
    try:
        row = database.execute(query, {"exact": exact, "lookup": lookup}).mappings().first()
    except Exception:
        return [], "PROVIDER_ERROR", None
    if not row:
        return [], "NO_MATCH", None
    status = str(row["match_status"] or "UNKNOWN").upper()
    if status not in {"MATCH", "MATCHED", "FOUND"}:
        return [], "NO_MATCH", {"status": status, "error": row["error_message"]}
    price = _number(row["price_amount"])
    if price is None:
        return [], "NO_PRICE", {"status": status, "title": row["title"]}
    item_id = _text(row["walmart_item_id"])
    return [normalize_observation({
        "retailer": "Walmart.com", "price": price, "currency": row["price_currency"],
        "availability": "MATCHED_CATALOG", "location_type": LOCATION_ONLINE,
        "url": f"https://www.walmart.com/ip/{item_id}" if item_id else None,
        "observed_at": row["checked_at"] or row["updated_at"], "identifier": exact,
        "product_name": row["title"],
    }, source="Walmart Catalog", exact_match=True, context=location)], "AVAILABLE", {"status": status, "item_id": item_id, "title": row["title"]}


def _listing_observations(database, identifier: str, *, channel: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not (_table_columns(database, "channel_listings") and _table_columns(database, "sales_channels")):
        return [], None
    columns = _table_columns(database, "channel_listings")
    if not {"barcode_lookup", "listed_price"}.issubset(columns):
        return [], None
    selected = {name: name if name in columns else "NULL" for name in ("listing_title", "listed_price", "listing_status", "external_product_id", "last_imported_at")}
    query = text(f"""SELECT {selected['listing_title']} title, {selected['listed_price']} price,
        {selected['listing_status']} listing_status, {selected['external_product_id']} item_id,
        {selected['last_imported_at']} observed_at FROM channel_listings cl JOIN sales_channels sc ON sc.channel_id=cl.channel_id
        WHERE LOWER(sc.channel_name)=:channel AND TRIM(CAST(cl.barcode_lookup AS TEXT)) IN (:exact,:lookup)
        ORDER BY {selected['last_imported_at']} DESC LIMIT 1""")
    try:
        row = database.execute(query, {"channel": channel, "exact": str(identifier), "lookup": str(identifier).lstrip("0") or "0"}).mappings().first()
    except Exception:
        return [], None
    if not row:
        return [], None
    price = _number(row["price"])
    metadata = {"already_listed": True, "status": row["listing_status"] or "UNKNOWN", "price": price}
    if price is None:
        return [], metadata
    return [normalize_observation({
        "retailer": channel.title(), "price": price, "location_type": LOCATION_MARKETPLACE,
        "availability": row["listing_status"] or "LISTED", "identifier": identifier,
        "product_name": row["title"], "observed_at": row["observed_at"],
    }, source=f"BrooksHouse {channel.title()} listing", exact_match=True)], metadata


def _marketplace_statuses(database, identifier: str, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses = []
    for channel, configured in (("Walmart", _configured("WALMART_CLIENT_ID", "WALMART_CLIENT_SECRET")),
                                ("Amazon", _configured("AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN")),
                                ("eBay", False)):
        listing = None
        try:
            _, listing = _listing_observations(database, identifier, channel=channel.casefold())
        except Exception:
            pass
        linked = [item for item in observations if item.get("location_type") == LOCATION_MARKETPLACE and str(item.get("retailer") or "").casefold().startswith(channel.casefold())]
        if listing and listing.get("already_listed"):
            eligibility = "ALREADY_LISTED"
        elif channel == "eBay":
            eligibility = "NOT_CONFIGURED"
        elif configured:
            eligibility = "UNKNOWN"
        else:
            eligibility = "NOT_CONFIGURED"
        statuses.append({"channel": channel, "eligibility": eligibility, "pricing_separate": True,
                         "observations": linked, "reason": "Seller eligibility is not established by market pricing" if eligibility == "UNKNOWN" else None})
    return statuses


def unavailable_retailers(names: Iterable[str], *, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [{"source": name, "retailer": name, "status": "NOT_CONFIGURED", "location_type": LOCATION_UNKNOWN,
             "local": False, "location": location, "observations": []} for name in names]


def marketplace_statuses(configured: dict[str, str] | None = None) -> list[dict[str, Any]]:
    configured = configured or {}
    return [{"channel": channel, "eligibility": configured.get(channel, "NOT_CONFIGURED"), "pricing_separate": True,
             "observations": [], "reason": "No eligibility integration result" if channel not in configured else None}
            for channel in ("Walmart", "Amazon", "eBay")]


def build_price_intelligence(external: dict[str, Any] | None, *, database=None, identifier: str | None = None,
                             location: dict[str, Any] | None = None, configured_marketplaces: dict[str, str] | None = None) -> dict[str, Any]:
    observations = observations_from_upc_lookup(external, location=location)
    provider_states = [{"provider": external.get("source", "UPCitemdb") if external else "UPCitemdb",
                        "status": "AVAILABLE" if observations else ("PROVIDER_ERROR" if external and external.get("error") else "NO_PRICE")}]
    walmart_meta = None
    if database is not None and identifier:
        walmart, state, walmart_meta = _walmart_catalog_observations(database, identifier, location=location)
        observations.extend(walmart)
        provider_states.append({"provider": "Walmart Catalog", "status": state})
        for channel in ("walmart", "amazon"):
            listing_rows, _ = _listing_observations(database, identifier, channel=channel)
            observations.extend(listing_rows)
    marketplaces = _marketplace_statuses(database, identifier, observations) if database is not None and identifier else marketplace_statuses(configured_marketplaces)
    for item in marketplaces:
        if item["eligibility"] == "NOT_CONFIGURED":
            provider_states.append({"provider": item["channel"], "status": "NOT_CONFIGURED"})
    prices = [(item.get("sale_price") or item.get("price")) for item in observations if item.get("exact_match") and (item.get("sale_price") or item.get("price")) is not None]
    return {
        "observations": observations,
        "local_observations": [item for item in observations if item["location_type"] == LOCATION_LOCAL],
        "online_observations": [item for item in observations if item["location_type"] == LOCATION_ONLINE],
        "marketplace_observations": [item for item in observations if item["location_type"] == LOCATION_MARKETPLACE],
        "lowest_observed_price": min(prices, default=None),
        "retailers": unavailable_retailers(("Target", "Home Depot", "Lowe's", "Walgreens/CVS"), location=location),
        "marketplaces": marketplaces, "provider_states": provider_states, "walmart_catalog": walmart_meta,
        "location": location, "provider_status": "connected" if observations else "unavailable",
    }
