"""Shared Smart Scan identification and Deal Scanner calculations.

This module deliberately contains no inventory-creation behavior.  It gives
the existing receiving scan and the Deal Scanner one normalized identification
contract while leaving catalog approval to their existing workflows.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import or_, select

from app.database.models import Product, ProductBarcode


MATCHED_CATALOG = "MATCHED_CATALOG"
IDENTIFIED_EXTERNAL = "IDENTIFIED_EXTERNAL"
PARTIAL_IDENTIFICATION = "PARTIAL_IDENTIFICATION"
UNKNOWN = "UNKNOWN"


def normalize_identifier(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def normalize_barcode(value: Any) -> dict[str, str]:
    raw = str(value or "").strip()
    exact = normalize_identifier(raw)
    lookup = exact.lstrip("0") or "0"
    without_check_digit = exact[:-1] if len(exact) > 1 else exact
    lookup_without_check_digit = without_check_digit.lstrip("0") or "0"
    return {
        "raw": raw,
        "exact": exact,
        "lookup": lookup,
        "without_check_digit": without_check_digit,
        "lookup_without_check_digit": lookup_without_check_digit,
    }


def _product_payload(product: Product, barcode: str, match_method: str) -> dict[str, Any]:
    return {
        "product_id": int(product.product_id),
        "product_name": product.product_name,
        "brand": product.brand,
        "description": product.description,
        "category": product.category,
        "size_value": str(product.size_value) if product.size_value is not None else None,
        "size_unit": product.size_unit,
        "pack_quantity": product.pack_quantity,
        "store_price": str(product.store_price) if product.store_price is not None else None,
        "suggested_retail_price": (
            str(product.suggested_retail_price)
            if product.suggested_retail_price is not None else None
        ),
        "barcode": barcode,
        "match_method": match_method,
    }


def catalog_product_for_barcode(database, raw_value: Any) -> dict[str, Any] | None:
    """Find a catalog product using exact barcode first, then legacy-safe forms."""
    parts = normalize_barcode(raw_value)
    if not parts["exact"]:
        return None
    rows = database.execute(
        select(ProductBarcode, Product).join(Product, Product.product_id == ProductBarcode.product_id)
        .where(
            or_(
                ProductBarcode.barcode == parts["exact"],
                ProductBarcode.barcode == parts["lookup"],
                ProductBarcode.barcode == parts["without_check_digit"],
                ProductBarcode.barcode == parts["lookup_without_check_digit"],
            )
        )
    ).all()
    if not rows:
        return None
    barcode_row, product = rows[0]
    stored = normalize_identifier(barcode_row.barcode)
    method = "exact" if stored == parts["exact"] else "normalized"
    return _product_payload(product, stored, method)


def _first(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _external_fields(external: dict[str, Any] | None) -> dict[str, Any]:
    source = external or {}
    return {
        "upc": _first(source, "upc", "UPC", "barcode"),
        "ean": _first(source, "ean", "EAN"),
        "asin": _first(source, "asin", "ASIN"),
        "sku": _first(source, "sku", "SKU"),
        "model_number": _first(source, "model_number", "model", "model_number"),
        "manufacturer": _first(source, "manufacturer", "manufacturer_name"),
        "brand": _first(source, "brand", "brand_name"),
        "product_name": _first(source, "product_name", "title", "name"),
        "variant": _first(source, "variant"),
        "size": _first(source, "size", "size_value"),
        "quantity_or_pack_count": _first(source, "quantity_or_pack_count", "pack_quantity", "quantity"),
        "image_refs": _first(source, "image_refs", "images") or [],
    }


def build_scan_result(
    raw_value: Any = "",
    *,
    scan_type: str = "barcode",
    catalog: dict[str, Any] | None = None,
    external: dict[str, Any] | None = None,
    ocr_text: str | None = None,
    identifiers: dict[str, Any] | None = None,
    image_refs: list[Any] | None = None,
) -> dict[str, Any]:
    """Normalize catalog, provider, OCR, and manually supplied evidence."""
    ext = _external_fields(external)
    supplied = identifiers or {}
    merged = {key: _first(supplied, key) or value for key, value in ext.items()}
    barcode_parts = normalize_barcode(raw_value)
    barcode = _first(supplied, "barcode", "upc", "ean") or merged.get("upc") or merged.get("ean")
    if barcode:
        normalized = normalize_identifier(barcode)
        if len(normalized) in (12, 13, 14):
            merged.setdefault("upc", normalized if len(normalized) == 12 else None)
            merged.setdefault("ean", normalized if len(normalized) == 13 else None)

    evidence: list[str] = []
    if catalog:
        evidence.append(f"catalog:{catalog.get('match_method', 'barcode')}")
    if external:
        evidence.append("external_reference")
    if ocr_text:
        evidence.append("ocr")
    if identifiers:
        evidence.append("operator_input")

    product_name = catalog.get("product_name") if catalog else merged.get("product_name")
    brand = catalog.get("brand") if catalog else merged.get("brand")
    status = UNKNOWN
    confidence = 0.0
    if catalog:
        status = MATCHED_CATALOG
        confidence = 1.0 if catalog.get("match_method") == "exact" else 0.95
        product_name = catalog.get("product_name") or product_name
        brand = catalog.get("brand") or brand
        merged["size"] = catalog.get("size_value") or merged.get("size")
        merged["quantity_or_pack_count"] = catalog.get("pack_quantity") or merged.get("quantity_or_pack_count")
    elif product_name or brand or merged.get("model_number") or merged.get("upc") or merged.get("ean"):
        if external and product_name:
            status = IDENTIFIED_EXTERNAL
            confidence = float(external.get("confidence") or 0.70)
        else:
            status = PARTIAL_IDENTIFICATION
            confidence = 0.45
    elif ocr_text or any(merged.values()) or barcode_parts["exact"]:
        status = PARTIAL_IDENTIFICATION if (ocr_text or any(merged.values())) else UNKNOWN
        confidence = 0.30 if status == PARTIAL_IDENTIFICATION else 0.0

    return {
        "scan_type": scan_type,
        "raw_value": str(raw_value or ""),
        "barcode": normalize_identifier(barcode) if barcode else barcode_parts["exact"] or None,
        "upc": normalize_identifier(merged.get("upc")) if merged.get("upc") else None,
        "ean": normalize_identifier(merged.get("ean")) if merged.get("ean") else None,
        "asin": merged.get("asin"),
        "sku": merged.get("sku"),
        "model_number": merged.get("model_number"),
        "manufacturer": merged.get("manufacturer"),
        "brand": brand,
        "product_name": product_name,
        "variant": merged.get("variant"),
        "size": merged.get("size"),
        "quantity_or_pack_count": merged.get("quantity_or_pack_count"),
        "ocr_text": ocr_text or "",
        "catalog_match": bool(catalog),
        "catalog_product_id": catalog.get("product_id") if catalog else None,
        "identification_confidence": round(max(0.0, min(confidence, 1.0)), 4),
        "identification_sources": evidence,
        "image_refs": image_refs if image_refs is not None else merged.get("image_refs", []),
        "status": status,
    }


def identify_barcode(database, raw_value: Any, external_lookup: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    catalog = catalog_product_for_barcode(database, raw_value)
    external = None
    if catalog is None and external_lookup:
        try:
            external = external_lookup(normalize_barcode(raw_value)["exact"]) or None
        except Exception:
            external = None
    result = build_scan_result(raw_value, catalog=catalog, external=external)
    # Keep the raw external evidence available to Deal Scanner so it can use a
    # cleaner internet identity while retaining catalog context separately.
    result["external"] = external
    if catalog:
        result["catalog_product_name"] = catalog.get("product_name")
    return result


def variants_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return false when known size/count values conflict."""
    for keys in (("size", "size_value"), ("quantity_or_pack_count", "pack_quantity", "quantity")):
        lval = _first(left, *keys)
        rval = _first(right, *keys)
        if lval not in (None, "") and rval not in (None, ""):
            if normalize_identifier(lval) != normalize_identifier(rval):
                return False
    return True


