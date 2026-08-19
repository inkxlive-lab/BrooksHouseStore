"""Source adapters for the review-first product enrichment workflow."""
from __future__ import annotations

import json
import inspect as python_inspect
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.database.models import (
    ProductEnrichmentItem,
    ProductEnrichmentLookupCache,
    ProductEnrichmentProposal,
)
from app.integrations.product_lookup import lookup_upc_online


SUGGESTED_FIELDS = {
    "product_name", "brand", "description", "category", "size_value",
    "size_unit", "suggested_retail_price", "product_image",
}


@dataclass
class SourceCandidate:
    field_name: str
    value: Any
    source_type: str
    source_name: str
    confidence: Decimal
    source_reference: str | None = None


class RateLimiter:
    """Thread-safe, injectable minimum-interval limiter for internet calls."""

    def __init__(
        self,
        minimum_interval_seconds: float = 1.25,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self.clock = clock
        self.sleeper = sleeper
        self._last_call: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if self._last_call is not None:
                remaining = self.minimum_interval_seconds - (now - self._last_call)
                if remaining > 0:
                    self.sleeper(remaining)
                    now = self.clock()
            self._last_call = now


DEFAULT_INTERNET_RATE_LIMITER = RateLimiter()
INTERNET_CACHE_TTL = timedelta(days=7)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(part) for part in value if part not in (None, ""))
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def _safe_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested(data: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, "", [], {}):
            return current
    return None


def _size_candidates(value: Any, source_type: str, source_name: str, confidence: Decimal):
    raw = _clean(value)
    if not raw:
        return []
    match = re.search(
        r"(?i)(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|fluid ounces?|ounces?|oz|lbs?|pounds?|"
        r"grams?|g|kilograms?|kg|millilit(?:er|re)s?|ml|lit(?:er|re)s?|l)\b",
        raw,
    )
    if not match:
        return []
    units = {
        "ounce": "oz", "ounces": "oz", "oz": "oz",
        "fl oz": "fl oz", "fluid ounce": "fl oz", "fluid ounces": "fl oz",
        "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
        "gram": "g", "grams": "g", "g": "g", "kilogram": "kg",
        "kilograms": "kg", "kg": "kg", "milliliter": "ml",
        "milliliters": "ml", "millilitre": "ml", "millilitres": "ml", "ml": "ml",
        "liter": "L", "liters": "L", "litre": "L", "litres": "L", "l": "L",
    }
    unit_key = re.sub(r"[.]", "", match.group(2).lower())
    unit_key = re.sub(r"\s+", " ", unit_key)
    return [
        SourceCandidate("size_value", match.group(1), source_type, source_name, confidence),
        SourceCandidate("size_unit", units.get(unit_key, unit_key), source_type, source_name, confidence),
    ]


def _from_mapping(
    data: dict[str, Any], source_type: str, source_name: str,
    confidence: Decimal, reference: str | None = None,
) -> list[SourceCandidate]:
    candidates: list[SourceCandidate] = []
    mapping = {
        "product_name": _nested(data, "title", "product_name", "name"),
        "brand": _nested(data, "brand", "vendor", "manufacturer", "product.brand"),
        "description": _nested(
            data, "description", "body_html", "bodyHtml", "longDescription",
            "shortDescription", "product.description",
        ),
        "category": _nested(data, "category", "product_type", "productType", "product.category"),
        "suggested_retail_price": _nested(data, "price", "listed_price", "price_amount", "price_low"),
        "product_image": _nested(
            data, "primary_image_url", "image_url", "imageUrl", "featured_image", "image",
        ),
    }
    images = _nested(data, "images", "product.images", "media")
    if not mapping["product_image"] and isinstance(images, list) and images:
        first = images[0]
        mapping["product_image"] = first if isinstance(first, str) else _nested(first, "url", "src")
    for field_name, value in mapping.items():
        cleaned = _clean(value)
        if cleaned:
            candidates.append(SourceCandidate(
                field_name, cleaned, source_type, source_name, confidence, reference
            ))
    candidates.extend(_size_candidates(
        _nested(data, "weight", "quantity", "size", "unit_weight"),
        source_type, source_name, min(confidence, Decimal("0.6500")),
    ))
    return candidates


def _table_names(database: Session) -> set[str]:
    return set(inspect(database.get_bind()).get_table_names())


