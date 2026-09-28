"""Read-only, provider-neutral price and marketplace intelligence.

This module adapts existing BrooksHouse data and the existing UPC lookup. It
never treats a missing provider as a zero price and never upgrades a generic
online observation into a local-store observation.
"""

from __future__ import annotations

import os
import json
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import inspect, text

PROVIDER_STATES = {
    "AVAILABLE", "AVAILABLE_WITH_PRICES", "AVAILABLE_IDENTITY_ONLY", "NOT_CONFIGURED",
    "AUTH_REQUIRED", "UNSUPPORTED", "NO_MATCH", "NO_PRICE", "RATE_LIMITED", "RATE_LIMITED_BURST",
    "RATE_LIMITED_DAILY", "RATE_LIMITED_PROVIDER", "CACHED_FALLBACK", "TIMEOUT",
    "PROVIDER_ERROR", "PARSE_ERROR",
}
SELLABILITY_STATES = {"ELIGIBLE", "LIKELY_ELIGIBLE", "RESTRICTED", "NOT_ELIGIBLE", "ALREADY_LISTED", "AUTH_REQUIRED", "NOT_CONFIGURED", "UNKNOWN"}
LOCATION_LOCAL = "LOCAL_STORE"
LOCATION_ONLINE = "ONLINE"
LOCATION_ONLINE_RETAIL = "ONLINE_RETAIL"
LOCATION_MARKETPLACE = "MARKETPLACE"
LOCATION_UNKNOWN = "UNKNOWN_LOCATION"
FRESHNESS_STATES = {"VERIFIED_CURRENT", "CURRENT", "STALE", "UNKNOWN"}
QUALITY_STATES = {"TRUSTED", "ACCEPTABLE", "LOW_CONFIDENCE", "OUTLIER", "UNVERIFIED"}
TRUSTED_RETAILERS = {
    "walmart", "walmart.com", "target", "home depot", "the home depot", "lowe's", "lowes",
    "ace hardware", "true value", "cvs", "walgreens", "best buy", "kroger", "kroger family",
    "amazon", "ebay",
}
RETAILER_ALIASES = {"the home depot": "Home Depot", "lowes": "Lowe's", "walmart": "Walmart.com", "wal-mart.com": "Walmart.com"}


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


def _observation_price(value: Any) -> Any:
    """Read provider price shapes without turning missing values into prices."""
    if isinstance(value, dict):
        return value.get("value") or value.get("amount") or value.get("price")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _observed_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def freshness_state(value: Any, *, now: datetime | None = None, exact_match: bool = False) -> str:
    observed = _observed_datetime(value)
    if observed is None:
        return "UNKNOWN"
    age_days = max(0.0, ((now or datetime.now(timezone.utc)) - observed).total_seconds() / 86400)
    if age_days <= 2:
        return "VERIFIED_CURRENT" if exact_match else "CURRENT"
    if age_days <= 14:
        return "CURRENT"
    return "STALE"


def retailer_trust(retailer: Any) -> str:
    name = str(retailer or "").casefold().strip()
    if name in TRUSTED_RETAILERS or name.startswith("wal-mart.com") or any(value in name for value in TRUSTED_RETAILERS if len(value) > 4):
        return "TRUSTED"
    if not name or name in {"unknown", "internet", "seller"} or name.startswith("unknown ") or name.endswith(" seller"):
        return "UNVERIFIED"
    return "ACCEPTABLE"


def _merchant_marketplace(raw: dict[str, Any]) -> str | None:
    value = " ".join(str(raw.get(key) or "") for key in ("marketplace", "channel")).casefold()
    for name in ("walmart", "amazon", "ebay"):
        if name in value:
            return name.title()
    if str(raw.get("merchant") or raw.get("retailer") or "").casefold().strip() in {"ebay", "ebay marketplace"}:
        return "Ebay"
    return None


