"""Human-confirmed discrepancy review helpers; inventory is invariant."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.services.approved_mapping_application import inventory_fingerprint
from app.services.channel_inventory_engine import load_source_line


EXPLICIT_MAPPING_CONFIRMATION = "CONFIRM MARKETPLACE MAPPING"
EXPLICIT_REVIEW_CONFIRMATION = "MARK REVIEWED WITHOUT INVENTORY CHANGE"


def _connect(database: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(database).resolve()
    target = f"file:{path.as_posix()}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(target, uri=read_only, timeout=30)
    connection.row_factory = sqlite3.Row
    if read_only:
        connection.execute("PRAGMA query_only=ON")
    else:
        connection.execute("PRAGMA foreign_keys=ON")
    return connection


def search_products(database: str | Path, query: str, limit: int = 30) -> list[dict]:
    term = str(query or "").strip()
    if not term:
        return []
    connection = _connect(database, read_only=True)
    try:
        product_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(products)")}
        brand = "COALESCE(p.brand,'')" if "brand" in product_columns else "''"
        description = "COALESCE(p.description,'')" if "description" in product_columns else "''"
        active = "COALESCE(p.active,1)=1" if "active" in product_columns else "1=1"
        like = f"%{term}%"
        rows = connection.execute(
            f"""SELECT p.product_id,p.product_name,{brand} brand,
                      {description} description,
                      (SELECT pb.barcode FROM product_barcodes pb WHERE pb.product_id=p.product_id
                        ORDER BY pb.is_primary DESC,pb.barcode_id LIMIT 1) barcode,
                      COALESCE(SUM(CASE WHEN l.active=1 THEN i.quantity_on_hand-COALESCE(i.quantity_reserved,0) ELSE 0 END),0) available
                 FROM products p LEFT JOIN inventory i ON i.product_id=p.product_id
                 LEFT JOIN inventory_locations l ON l.location_id=i.location_id
                WHERE {active} AND
                      (CAST(p.product_id AS TEXT)=? OR p.product_name LIKE ? COLLATE NOCASE
                       OR {brand} LIKE ? COLLATE NOCASE
                       OR {description} LIKE ? COLLATE NOCASE
                       OR EXISTS(SELECT 1 FROM product_barcodes pb WHERE pb.product_id=p.product_id
                                 AND CAST(pb.barcode AS TEXT) LIKE ?))
                GROUP BY p.product_id,p.product_name
                ORDER BY CASE WHEN CAST(p.product_id AS TEXT)=? THEN 0 ELSE 1 END,p.product_name LIMIT ?""",
            (term,like,like,like,like,term,max(1,min(int(limit),100)))).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def mapping_confirmation_preview(database: str | Path, channel: str, order_id: str,
                                 order_line_id: str, product_id: int) -> dict:
    connection = _connect(database, read_only=True)
    try:
        source = load_source_line(connection,channel,order_id,order_line_id)
        product = connection.execute(
            "SELECT product_id,product_name,COALESCE(brand,'') brand FROM products WHERE product_id=? AND COALESCE(active,1)=1",
            (int(product_id),)).fetchone()
        if product is None:
            raise ValueError("Selected BrooksHouse product is missing or inactive")
        inventory = [dict(row) for row in connection.execute(
            """SELECT i.inventory_id,l.location_name,i.container_id,i.quantity_on_hand,i.quantity_reserved
                 FROM inventory i JOIN inventory_locations l USING(location_id)
                WHERE i.product_id=? ORDER BY l.location_name,i.inventory_id""",(int(product_id),))]
        return {"channel":source.channel,"order_id":source.order_id,"order_line_id":source.order_line_id,
                "marketplace_sku":source.sku,"marketplace_identifier":source.asin,
                "marketplace_title":source.title,"current_product_id":source.product_id,
                "selected_product":dict(product),"inventory":inventory,
                "source_version":source.source_version,"required_confirmation":EXPLICIT_MAPPING_CONFIRMATION}
    finally:
        connection.close()


def _normalized_identity(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _walmart_raw_lines(raw_json: str) -> list[dict]:
    try:
        order = json.loads(raw_json or "{}")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Retained Walmart source evidence is not valid JSON") from exc
    lines = ((order.get("orderLines") or {}).get("orderLine") or []) if isinstance(order, dict) else []
    if isinstance(lines, dict):
        lines = [lines]
    return [line for line in lines if isinstance(line, dict)]


def _resolve_walmart_listing_identity(connection: sqlite3.Connection, source) -> int:
    listings = connection.execute(
        "SELECT walmart_listing_id FROM walmart_listings WHERE TRIM(seller_sku)=? COLLATE NOCASE",
        (source.sku,)).fetchall()
    if len(listings) > 1:
        raise RuntimeError("Walmart listing identity is ambiguous")
    if len(listings) == 1:
        return int(listings[0][0])

    order_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(walmart_orders)")}
    line_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(walmart_order_lines)")}
    required_order = {"raw_json"}
    required_line = {"line_number", "sku", "upc", "item_name"}
    if not required_order <= order_columns or not required_line <= line_columns:
        raise RuntimeError("Walmart listing identity is missing and retained source evidence is unavailable")

    stored = connection.execute(
        """SELECT wol.line_number,wol.sku,wol.upc,wol.item_name,wo.raw_json
             FROM walmart_order_lines wol JOIN walmart_orders wo USING(purchase_order_id)
            WHERE wol.purchase_order_id=? AND wol.order_line_id=?""",
        (source.order_id,source.order_line_id)).fetchone()
    if stored is None:
        raise RuntimeError("Walmart order line source evidence is missing")
    raw_matches = [line for line in _walmart_raw_lines(stored["raw_json"])
                   if str(line.get("lineNumber") or "").strip() == str(stored["line_number"] or "").strip()]
    if len(raw_matches) != 1:
        raise RuntimeError("Retained Walmart source line identity is missing or ambiguous")
    raw_item = raw_matches[0].get("item") or {}
    raw_sku = str(raw_item.get("sku") or "").strip()
    raw_title = str(raw_item.get("productName") or raw_item.get("itemName") or "").strip()
    raw_upc = str(raw_item.get("upc") or "").strip()
    if (_normalized_identity(raw_sku) != _normalized_identity(stored["sku"]) or
            not raw_title or _normalized_identity(raw_title) != _normalized_identity(stored["item_name"])):
        raise RuntimeError("Retained Walmart source evidence conflicts with the normalized order line")
    if raw_upc and stored["upc"] and _normalized_identity(raw_upc) != _normalized_identity(stored["upc"]):
        raise RuntimeError("Retained Walmart UPC conflicts with the normalized order line")

    history = connection.execute(
        "SELECT item_name,upc FROM walmart_order_lines WHERE TRIM(sku)=? COLLATE NOCASE",
        (source.sku,)).fetchall()
    titles = {_normalized_identity(row["item_name"]) for row in history if _normalized_identity(row["item_name"])}
    upcs = {_normalized_identity(row["upc"]) for row in history if _normalized_identity(row["upc"])}
    if len(titles) != 1 or len(upcs) > 1:
        raise RuntimeError("Historical Walmart order evidence makes this seller SKU ambiguous")
    if raw_upc:
        upcs.add(_normalized_identity(raw_upc))
        if len(upcs) > 1:
            raise RuntimeError("Historical Walmart UPC evidence makes this seller SKU ambiguous")

    now = datetime.now(timezone.utc).isoformat()
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(walmart_listings)")}
    fields, values = ["seller_sku"], [raw_sku]
    if "item_name" in columns:
        fields.append("item_name"); values.append(raw_title)
    if "created_at" in columns:
        fields.append("created_at"); values.append(now)
    if "updated_at" in columns:
        fields.append("updated_at"); values.append(now)
    cursor = connection.execute(
        f"INSERT INTO walmart_listings ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)
    return int(cursor.lastrowid)


def apply_confirmed_mapping(database: str | Path, preview: dict, *, confirmation: str) -> dict:
    if confirmation != EXPLICIT_MAPPING_CONFIRMATION:
        raise ValueError("Exact mapping confirmation is required")
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        before = inventory_fingerprint(connection)
        source = load_source_line(connection,preview["channel"],preview["order_id"],preview["order_line_id"])
        if source.source_version != preview["source_version"] or source.sku != preview["marketplace_sku"]:
            raise RuntimeError("Marketplace line changed after confirmation preview")
        product_id = int(preview["selected_product"]["product_id"])
        if connection.execute("SELECT 1 FROM products WHERE product_id=? AND COALESCE(active,1)=1",(product_id,)).fetchone() is None:
            raise RuntimeError("Selected product is no longer active")
        if source.channel == "shopify":
            changed = connection.execute(
                """UPDATE shopify_sales_lines SET product_id=?,match_status='matched',match_method='manual_channel_inventory_review',updated_at=?
                     WHERE shopify_order_id=? AND shopify_line_id=?""",
                (product_id,datetime.now(timezone.utc).isoformat(),source.order_id,source.order_line_id)).rowcount
        elif source.channel == "amazon":
            predicates, identity_params = [], []
            if source.sku:
                predicates.append("TRIM(seller_sku)=? COLLATE NOCASE"); identity_params.append(source.sku)
            if source.asin:
                predicates.append("TRIM(asin)=? COLLATE NOCASE"); identity_params.append(source.asin)
            if not predicates:
                raise RuntimeError("Amazon line has no non-empty listing identity")
            listings = connection.execute(
                f"SELECT amazon_listing_id FROM amazon_listings WHERE {' OR '.join(predicates)}",identity_params).fetchall()
            if len(listings) != 1:
                raise RuntimeError("Amazon listing identity is missing or ambiguous")
            listing_id = int(listings[0][0])
            connection.execute(
                """INSERT INTO amazon_product_links(amazon_listing_id,product_id,match_status,match_method,linked_at)
                   VALUES(?,?,'linked','manual_channel_inventory_review',?)
                   ON CONFLICT(amazon_listing_id) DO UPDATE SET product_id=excluded.product_id,match_status='linked',
                   match_method=excluded.match_method,linked_at=excluded.linked_at""",
                (listing_id,product_id,datetime.now(timezone.utc).isoformat()))
            changed = connection.execute(
                "UPDATE amazon_order_item_history SET product_id=? WHERE amazon_order_id=? AND order_item_id=?",
                (product_id,source.order_id,source.order_line_id)).rowcount
        elif source.channel == "walmart":
            listing_id = _resolve_walmart_listing_identity(connection,source)
            link_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(walmart_product_links)")}
            existing_link = connection.execute(
                "SELECT walmart_product_link_id FROM walmart_product_links WHERE walmart_listing_id=?",(listing_id,)).fetchone()
            if "matched_at" in link_columns:
                if existing_link:
                    connection.execute("UPDATE walmart_product_links SET product_id=?,match_status='linked',matched_at=? WHERE walmart_product_link_id=?",
                                       (product_id,datetime.now(timezone.utc).isoformat(),existing_link[0]))
                else:
                    connection.execute("INSERT INTO walmart_product_links(walmart_listing_id,product_id,match_status,matched_at) VALUES(?,?,'linked',?)",
                                       (listing_id,product_id,datetime.now(timezone.utc).isoformat()))
            else:
                if existing_link:
                    connection.execute("UPDATE walmart_product_links SET product_id=?,match_status='linked' WHERE walmart_product_link_id=?",
                                       (product_id,existing_link[0]))
                else:
                    connection.execute("INSERT INTO walmart_product_links(walmart_listing_id,product_id,match_status) VALUES(?,?,'linked')",
                                       (listing_id,product_id))
            changed = connection.execute(
                "UPDATE walmart_order_lines SET product_id=? WHERE purchase_order_id=? AND order_line_id=?",
                (product_id,source.order_id,source.order_line_id)).rowcount
        else:
            raise ValueError("Unsupported channel")
        if changed != 1:
            raise RuntimeError("The intended marketplace order line was not updated exactly once")
        after = inventory_fingerprint(connection)
        if before != after:
            raise RuntimeError("Mapping confirmation unexpectedly changed inventory")
        connection.commit()
        return {"status":"mapping_confirmed","channel":source.channel,"product_id":product_id,
                "affected_order_lines":int(changed),"inventory_unchanged":True}
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def mark_reviewed(database: str | Path, channel: str, order_id: str, order_line_id: str,
                  actor: str, *, confirmation: str) -> dict:
    if confirmation != EXPLICIT_REVIEW_CONFIRMATION:
        raise ValueError("Exact review-only confirmation is required")
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        before = inventory_fingerprint(connection)
        source = load_source_line(connection,channel,order_id,order_line_id)
        now = datetime.now(timezone.utc).isoformat()
        details = json.dumps({"review_type":"channel_inventory_safe_candidate","reviewed_by":actor,
                              "reviewed_at":now,"inventory_mutation":False},separators=(",",":"))
        connection.execute(
            """INSERT INTO operations_work_queue(task_key,task_type,title,details,priority,status,source_channel,
                   source_reference,product_id,requested_quantity,created_at,updated_at,completed_at)
               VALUES(?,?,?,?,?,'completed',?,?,?,?,?,?,?)
               ON CONFLICT(task_key) DO UPDATE SET details=excluded.details,status='completed',updated_at=excluded.updated_at,
                   completed_at=excluded.completed_at""",
            (f"channel-inventory-review:{source.channel}:{source.order_id}:{source.order_line_id}","channel_inventory_review",
             f"Reviewed {source.channel} order {source.order_id} line {source.order_line_id}",details,"normal",source.channel,
             f"{source.order_id}|{source.order_line_id}",source.product_id,source.quantity,now,now,now))
        if before != inventory_fingerprint(connection):
            raise RuntimeError("Review metadata unexpectedly changed inventory")
        connection.commit()
        return {"status":"reviewed","inventory_unchanged":True}
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()
