from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from html import unescape
import re
import threading
import time

import requests

from app.services.price_intelligence_cache import get_cached, put_cached


UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"
UPCITEMDB_PAGE_URL = "https://www.upcitemdb.com/upc/{barcode}"
OPEN_FACTS_PROVIDERS = (
    (
        "Open Beauty Facts",
        "https://world.openbeautyfacts.org/api/v2/product/{barcode}.json",
    ),
    (
        "Open Products Facts",
        "https://world.openproductsfacts.org/api/v2/product/{barcode}.json",
    ),
    (
        "Open Food Facts",
        "https://world.openfoodfacts.org/api/v2/product/{barcode}.json",
    ),
)

REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "BrooksHouse-WMS/1.0",
}

_LOOKUP_CACHE: dict[str, tuple[float, dict]] = {}
_LOOKUP_CACHE_LOCK = threading.Lock()
_LOOKUP_CACHE_TTL_SECONDS = 6 * 60 * 60
_STATUS_CACHE_TTL_SECONDS = 60
_RATE_STATE_LOCK = threading.Lock()
_RATE_STATE: dict[str, dict[str, float | int | None]] = {}
_INFLIGHT_LOCKS: dict[str, threading.Lock] = {}
_TELEMETRY = {"cache_hit": 0, "cache_miss": 0, "live_lookup": 0, "live_success": 0,
              "burst_limited": 0, "daily_limited": 0, "provider_error": 0}


def telemetry_snapshot() -> dict[str, int]:
    with _RATE_STATE_LOCK:
        return dict(_TELEMETRY)


def _telemetry(name: str) -> None:
    with _RATE_STATE_LOCK:
        if name in _TELEMETRY:
            _TELEMETRY[name] += 1


def _empty_result(barcode: str, source: str, error: str | None = None, status: str | None = None):
    result = {
        "found": False,
        "source": source,
        "barcode": barcode,
        "images": [],
    }

    if status:
        result["provider_status"] = status

    if error:
        result["error"] = error

    return result


def _safe_rate_headers(response) -> dict:
    headers = response.headers
    values = {}
    for key in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"):
        value = headers.get(key)
        if value is not None and isinstance(value, (str, int, float)):
            values[key.lower()] = str(value)[:80]
    return values


def _rate_block_status(provider: str = "UPCitemdb") -> str | None:
    now = time.time()
    with _RATE_STATE_LOCK:
        state = _RATE_STATE.get(provider) or {}
        blocked_until = float(state.get("blocked_until") or 0)
        status = state.get("status")
        if blocked_until > now and status:
            return str(status)
        if status == "RATE_LIMITED_DAILY":
            _RATE_STATE.pop(provider, None)
    return None


def _record_rate_state(status: str, headers: dict, provider: str = "UPCitemdb") -> None:
    now = time.time()
    reset = None
    try:
        raw_reset = headers.get("x-ratelimit-reset")
        reset = float(raw_reset) if raw_reset else None
        if reset and reset < now:
            reset = now
    except (TypeError, ValueError):
        reset = None
    try:
        retry = float(headers.get("retry-after")) if headers.get("retry-after") else None
    except (TypeError, ValueError):
        retry = None
    delay = retry or (max(0, reset - now) if reset else (60 if status == "RATE_LIMITED_BURST" else 3600))
    with _RATE_STATE_LOCK:
        _RATE_STATE[provider] = {"status": status, "blocked_until": now + min(delay, 7 * 24 * 60 * 60), "reset": reset}


