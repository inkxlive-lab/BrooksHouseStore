"""Small persistent cache for external Deal Scanner price evidence.

This is reference data only.  It intentionally has no relationship to
products, inventory, receiving, or marketplace publication.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, text


PRICE_CACHE_TABLE = "deal_price_intelligence_cache"
FRESH_TTL_SECONDS = 6 * 60 * 60
RECENT_TTL_SECONDS = 48 * 60 * 60


def normalize_identifier(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def freshness_state(retrieved_at: Any, *, now: datetime | None = None) -> str:
    try:
        then = datetime.fromisoformat(str(retrieved_at).replace("Z", "+00:00"))
        then = then.replace(tzinfo=timezone.utc) if then.tzinfo is None else then.astimezone(timezone.utc)
        current = now or datetime.now(timezone.utc)
        age = max(0.0, (current - then).total_seconds())
    except (TypeError, ValueError):
        return "STALE"
    if age <= FRESH_TTL_SECONDS:
        return "FRESH"
    if age <= RECENT_TTL_SECONDS:
        return "RECENT"
    return "STALE"


def _available(database) -> bool:
    if database is None:
        return False
    try:
        return inspect(database.get_bind()).has_table(PRICE_CACHE_TABLE)
    except Exception:
        return False


def _age_seconds(retrieved_at: str) -> int | None:
    try:
        then = datetime.fromisoformat(str(retrieved_at).replace("Z", "+00:00"))
        then = then.replace(tzinfo=timezone.utc) if then.tzinfo is None else then.astimezone(timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    except (TypeError, ValueError):
        return None


def _decorate(payload: dict[str, Any], row: dict[str, Any], *, live_status: str | None = None) -> dict[str, Any]:
    result = dict(payload)
    retrieved_at = row.get("retrieved_at")
    result["cache_state"] = freshness_state(retrieved_at)
    result["cache_retrieved_at"] = retrieved_at
    result["cache_age_seconds"] = _age_seconds(retrieved_at)
    result["cache_provider"] = row.get("provider")
    result["cache_hit"] = True
    result["provider_observation_at"] = row.get("provider_observation_at")
    result["cache_metadata"] = json.loads(row.get("metadata_json") or "{}")
    if live_status:
        result["live_provider_status"] = live_status
        result["live_provider_error"] = result.get("error")
        result["provider_status"] = "CACHED_FALLBACK"
        result["error"] = "Broad price provider temporarily unavailable; cached observations shown."
    return result


def get_cached(database, identifier: Any, provider: str = "UPCitemdb") -> dict[str, Any] | None:
    key = normalize_identifier(identifier)
    if not key or not _available(database):
        return None
    row = database.execute(text(f"SELECT * FROM {PRICE_CACHE_TABLE} WHERE identifier=:identifier AND provider=:provider"),
                           {"identifier": key, "provider": provider}).mappings().first()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        return None
    return _decorate(payload, dict(row))


def put_cached(database, identifier: Any, payload: dict[str, Any], *, provider: str = "UPCitemdb",
               provider_observation_at: str | None = None) -> bool:
    key = normalize_identifier(identifier)
    if not key or not _available(database) or not payload:
        return False
    metadata = {
        "http_status": payload.get("http_status"),
        "provider_error_code": payload.get("provider_error_code"),
        "rate_limit": payload.get("rate_limit") or {},
    }
    now = utc_now()
    provider_observation_at = provider_observation_at or payload.get("provider_observation_at") or payload.get("observed_at")
    bind = database.get_bind()
    with bind.begin() as connection:
        connection.execute(text(f"""INSERT INTO {PRICE_CACHE_TABLE}
        (identifier, provider, payload_json, retrieved_at, provider_observation_at, last_status, metadata_json, updated_at)
        VALUES (:identifier, :provider, :payload, :retrieved, :observed, :status, :metadata, :updated)
        ON CONFLICT(identifier, provider) DO UPDATE SET
          payload_json=excluded.payload_json, retrieved_at=excluded.retrieved_at,
          provider_observation_at=excluded.provider_observation_at, last_status=excluded.last_status,
          metadata_json=excluded.metadata_json, updated_at=excluded.updated_at"""), {
            "identifier": key, "provider": provider, "payload": json.dumps(payload, default=str),
            "retrieved": now, "observed": provider_observation_at, "status": payload.get("provider_status"),
            "metadata": json.dumps(metadata, default=str), "updated": now,
        })
    return True


def cache_is_usable(result: dict[str, Any] | None) -> bool:
    return bool(result and result.get("cache_state") in {"FRESH", "RECENT", "STALE"})