def resolve_acquisition_cost(mode: str, bin_price: Any = None, acquisition_cost: Any = None) -> Decimal | None:
    value = bin_price if mode == "bin" and bin_price not in (None, "") else acquisition_cost
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount >= 0 else None


def calculate_deal_economics(*, mode: str, bin_price: Any = None, acquisition_cost: Any = None,
                             candidate_resale_price: Any = None, fees: Any = 0,
                             shipping_estimate: Any = 0, quantity: Any = 1) -> dict[str, Any]:
    cost = resolve_acquisition_cost(mode, bin_price, acquisition_cost)
    def money(value: Any) -> Decimal:
        try:
            return Decimal(str(value or 0))
        except (InvalidOperation, ValueError):
            return Decimal("0")
    resale = money(candidate_resale_price)
    profit = None if cost is None or resale <= 0 else resale - cost - money(fees) - money(shipping_estimate)
    units = max(1, int(quantity or 1))
    return {
        "acquisition_cost": str(cost) if cost is not None else None,
        "candidate_resale_price": str(resale) if resale > 0 else None,
        "estimated_profit": str(profit) if profit is not None else None,
        "estimated_margin": str((profit / resale * 100).quantize(Decimal("0.01"))) if profit is not None and resale > 0 else None,
        "quantity": units,
        "potential_total_profit": str(profit * units) if profit is not None else None,
    }