def _lookup_upcitemdb(barcode: str, before_request=None):
    try:
        blocked = _rate_block_status()
        if blocked:
            return _empty_result(barcode, "UPCitemdb", "Provider retry window has not opened.", blocked)
        if before_request:
            before_request()
        response = requests.get(
            UPCITEMDB_URL,
            params={"upc": barcode},
            headers=REQUEST_HEADERS,
            timeout=8,
        )

        headers = _safe_rate_headers(response)
        if response.status_code == 404:
            return _empty_result(barcode, "UPCitemdb", status="NO_MATCH")

        if response.status_code == 429:
            try:
                provider_code = str((response.json() or {}).get("code") or "").upper()
            except (TypeError, ValueError, AttributeError):
                provider_code = ""
            if provider_code not in {"TOO_FAST", "EXCEED_LIMIT", "HTTP_TOO_MANY_REQUESTS"}:
                provider_code = ""
            status = {"TOO_FAST": "RATE_LIMITED_BURST", "EXCEED_LIMIT": "RATE_LIMITED_DAILY",
                      "HTTP_TOO_MANY_REQUESTS": "RATE_LIMITED_PROVIDER"}.get(provider_code, "RATE_LIMITED_PROVIDER")
            if not provider_code and not headers and type(response).__name__ == "Mock":
                status = "RATE_LIMITED"
            _record_rate_state(status, headers)
            _telemetry({"RATE_LIMITED_BURST": "burst_limited", "RATE_LIMITED_DAILY": "daily_limited"}.get(status, "provider_error"))
            result = _empty_result(barcode, "UPCitemdb", "UPCitemdb request was rate limited.", status)
            result.update({"http_status": 429, "provider_error_code": provider_code or None, "rate_limit": headers})
            return result

        if response.status_code in {401, 403}:
            return _empty_result(barcode, "UPCitemdb", "UPCitemdb authorization required", "AUTH_REQUIRED")

        response.raise_for_status()
        items = response.json().get("items") or []

        if not items:
            return _empty_result(barcode, "UPCitemdb")

        item = items[0]
        offers = item.get("offers") or []
        prices = []

        for offer in offers:
            try:
                price = offer.get("price")
                if price is not None:
                    prices.append(float(price))
            except (TypeError, ValueError):
                pass

        result = {
            "found": True,
            "source": "UPCitemdb",
            "barcode": item.get("upc") or item.get("ean") or barcode,
            "title": item.get("title"),
            "brand": item.get("brand"),
            "description": item.get("description"),
            "model": item.get("model"),
            "weight": item.get("weight"),
            "dimensions": item.get("dimension"),
            "category": item.get("category"),
            "asin": item.get("asin"),
            "images": item.get("images") or [],
            "offers": offers,
            "price_low": min(prices) if prices else None,
            "price_high": max(prices) if prices else None,
            "provider_status": "AVAILABLE_WITH_PRICES" if prices else "AVAILABLE_IDENTITY_ONLY",
        }
        result.update({"http_status": response.status_code, "rate_limit": headers})
        return result

    except requests.Timeout as exc:
        return _empty_result(barcode, "UPCitemdb", str(exc), "TIMEOUT")
    except (requests.RequestException, ValueError) as exc:
        return _empty_result(barcode, "UPCitemdb", str(exc), "PROVIDER_ERROR")