def normalize_observation(raw: dict[str, Any], *, source: str | None = None,
                          exact_match: bool = False, local: bool = False,
                          context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one observation while preserving unknown location/price."""
    marketplace = _merchant_marketplace(raw)
    explicit_location = _text(raw.get("location_type"))
    if explicit_location in {LOCATION_LOCAL, LOCATION_ONLINE, LOCATION_ONLINE_RETAIL, LOCATION_MARKETPLACE, LOCATION_UNKNOWN}:
        location_type = explicit_location
    elif marketplace:
        location_type = LOCATION_MARKETPLACE
    elif local:
        location_type = LOCATION_LOCAL
    else:
        location_type = LOCATION_ONLINE
    retailer = _text(raw.get("retailer") or raw.get("merchant") or marketplace or source)
    observed_at = _text(raw.get("observed_at") or raw.get("timestamp")) or _now()
    exact = bool(exact_match or raw.get("exact_match"))
    item_price = _number(raw.get("sale_price") or raw.get("price"))
    shipping_price = _number(raw.get("shipping_price") or raw.get("shipping_cost"))
    if shipping_price is None and isinstance(raw.get("shipping"), dict):
        shipping_price = _number(raw["shipping"].get("price") or raw["shipping"].get("value") or raw["shipping"].get("cost"))
    delivered_price = _number(raw.get("delivered_price") or raw.get("total_price"))
    if delivered_price is None and item_price is not None and shipping_price is not None:
        delivered_price = round(item_price + shipping_price, 2)
    cache_state = _text(raw.get("cache_state"))
    observation_state = _text(raw.get("observation_state")) or ("CACHED" if cache_state else "LIVE")
    online_category = LOCATION_ONLINE_RETAIL if location_type == LOCATION_ONLINE else location_type
    return {
        "retailer": RETAILER_ALIASES.get((retailer or "").casefold(), retailer),
        "price": item_price,
        "sale_price": _number(raw.get("sale_price")),
        "currency": _text(raw.get("currency") or raw.get("price_currency")),
        "availability": _text(raw.get("availability")),
        "location_type": location_type,
        "location_category": online_category,
        "local": location_type == LOCATION_LOCAL,
        "store_name": _text(raw.get("store_name")),
        "store_id": _text(raw.get("store_id")),
        "postal_code": _text(raw.get("postal_code") or (context or {}).get("postal_code")),
        "url": _text(raw.get("url") or raw.get("link")),
        "observed_at": observed_at,
        "source": _text(source or raw.get("source")) or "unknown",
        "provider": _text(source or raw.get("source")) or "unknown",
        "confidence": round(float(raw.get("confidence", raw.get("match_confidence", 1.0 if exact else 0.5))), 4),
        "freshness_state": freshness_state(observed_at, exact_match=exact),
        "quality_state": "TRUSTED" if retailer_trust(retailer) == "TRUSTED" and exact else "ACCEPTABLE" if exact else "UNVERIFIED",
        "retailer_trust": retailer_trust(retailer),
        "product_name": _text(raw.get("product_name") or raw.get("title")),
        "identifier": _text(raw.get("identifier") or raw.get("upc") or raw.get("ean")),
        "exact_match": exact,
        "unit_or_pack": raw.get("unit_or_pack") or raw.get("quantity_or_pack_count"),
        "size": raw.get("size") or raw.get("size_value"),
        "quantity_or_pack_count": raw.get("quantity_or_pack_count") or raw.get("pack_quantity"),
        "shipping": raw.get("shipping") or raw.get("shipping_context"),
        "shipping_price": shipping_price,
        "delivered_price": delivered_price,
        "comparison_basis": "DELIVERED_PRICE" if delivered_price is not None else "ITEM_PRICE",
        "condition": _text(raw.get("condition")),
        "product_url": _text(raw.get("product_url") or raw.get("url") or raw.get("link")),
        "seller": _text(raw.get("seller")),
        "channel": "marketplace" if location_type == LOCATION_MARKETPLACE else "retail",
        "cache_state": cache_state,
        "observation_state": observation_state,
        "retrieved_at": _text(raw.get("retrieved_at")) or observed_at,
    }


def _variant_conflict(identity: dict[str, Any], observation: dict[str, Any]) -> bool:
    for key in ("size", "quantity_or_pack_count"):
        left, right = identity.get(key), observation.get(key)
        if left not in (None, "") and right not in (None, ""):
            clean = lambda value: "".join(ch for ch in str(value).casefold() if ch.isalnum())
            if clean(left) != clean(right):
                return True
    return False


def _raw_observation_rows(result: dict[str, Any] | None) -> list[Any]:
    if not result:
        return []
    rows = list(result.get("observations") or result.get("price_observations") or [])
    rows.extend(result.get("offers") or [])
    rows.extend(price if isinstance(price, dict) else {"price": price} for price in (result.get("prices") or []))
    return rows


def observations_from_upc_lookup(result: dict[str, Any] | None, *, location: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Adapt the existing UPCitemdb/Open Facts contract into observations."""
    if not result:
        return []
    observations: list[dict[str, Any]] = []
    for offer in _raw_observation_rows(result):
        if not isinstance(offer, dict) or _number(_observation_price(offer.get("price") or offer.get("sale_price") or offer.get("amount"))) is None:
            continue
        normalized_input = dict(offer)
        normalized_input["price"] = _observation_price(offer.get("price") or offer.get("sale_price") or offer.get("amount"))
        normalized_input.setdefault("currency", offer.get("currency") or result.get("currency"))
        normalized_input.setdefault("observed_at", offer.get("observed_at") or result.get("provider_observation_at") or result.get("cache_retrieved_at"))
        observation = normalize_observation(
            {**normalized_input, "identifier": offer.get("identifier") or result.get("barcode"),
             "product_name": offer.get("product_name") or offer.get("title") or result.get("title"),
             "cache_state": result.get("cache_state"),
             "observation_state": "CACHED" if result.get("cache_state") else "LIVE",
             "retrieved_at": result.get("cache_retrieved_at") or result.get("provider_observation_at")},
            source=result.get("source"), exact_match=True, context=location,
        )
        if result.get("cache_state") == "STALE":
            observation["freshness_state"] = "STALE"
        if not _variant_conflict(result, observation):
            observations.append(observation)
    low = _number(result.get("price_low"))
    if not observations and low is not None:
        observations.append(normalize_observation(
            {"price": low, "identifier": result.get("barcode"), "product_name": result.get("title"),
             "availability": "REFERENCE_RANGE", "currency": result.get("currency"),
             "cache_state": result.get("cache_state"),
             "observation_state": "CACHED" if result.get("cache_state") else "LIVE"},
            source=result.get("source"), exact_match=True, context=location,
        ))
    return observations


def provider_diagnostics(result: dict[str, Any] | None, normalized: list[dict[str, Any]] | None = None,
                         identifier: str | None = None) -> dict[str, Any]:
    """Return safe retrieval/count diagnostics without exposing provider payloads."""
    result = result or {}
    rows = _raw_observation_rows(result)
    normalized = normalized if normalized is not None else observations_from_upc_lookup(result)
    reasons: Counter[str] = Counter()
    accepted = 0
    for row in rows:
        if not isinstance(row, dict):
            reasons["MALFORMED_OFFER"] += 1
            continue
        value = _observation_price(row.get("price") or row.get("sale_price") or row.get("amount"))
        if _number(value) is None:
            reasons["MISSING_OR_INVALID_PRICE"] += 1
            continue
        if _variant_conflict(result, normalize_observation(
            {**row, "identifier": row.get("identifier") or result.get("barcode"),
             "product_name": row.get("product_name") or row.get("title") or result.get("title")},
            source=result.get("source"), exact_match=True,
        )):
            reasons["WRONG_VARIANT"] += 1
            continue
        accepted += 1
    error = str(result.get("error") or "")
    error_lower = error.casefold()
    explicit_status = str(result.get("provider_status") or "").upper()
    if explicit_status in PROVIDER_STATES:
        status = explicit_status
    elif normalized:
        # Keep the M2.3 public-page diagnostic contract while the normalized
        # observations carry the stronger price detail internally.
        status = "AVAILABLE"
    elif error:
        status = "TIMEOUT" if "timeout" in error_lower and str(result.get("source") or "") == "UPCitemdb" and not identifier else "PROVIDER_ERROR"
    elif result.get("found") or result.get("title") or result.get("images"):
        status = "AVAILABLE_IDENTITY_ONLY"
    elif any(key in result for key in ("offers", "observations", "price_observations", "prices", "price_low")):
        status = "NO_PRICE"
    else:
        status = "NO_MATCH"
    return {
        "provider": result.get("source") or "UPCitemdb",
        "status": status,
        "response_type": type(result).__name__,
        "identity_present": bool(result.get("found") or result.get("title") or result.get("images")),
        "raw_offer_count": len(rows),
        "normalized_offer_count": len(normalized),
        "rejected_offer_count": max(0, len(rows) - accepted),
        "rejection_reasons": dict(reasons),
        "error_present": bool(error),
        "duration_ms": result.get("duration_ms"),
        "http_status": result.get("http_status"),
        "provider_error_code": result.get("provider_error_code"),
        "rate_limit": result.get("rate_limit") or {},
        "cache_state": result.get("cache_state"),
        "cache_retrieved_at": result.get("cache_retrieved_at"),
    }


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
        if database is not None:
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


def ebay_browse_observations(identifier: str, *, timeout: float = 4.0) -> tuple[list[dict[str, Any]], str]:
    """Read active eBay Browse listings when an OAuth token is configured.

    This deliberately does not call the API without an explicit token and
    does not describe active listings as sold/completed-market evidence.
    """
    token = os.getenv("EBAY_OAUTH_TOKEN", "").strip()
    if not token:
        return [], "AUTH_REQUIRED"
    query = urlencode({"gtin": identifier, "limit": "20", "fieldgroups": "EXTENDED"})
    request = Request(
        f"https://api.ebay.com/buy/browse/v1/item_summary/search?{query}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "X-EBAY-C-MARKETPLACE-ID": os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except HTTPError as error:
        return [], "AUTH_REQUIRED" if error.code in {401, 403} else "PROVIDER_ERROR"
    except (URLError, TimeoutError, ValueError):
        return [], "PROVIDER_ERROR"
    observations = []
    for item in payload.get("itemSummaries") or []:
        price = item.get("price") or {}
        shipping = (item.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
        if _number(price.get("value")) is None:
            continue
        observations.append(normalize_observation({
            "retailer": "eBay", "price": price.get("value"), "currency": price.get("currency"),
            "shipping_price": shipping.get("value"), "condition": item.get("condition"),
            "availability": "ACTIVE", "location_type": LOCATION_MARKETPLACE,
            "seller": (item.get("seller") or {}).get("username"), "product_url": item.get("itemWebUrl"),
            "identifier": identifier, "title": item.get("title"), "observed_at": _now(),
        }, source="eBay Browse", exact_match=True))
    return observations, "AVAILABLE" if observations else "NO_MATCH"


def _observation_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Conservative offer identity used only for duplicate offer suppression."""
    def clean(value: Any) -> str:
        return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())
    return (
        clean(item.get("retailer")), clean(item.get("identifier")),
        clean(item.get("size")), clean(item.get("quantity_or_pack_count")),
        clean(item.get("condition")), item.get("location_type"),
        item.get("sale_price") or item.get("price"), item.get("shipping_price"), clean(item.get("store_id") or item.get("store_name")),
    )


def deduplicate_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge only the same merchant/variant/price offer across providers."""
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in observations:
        key = _observation_key(item)
        existing = merged.get(key)
        if existing is None:
            item = dict(item)
            item["sources"] = [item.get("source")] if item.get("source") else []
            merged[key] = item
            continue
        sources = existing.setdefault("sources", [])
        if item.get("source") and item["source"] not in sources:
            sources.append(item["source"])
        # A live observation is stronger than a cached copy of the same offer.
        if existing.get("observation_state") == "CACHED" and item.get("observation_state") == "LIVE":
            replacement = dict(item)
            replacement["sources"] = sources
            merged[key] = replacement
    return list(merged.values())


def rank_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank without deleting evidence; mark stale, unverified, and outlier rows."""
    ranked = [dict(item) for item in observations if _number(item.get("sale_price") or item.get("price")) is not None]
    cluster = [float(item.get("sale_price") or item.get("price")) for item in ranked
               if item.get("exact_match") and item.get("freshness_state") in {"VERIFIED_CURRENT", "CURRENT"}
               and item.get("retailer_trust") == "TRUSTED"]
    median = statistics.median(cluster) if len(cluster) >= 2 else None
    for item in ranked:
        price = float(item.get("sale_price") or item.get("price"))
        item["reference_eligible"] = bool(
            item.get("exact_match")
            and item.get("freshness_state") in {"VERIFIED_CURRENT", "CURRENT"}
            and item.get("quality_state") not in {"OUTLIER", "LOW_CONFIDENCE"}
            and item.get("retailer_trust") in {"TRUSTED", "ACCEPTABLE"}
        )
        if item.get("freshness_state") == "STALE":
            item["quality_state"] = "LOW_CONFIDENCE"
            item["reference_eligible"] = False
            item["exclusion_reason"] = "STALE_SOURCE"
        if median is not None and (price > max(median * 3, median + 10) or price < median / 4):
            item["quality_state"] = "OUTLIER"
            item["outlier_reason"] = f"Outside trusted price cluster around {median:.2f}"
            item["reference_eligible"] = False
            item["exclusion_reason"] = "OUTLIER"
        item.setdefault("quality_state", "UNVERIFIED")
        if not item["reference_eligible"] and not item.get("exclusion_reason"):
            item["exclusion_reason"] = "LOW_CONFIDENCE_OR_NOT_EXACT"
    freshness_rank = {"VERIFIED_CURRENT": 0, "CURRENT": 1, "UNKNOWN": 2, "STALE": 3}
    quality_rank = {"TRUSTED": 0, "ACCEPTABLE": 1, "UNVERIFIED": 2, "LOW_CONFIDENCE": 3, "OUTLIER": 4}
    ranked.sort(key=lambda item: (quality_rank.get(item.get("quality_state"), 5), freshness_rank.get(item.get("freshness_state"), 4), item.get("price") or 0))
    return ranked


def select_reference_price(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Select a conservative reference and expose the evidence used/excluded."""
    ranked = rank_observations(observations)
    eligible = [item for item in ranked if item.get("exact_match") and item.get("freshness_state") in {"VERIFIED_CURRENT", "CURRENT"}
                and item.get("quality_state") not in {"OUTLIER", "LOW_CONFIDENCE"}]
    trusted = [item for item in eligible if item.get("retailer_trust") == "TRUSTED"]
    # A verified exact retailer observation is still useful when no named
    # trusted retailer returned a price; unverified sellers never qualify.
    if not trusted:
        trusted = [item for item in eligible if item.get("retailer_trust") == "ACCEPTABLE"]
    prices = [float(item.get("sale_price") or item.get("price")) for item in trusted]
    if not prices:
        return {"price": None, "currency": None, "confidence": 0.0, "quality_state": "UNVERIFIED",
                "trusted_range": None, "explanation": "No current exact-match observations met the reference-quality threshold.",
                "observation_count": 0, "excluded_count": len(ranked), "ranked_observations": ranked}
    target = statistics.median(prices)
    selected = min(trusted, key=lambda item: (abs(float(item.get("sale_price") or item.get("price")) - target),
                                             0 if item.get("retailer_trust") == "TRUSTED" else 1))
    confidence = min(1.0, 0.55 + min(len(trusted), 5) * 0.08 + (0.12 if selected.get("retailer_trust") == "TRUSTED" else 0))
    excluded = len(ranked) - len(trusted)
    return {"price": round(float(selected.get("sale_price") or selected.get("price")), 2),
            "currency": selected.get("currency"), "confidence": round(confidence, 2),
            "quality_state": "TRUSTED" if selected.get("retailer_trust") == "TRUSTED" else "ACCEPTABLE",
            "trusted_range": {"low": round(min(prices), 2), "high": round(max(prices), 2)},
            "explanation": f"Based on {len(trusted)} current exact-match observation(s); excluded {excluded} stale, outlier, or lower-confidence observation(s).",
            "observation_count": len(trusted), "excluded_count": excluded, "selected_retailer": selected.get("retailer"),
            "ranked_observations": ranked}


def build_price_intelligence(external: dict[str, Any] | None, *, database=None, identifier: str | None = None,
                             location: dict[str, Any] | None = None, configured_marketplaces: dict[str, str] | None = None,
                             provider_adapters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run independent read-only price sources and return partial results.

    ``provider_adapters`` is deliberately injectable for legitimate future
    sources and deterministic tests. Each adapter receives ``identifier`` and
    ``location`` and returns either ``(rows, status)`` or a rows list.
    Provider exceptions are isolated so one outage cannot erase other prices.
    """
    observations = observations_from_upc_lookup(external, location=location)
    diagnostics = provider_diagnostics(external, observations, identifier=identifier)
    provider_states = [{"provider": diagnostics["provider"], "status": diagnostics["status"]}]
    walmart_meta = None
    ebay_future = None
    ebay_pool = ThreadPoolExecutor(max_workers=1) if identifier else None
    if ebay_pool is not None:
        ebay_future = ebay_pool.submit(ebay_browse_observations, str(identifier))
    if database is not None and identifier:
        walmart, state, walmart_meta = _walmart_catalog_observations(database, identifier, location=location)
        observations.extend(walmart)
        provider_states.append({"provider": "Walmart Catalog", "status": state})
        for channel in ("walmart", "amazon"):
            listing_rows, _ = _listing_observations(database, identifier, channel=channel)
            observations.extend(listing_rows)
    if ebay_future is not None:
        try:
            ebay_rows, ebay_state = ebay_future.result()
        except Exception:
            ebay_rows, ebay_state = [], "PROVIDER_ERROR"
        observations.extend(ebay_rows)
        provider_states.append({"provider": "eBay Browse", "status": ebay_state})
        ebay_pool.shutdown(wait=False)
    else:
        provider_states.append({"provider": "eBay Browse", "status": "AUTH_REQUIRED"})
    adapters = provider_adapters or {}
    if adapters:
        with ThreadPoolExecutor(max_workers=len(adapters)) as adapter_pool:
            futures = {provider: adapter_pool.submit(adapter, identifier, location) for provider, adapter in adapters.items()}
            for provider, future in futures.items():
                try:
                    result = future.result()
                    rows, state = result if isinstance(result, tuple) else (result, "AVAILABLE_WITH_PRICES")
                    normalized_rows = [row if row.get("freshness_state") and row.get("provider") else normalize_observation(row, source=provider, exact_match=True, context=location) for row in (rows or [])]
                    observations.extend(normalized_rows)
                    provider_states.append({"provider": provider, "status": state})
                except Exception:
                    provider_states.append({"provider": provider, "status": "PROVIDER_ERROR"})
    observations = deduplicate_observations(observations)
    marketplaces = _marketplace_statuses(database, identifier or "", observations) if identifier else marketplace_statuses(configured_marketplaces)
    for item in marketplaces:
        if item["eligibility"] == "NOT_CONFIGURED":
            provider_states.append({"provider": item["channel"], "status": "NOT_CONFIGURED"})
    reference = select_reference_price(observations)
    ranked = reference.pop("ranked_observations", [])
    priced = [item for item in ranked if item.get("exact_match") and (item.get("sale_price") or item.get("price")) is not None]
    prices = [(item.get("sale_price") or item.get("price")) for item in priced]
    delivered = [item.get("delivered_price") for item in priced if item.get("delivered_price") is not None]
    return {
        "observations": ranked,
        "local_observations": [item for item in ranked if item["location_type"] == LOCATION_LOCAL],
        "online_observations": [item for item in ranked if item["location_type"] in {LOCATION_ONLINE, LOCATION_ONLINE_RETAIL}],
        "marketplace_observations": [item for item in ranked if item["location_type"] == LOCATION_MARKETPLACE],
        "lowest_observed_price": min(prices, default=None),
        "lowest_delivered_price": min(delivered, default=None),
        "price_comparison_basis": "DELIVERED_PRICE_WHEN_KNOWN_ELSE_ITEM_PRICE",
        "price_groups": {
            "best_trusted": [item for item in ranked if item.get("reference_eligible")],
            "other_observed": [item for item in ranked if not item.get("reference_eligible")],
        },
        "retailers": unavailable_retailers(("Target", "Home Depot", "Lowe's", "Walgreens/CVS"), location=location),
        "marketplaces": marketplaces, "provider_states": provider_states, "walmart_catalog": walmart_meta,
        "reference_price": reference, "location": location,
        "provider_status": "connected" if observations else (
            "unavailable" if external is None else provider_states[0]["status"]
        ),
        "provider_diagnostics": diagnostics,
        "cache": {
            "state": external.get("cache_state") if external else None,
            "retrieved_at": external.get("cache_retrieved_at") if external else None,
            "age_seconds": external.get("cache_age_seconds") if external else None,
            "live_provider_status": external.get("live_provider_status") if external else None,
        },
    }
