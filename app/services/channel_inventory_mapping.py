"""Authoritative, fail-closed marketplace mapping validation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


SAFE_LINK_STATUSES = {"linked", "matched", "manual"}


@dataclass(frozen=True)
class MappingValidation:
    safe: bool
    status: str
    reason: str
    product_id: int | None
    link_ids: tuple[int, ...] = ()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _product_is_active(connection: sqlite3.Connection, product_id: int | None) -> bool:
    if product_id is None:
        return False
    columns = _columns(connection, "products")
    active = " AND COALESCE(active,1)=1" if "active" in columns else ""
    return connection.execute(f"SELECT 1 FROM products WHERE product_id=?{active}", (product_id,)).fetchone() is not None


def validate_mapping(connection: sqlite3.Connection, channel: str, product_id: int | None,
                     sku: str = "", asin: str = "", stored_status: str = "") -> MappingValidation:
    channel = channel.casefold()
    if product_id is None:
        return MappingValidation(False, "missing", "order line has no BrooksHouse product", None)
    if not _product_is_active(connection, product_id):
        return MappingValidation(False, "stale_or_disabled", "mapped BrooksHouse product is missing or disabled", product_id)
    if channel == "shopify":
        status = stored_status.casefold().strip()
        if status != "matched":
            return MappingValidation(False, status or "missing", "Shopify line is not authoritatively matched", product_id)
        return MappingValidation(True, "matched", "Shopify line has an active exact match", product_id)
    if channel == "amazon":
        if not _columns(connection, "amazon_listings") or not _columns(connection, "amazon_product_links"):
            return MappingValidation(False, "missing", "Amazon authoritative mapping tables are unavailable", product_id)
        listing_columns = _columns(connection, "amazon_listings")
        keys, params = [], []
        if sku and "seller_sku" in listing_columns:
            keys.append("TRIM(al.seller_sku)=? COLLATE NOCASE"); params.append(sku.strip())
        if asin and "asin" in listing_columns:
            keys.append("TRIM(al.asin)=? COLLATE NOCASE"); params.append(asin.strip())
        if not keys:
            return MappingValidation(False, "missing", "Amazon line has no authoritative listing key", product_id)
        approval = "LOWER(COALESCE(al.approval_status,'approved'))" if "approval_status" in listing_columns else "'approved'"
        inventory_state = "LOWER(COALESCE(al.inventory_status,'active'))" if "inventory_status" in listing_columns else "'active'"
        rows = connection.execute(
            f"""SELECT apl.amazon_product_link_id,apl.product_id,LOWER(COALESCE(apl.match_status,'')) status,
                        {approval} approval_status,{inventory_state} listing_state
                  FROM amazon_listings al JOIN amazon_product_links apl USING(amazon_listing_id)
                 WHERE ({' OR '.join(keys)})""", params).fetchall()
    elif channel == "walmart":
        if not sku:
            return MappingValidation(False, "missing", "Walmart line has no seller SKU", product_id)
        if not _columns(connection, "walmart_listings") or not _columns(connection, "walmart_product_links"):
            return MappingValidation(False, "missing", "Walmart authoritative mapping tables are unavailable", product_id)
        listing_columns = _columns(connection, "walmart_listings")
        active = "COALESCE(wl.active,1)" if "active" in listing_columns else "1"
        rows = connection.execute(
            f"""SELECT wpl.walmart_product_link_id,wpl.product_id,LOWER(COALESCE(wpl.match_status,'')) status,
                       'approved' approval_status,CASE WHEN {active}=1 THEN 'active' ELSE 'disabled' END listing_state
                 FROM walmart_listings wl JOIN walmart_product_links wpl USING(walmart_listing_id)
                WHERE TRIM(wl.seller_sku)=? COLLATE NOCASE""", (sku.strip(),)).fetchall()
    else:
        return MappingValidation(False, "unsupported", f"unsupported channel {channel}", product_id)
    safe = [row for row in rows if str(row["status"]) in SAFE_LINK_STATUSES and row["product_id"] is not None
            and str(row["approval_status"]) not in {"rejected","disabled","inactive","suppressed"}
            and str(row["listing_state"]) not in {"disabled","inactive","retired","deleted"}]
    products = {int(row["product_id"]) for row in safe}
    if len(rows) != 1 or len(safe) != 1 or len(products) != 1:
        statuses = sorted({str(row["status"] or "missing") for row in rows})
        state = "ambiguous" if len(rows) > 1 or len(products) > 1 else (statuses[0] if statuses else "missing")
        return MappingValidation(False, state, "authoritative marketplace link is missing, unsafe, or ambiguous", product_id,
                                 tuple(int(row[0]) for row in rows))
    linked_product = next(iter(products))
    if linked_product != int(product_id):
        return MappingValidation(False, "conflict", "order-line product differs from authoritative marketplace link", product_id,
                                 (int(safe[0][0]),))
    return MappingValidation(True, "matched", "authoritative marketplace link is active and unambiguous", product_id,
                             (int(safe[0][0]),))