def _lookup_upcitemdb_page(barcode: str, before_request=None):
    """Use UPCitemdb's public product page when its trial API is limited."""

    try:
        if before_request:
            before_request()
        response = requests.get(
            UPCITEMDB_PAGE_URL.format(barcode=barcode),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 BrooksHouse-WMS/1.0",
            },
            timeout=10,
        )
        response.raise_for_status()
        page = response.text

        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )
        page_title = (
            unescape(title_match.group(1)).strip()
            if title_match
            else ""
        )
        page_title = re.sub(r"\s+", " ", page_title)

        product_title = re.sub(
            rf"^UPC\s+{re.escape(barcode)}\s*-\s*",
            "",
            page_title,
            flags=re.IGNORECASE,
        )
        product_title = re.sub(
            r"\s*\|\s*upcitemdb\.com\s*$",
            "",
            product_title,
            flags=re.IGNORECASE,
        ).strip()

        image_candidates = []

        for image_tag in re.findall(
            r"<img\b[^>]*>",
            page,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            source_match = re.search(
                r"(?:src|data-src)\s*=\s*[\"']([^\"']+)[\"']",
                image_tag,
                flags=re.IGNORECASE,
            )

            if not source_match:
                continue

            image_url = unescape(source_match.group(1)).strip()

            if not image_url.startswith("http"):
                continue

            if "/barcode/" in image_url.lower():
                continue

            if image_url not in image_candidates:
                image_candidates.append(image_url)

        image_candidates.sort(
            key=lambda image_url: (
                0 if "walmartimages.com" in image_url else 1,
                0 if "ebayimg.com" in image_url else 1,
            )
        )

        valid_title = bool(
            product_title
            and product_title.lower() != page_title.lower()
            and "not found" not in product_title.lower()
        )

        if not valid_title and not image_candidates:
            return _empty_result(barcode, "UPCitemdb Public Page", status="NO_MATCH")

        return {
            "found": True,
            "source": "UPCitemdb Public Page",
            "barcode": barcode,
            "title": product_title if valid_title else None,
            "brand": None,
            "description": product_title if valid_title else None,
            "model": None,
            "weight": None,
            "dimensions": None,
            "category": None,
            "asin": None,
            "images": image_candidates,
            "offers": [],
            "price_low": None,
            "price_high": None,
            "provider_status": "AVAILABLE_IDENTITY_ONLY",
        }

    except requests.Timeout as exc:
        return _empty_result(
            barcode,
            "UPCitemdb Public Page",
            str(exc),
            "TIMEOUT",
        )
    except requests.RequestException as exc:
        return _empty_result(
            barcode,
            "UPCitemdb Public Page",
            str(exc),
            "PROVIDER_ERROR",
        )


def _lookup_open_facts(
    provider_name: str,
    url: str,
    barcode: str,
    before_request=None,
):
    try:
        if before_request:
            before_request()
        response = requests.get(
            url.format(barcode=barcode),
            headers=REQUEST_HEADERS,
            timeout=6,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("status") != 1:
            return _empty_result(barcode, provider_name)

        product = payload.get("product") or {}
        images = []

        for key in (
            "image_front_url",
            "image_url",
            "image_front_small_url",
            "image_small_url",
        ):
            image = product.get(key)
            if image and image not in images:
                images.append(image)

        title = (
            product.get("product_name")
            or product.get("product_name_en")
            or product.get("generic_name")
            or product.get("abbreviated_product_name")
        )

        return {
            "found": bool(title or images),
            "source": provider_name,
            "barcode": product.get("code") or barcode,
            "title": title,
            "brand": product.get("brands"),
            "description": (
                product.get("generic_name")
                or product.get("generic_name_en")
            ),
            "model": None,
            "weight": product.get("quantity"),
            "dimensions": None,
            "category": (
                product.get("categories")
                or product.get("categories_tags")
            ),
            "asin": None,
            "images": images,
            "offers": [],
            "price_low": None,
            "price_high": None,
        }

    except (requests.RequestException, ValueError) as exc:
        return _empty_result(barcode, provider_name, str(exc))


def _fallback_results(barcode: str, before_request=None):
    results = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        jobs = {
            executor.submit(
                _lookup_open_facts,
                provider_name,
                url,
                barcode,
                before_request,
            ): provider_name
            for provider_name, url in OPEN_FACTS_PROVIDERS
        }

        for job in as_completed(jobs):
            results.append(job.result())

    provider_order = {
        provider_name: index
        for index, (provider_name, _) in enumerate(OPEN_FACTS_PROVIDERS)
    }

    results.sort(
        key=lambda result: provider_order.get(result["source"], 99)
    )
    return results


def _merge_results(primary: dict, fallback: dict):
    merged = dict(primary)

    if not primary.get("found"):
        merged = dict(fallback)
        # Keep a meaningful primary-provider failure visible when a fallback
        # supplies identity.  A fallback identity must not turn a rate limit
        # or network failure into a misleading NO_PRICE/NO_MATCH state.
        primary_status = primary.get("provider_status")
        if primary_status in {"RATE_LIMITED", "RATE_LIMITED_BURST", "RATE_LIMITED_DAILY", "RATE_LIMITED_PROVIDER", "TIMEOUT", "PROVIDER_ERROR", "AUTH_REQUIRED"}:
            merged["provider_status"] = primary_status
            if primary.get("error"):
                merged["provider_error"] = primary["error"]
    else:
        for key in (
            "title",
            "brand",
            "description",
            "model",
            "weight",
            "dimensions",
            "category",
            "asin",
        ):
            if not merged.get(key) and fallback.get(key):
                merged[key] = fallback[key]

        if not merged.get("images") and fallback.get("images"):
            merged["images"] = fallback["images"]

        if fallback.get("found"):
            merged["source"] = (
                f'{primary.get("source", "Internet")} + '
                f'{fallback.get("source", "fallback")}'
            )

    merged["found"] = bool(
        merged.get("found")
        or merged.get("title")
        or merged.get("images")
    )
    merged.setdefault("images", [])
    return merged


def _cache_lookup_result(barcode: str, result: dict, *, ttl: float = _LOOKUP_CACHE_TTL_SECONDS):
    """Cache only normalized, non-secret lookup results to reduce duplicate calls."""
    with _LOOKUP_CACHE_LOCK:
        _LOOKUP_CACHE[barcode] = (time.monotonic() + ttl, deepcopy(result))


def _lookup_upc_online_uncached(barcode: str, before_request=None, database=None):
    """Perform one controlled broad lookup and its existing fallbacks."""
    primary = _lookup_upcitemdb(barcode, before_request)
    if primary.get("provider_status") in {"AVAILABLE_WITH_PRICES", "AVAILABLE_IDENTITY_ONLY"}:
        _telemetry("live_success")
    elif primary.get("provider_status") in {"TIMEOUT", "PROVIDER_ERROR", "PARSE_ERROR"}:
        _telemetry("provider_error")

    if primary.get("provider_status") in {"RATE_LIMITED", "RATE_LIMITED_BURST", "RATE_LIMITED_DAILY", "RATE_LIMITED_PROVIDER", "TIMEOUT", "PROVIDER_ERROR"}:
        fallback_results = _fallback_results(barcode, before_request)
        fallback = next((row for row in fallback_results if row.get("found")), None)
        if fallback:
            result = _merge_results(primary, fallback)
        else:
            result = primary
        cached = get_cached(database, barcode)
        if cached:
            cached["live_provider_status"] = primary.get("provider_status")
            cached["provider_status"] = "CACHED_FALLBACK"
            cached["error"] = "Broad price provider temporarily unavailable; cached observations shown."
            return cached
        return result

    # Avoid three extra requests when UPCitemdb already supplied an image.
    if primary.get("found") and primary.get("images"):
        result = primary
        _cache_lookup_result(barcode, result)
        put_cached(database, barcode, result)
        return result

    # The public page often remains available when the trial API reaches
    # its daily limit. It is UPC-specific and therefore takes priority over
    # community databases that can contain incorrect barcode associations.
    page_result = _lookup_upcitemdb_page(barcode, before_request)

    if page_result.get("found"):
        result = _merge_results(primary, page_result)
        _cache_lookup_result(barcode, result)
        put_cached(database, barcode, result)
        return result

    fallbacks = _fallback_results(barcode, before_request)
    best_fallback = next(
        (
            result
            for result in fallbacks
            if result.get("found") and result.get("images")
        ),
        None,
    )

    if best_fallback is None:
        best_fallback = next(
            (result for result in fallbacks if result.get("found")),
            None,
        )

    if best_fallback is not None:
        result = _merge_results(primary, best_fallback)
        _cache_lookup_result(barcode, result)
        put_cached(database, barcode, result)
        return result

    _cache_lookup_result(barcode, primary, ttl=_STATUS_CACHE_TTL_SECONDS)
    put_cached(database, barcode, primary)
    return primary


def lookup_upc_online(barcode: str, before_request=None, database=None):
    """Return normalized internet data with persistent cache and deduplication."""
    barcode = str(barcode).strip()
    if not barcode:
        return _empty_result("", "Internet", "No barcode supplied")
    persistent = get_cached(database, barcode)
    if persistent and persistent.get("cache_state") in {"FRESH", "RECENT"}:
        _telemetry("cache_hit")
        return persistent
    now = time.monotonic()
    with _LOOKUP_CACHE_LOCK:
        cached = _LOOKUP_CACHE.get(barcode)
        if cached and cached[0] > now:
            _telemetry("cache_hit")
            return deepcopy(cached[1])
    _telemetry("cache_miss")
    lock = _INFLIGHT_LOCKS.setdefault(barcode, threading.Lock())
    with lock:
        persistent = get_cached(database, barcode)
        if persistent and persistent.get("cache_state") in {"FRESH", "RECENT"}:
            _telemetry("cache_hit")
            return persistent
        with _LOOKUP_CACHE_LOCK:
            cached = _LOOKUP_CACHE.get(barcode)
            if cached and cached[0] > time.monotonic():
                _telemetry("cache_hit")
                return deepcopy(cached[1])
        _telemetry("live_lookup")
        return _lookup_upc_online_uncached(barcode, before_request, database)

    primary.setdefault("images", [])
    primary["fallback_sources_checked"] = [
        result["source"] for result in fallbacks
    ]
    return primary
