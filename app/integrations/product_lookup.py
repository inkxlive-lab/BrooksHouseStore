from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
import re

import requests


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


def _empty_result(barcode: str, source: str, error: str | None = None):
    result = {
        "found": False,
        "source": source,
        "barcode": barcode,
        "images": [],
    }

    if error:
        result["error"] = error

    return result


def _lookup_upcitemdb(barcode: str, before_request=None):
    try:
        if before_request:
            before_request()
        response = requests.get(
            UPCITEMDB_URL,
            params={"upc": barcode},
            headers=REQUEST_HEADERS,
            timeout=8,
        )

        if response.status_code == 404:
            return _empty_result(barcode, "UPCitemdb")

        if response.status_code == 429:
            return _empty_result(
                barcode,
                "UPCitemdb",
                "UPCitemdb daily/rate limit reached",
            )

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

        return {
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
        }

    except (requests.RequestException, ValueError) as exc:
        return _empty_result(barcode, "UPCitemdb", str(exc))


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
            return _empty_result(barcode, "UPCitemdb Public Page")

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
        }

    except requests.RequestException as exc:
        return _empty_result(
            barcode,
            "UPCitemdb Public Page",
            str(exc),
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


def lookup_upc_online(barcode: str, before_request=None):
    """Return normalized internet data with image-capable fallbacks."""

    barcode = str(barcode).strip()

    if not barcode:
        return _empty_result("", "Internet", "No barcode supplied")

    primary = _lookup_upcitemdb(barcode, before_request)

    # Avoid three extra requests when UPCitemdb already supplied an image.
    if primary.get("found") and primary.get("images"):
        return primary

    # The public page often remains available when the trial API reaches
    # its daily limit. It is UPC-specific and therefore takes priority over
    # community databases that can contain incorrect barcode associations.
    page_result = _lookup_upcitemdb_page(barcode, before_request)

    if page_result.get("found"):
        return _merge_results(primary, page_result)

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
        return _merge_results(primary, best_fallback)

    primary.setdefault("images", [])
    primary["fallback_sources_checked"] = [
        result["source"] for result in fallbacks
    ]
    return primary