def local_candidates(database: Session, item: ProductEnrichmentItem) -> list[SourceCandidate]:
    """Read existing catalog/channel data without making network requests."""
    tables = _table_names(database)
    barcode = (item.primary_barcode or "").strip()
    lookup = barcode.lstrip("0") or "0"
    candidates: list[SourceCandidate] = []

    if barcode and "master_catalog" in tables:
        row = database.execute(text("""
            SELECT description, unit_weight, barcode_exact
            FROM master_catalog
            WHERE barcode_exact = :barcode OR barcode_raw = :barcode
               OR barcode_lookup = :lookup
            ORDER BY catalog_id LIMIT 1
        """), {"barcode": barcode, "lookup": lookup}).mappings().first()
        if row:
            candidates.extend(_from_mapping(
                {"title": row["description"], "description": row["description"],
                 "unit_weight": row["unit_weight"]},
                "local_catalog", "Master Catalog", Decimal("0.7800"), barcode,
            ))

    if "channel_content_snapshots" in tables:
        rows = database.execute(text("""
            SELECT channel_name, external_id, title, description, brand, price,
                   primary_image_url, attributes_json
            FROM channel_content_snapshots WHERE product_id = :product_id
            ORDER BY synced_at DESC
        """), {"product_id": item.product_id}).mappings().all()
        for row in rows:
            data = dict(row)
            data.update(_safe_json(row["attributes_json"]))
            candidates.extend(_from_mapping(
                data, "channel_snapshot", str(row["channel_name"] or "Channel").title(),
                Decimal("0.8600"), _clean(row["external_id"]),
            ))

    if barcode and {"channel_listings", "sales_channels"}.issubset(tables):
        rows = database.execute(text("""
            SELECT sc.channel_name, cl.external_product_id, cl.listing_title,
                   cl.vendor, cl.listed_price, cl.source_data
            FROM channel_listings cl
            JOIN sales_channels sc ON sc.channel_id = cl.channel_id
            WHERE cl.barcode_exact = :barcode OR cl.barcode_lookup = :lookup
            ORDER BY cl.last_imported_at DESC
        """), {"barcode": barcode, "lookup": lookup}).mappings().all()
        for row in rows:
            data = _safe_json(row["source_data"])
            data.update({"title": row["listing_title"], "brand": row["vendor"],
                         "price": row["listed_price"]})
            candidates.extend(_from_mapping(
                data, "channel_listing", str(row["channel_name"] or "Channel").title(),
                Decimal("0.8200"), _clean(row["external_product_id"]),
            ))

    if barcode and "walmart_catalog_matches" in tables:
        row = database.execute(text("""
            SELECT walmart_item_id, title, brand, product_type, price_amount,
                   image_url, source_data FROM walmart_catalog_matches
            WHERE barcode_lookup = :lookup AND UPPER(match_status) = 'MATCH'
            ORDER BY updated_at DESC LIMIT 1
        """), {"lookup": lookup}).mappings().first()
        if row:
            data = _safe_json(row["source_data"])
            data.update({"title": row["title"], "brand": row["brand"],
                         "product_type": row["product_type"], "price": row["price_amount"],
                         "image_url": row["image_url"]})
            candidates.extend(_from_mapping(
                data, "catalog_lookup", "Walmart Catalog", Decimal("0.8400"),
                _clean(row["walmart_item_id"]),
            ))
    return candidates


def internet_candidates(
    barcode: str,
    lookup: Callable[[str], dict[str, Any]] = lookup_upc_online,
    limiter: RateLimiter | None = None,
    database: Session | None = None,
    now: datetime | None = None,
) -> tuple[list[SourceCandidate], str | None]:
    if not barcode:
        return [], "Product has no barcode for internet lookup."
    checked_at = now or datetime.now()
    cache_key = f"internet:{barcode.strip()}"
    cached = None
    if database is not None:
        cached = database.scalar(
            select(ProductEnrichmentLookupCache).where(
                ProductEnrichmentLookupCache.cache_key == cache_key
            )
        )
        if cached is not None and cached.expires_at > checked_at:
            payload = _safe_json(cached.payload_json)
            return _from_mapping(
                payload,
                cached.source_type,
                cached.source_name,
                Decimal("0.7000"),
                barcode,
            ), None
    request_limiter = limiter or DEFAULT_INTERNET_RATE_LIMITER
    try:
        parameters = python_inspect.signature(lookup).parameters
        if "before_request" in parameters:
            result = lookup(barcode, before_request=request_limiter.wait)
        else:
            request_limiter.wait()
            result = lookup(barcode)
    except Exception as exc:  # source errors are persisted, not allowed to break the batch
        return [], f"Internet lookup failed: {type(exc).__name__}: {exc}"[:1000]
    if result.get("error") and not result.get("found"):
        return [], _clean(result.get("error"))
    source = _clean(result.get("source")) or "Internet"
    if database is not None and result.get("found"):
        payload_json = json.dumps(result, default=str, sort_keys=True)
        if cached is None:
            cached = ProductEnrichmentLookupCache(
                cache_key=cache_key,
                source_type="internet",
                source_name=source,
                payload_json=payload_json,
                expires_at=checked_at + INTERNET_CACHE_TTL,
            )
            database.add(cached)
        else:
            cached.source_type = "internet"
            cached.source_name = source
            cached.payload_json = payload_json
            cached.expires_at = checked_at + INTERNET_CACHE_TTL
    return _from_mapping(
        result, "internet", source, Decimal("0.7000"), barcode
    ), _clean(result.get("error"))


def save_candidates(
    database: Session, item: ProductEnrichmentItem, candidates: list[SourceCandidate]
) -> int:
    missing = set(json.loads(item.missing_fields_json or "[]"))
    existing = {
        (row.field_name, row.normalized_value, row.source_name)
        for row in item.proposals
    }
    saved = 0
    for candidate in candidates:
        if candidate.field_name not in SUGGESTED_FIELDS or candidate.field_name not in missing:
            continue
        value = _clean(candidate.value)
        if not value:
            continue
        key = (candidate.field_name, value.casefold(), candidate.source_name)
        if key in existing:
            continue
        database.add(ProductEnrichmentProposal(
            item_id=item.item_id,
            field_name=candidate.field_name,
            proposed_value=value,
            normalized_value=value.casefold(),
            source_type=candidate.source_type,
            source_name=candidate.source_name[:120],
            source_reference=(candidate.source_reference or "")[:300] or None,
            confidence=candidate.confidence,
            status="proposed",
        ))
        existing.add(key)
        saved += 1
    return saved


def save_source_error(
    database: Session, item: ProductEnrichmentItem, source_name: str, message: str
) -> None:
    database.add(ProductEnrichmentProposal(
        item_id=item.item_id, field_name="source_error", proposed_value=None,
        normalized_value=None, source_type="internet", source_name=source_name[:120],
        confidence=None, status="error", error_code="lookup_error",
        error_message=str(message)[:2000],
    ))
