"""Shared, explicit marketplace publishing workflow for Walmart and Amazon."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable

from app.database_resolution import configured_sqlite_path


CHANNELS = ("walmart", "amazon")
FINAL_OR_IN_FLIGHT = {"SUBMITTED", "PROCESSING", "PUBLISHED", "ALREADY_LISTED"}
VALID_STATUSES = {
    "DRAFT", "NEEDS_ATTENTION", "READY", "SUBMITTED", "PROCESSING",
    "PUBLISHED", "FAILED", "ALREADY_LISTED",
}
MARKETPLACE_ID = "ATVPDKIKX0DER"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS marketplace_publish_queue (
    publish_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL CHECK(channel IN ('walmart','amazon')),
    product_id INTEGER NOT NULL,
    seller_sku TEXT NOT NULL,
    gtin TEXT,
    external_catalog_id TEXT,
    catalog_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    submission_type TEXT NOT NULL,
    proposed_price NUMERIC,
    proposed_quantity INTEGER NOT NULL DEFAULT 0,
    fulfillment_type TEXT NOT NULL DEFAULT 'merchant',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    idempotency_key TEXT NOT NULL UNIQUE,
    external_submission_id TEXT,
    submitted_at TEXT,
    processed_at TEXT,
    last_checked_at TEXT,
    validation_json TEXT NOT NULL DEFAULT '[]',
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(product_id) REFERENCES products(product_id),
    UNIQUE(channel, product_id)
);
CREATE INDEX IF NOT EXISTS ix_marketplace_publish_status
    ON marketplace_publish_queue(status, channel, product_id);
CREATE TABLE IF NOT EXISTS marketplace_publish_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    status_before TEXT,
    status_after TEXT,
    event_data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(publish_id) REFERENCES marketplace_publish_queue(publish_id)
);
CREATE INDEX IF NOT EXISTS ix_marketplace_publish_events_publish
    ON marketplace_publish_events(publish_id, created_at);
"""


class PublishError(RuntimeError):
    """Safe operator-facing workflow error."""


class DuplicateSubmission(PublishError):
    pass


class ValidationBlocked(PublishError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class SubmissionResult:
    status: str
    external_submission_id: str | None = None
    external_catalog_id: str | None = None
    response: dict[str, Any] | None = None
    error: str | None = None


Submitter = Callable[[dict[str, Any]], SubmissionResult]
StatusFetcher = Callable[[dict[str, Any]], SubmissionResult]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_gtin(value: Any) -> str:
    return "".join(character for character in _text(value) if character.isdigit())


def _valid_gtin(value: str) -> bool:
    return len(value) in {8, 12, 13, 14}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _safe_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def connect(database: str | Path | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database or configured_sqlite_path()), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def apply_schema(connection: sqlite3.Connection) -> None:
    """Apply the additive schema. Tests call this only against temporary databases."""
    connection.executescript(SCHEMA_SQL)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(connection, name):
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')}


def _first(columns: set[str], *names: str) -> str | None:
    return next((name for name in names if name in columns), None)


def _configured(channel: str) -> bool:
    if channel == "walmart":
        return bool(os.getenv("WALMART_CLIENT_ID") and os.getenv("WALMART_CLIENT_SECRET"))
    return bool(
        (os.getenv("AMAZON_LWA_CLIENT_ID") or os.getenv("SP_API_CLIENT_ID") or os.getenv("LWA_CLIENT_ID"))
        and (os.getenv("AMAZON_LWA_CLIENT_SECRET") or os.getenv("SP_API_CLIENT_SECRET") or os.getenv("LWA_CLIENT_SECRET"))
        and (os.getenv("AMAZON_REFRESH_TOKEN") or os.getenv("SP_API_REFRESH_TOKEN") or os.getenv("LWA_REFRESH_TOKEN"))
    )


def _primary_gtin(connection: sqlite3.Connection, product_id: int) -> str:
    row = connection.execute(
        """SELECT barcode FROM product_barcodes WHERE product_id=?
           ORDER BY is_primary DESC, barcode_id LIMIT 1""", (product_id,)
    ).fetchone()
    return _normalized_gtin(row[0] if row else "")


def _images(connection: sqlite3.Connection, product_id: int) -> list[dict[str, Any]]:
    if not _table_exists(connection, "product_images"):
        return []
    columns = _columns(connection, "product_images")
    value_column = _first(columns, "image_url", "image_path", "url", "source_url", "external_url")
    if not value_column:
        return []
    primary = "is_primary" if "is_primary" in columns else "0"
    image_type = "image_type" if "image_type" in columns else "''"
    rows = connection.execute(
        f"""SELECT image_id, {value_column} image_source, {primary} is_primary,
                   {image_type} image_type FROM product_images
            WHERE product_id=? AND TRIM(COALESCE({value_column},''))<>''
            ORDER BY {primary} DESC, image_id""", (product_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def available_quantity(connection: sqlite3.Connection, product_id: int) -> int:
    """Physical on-hand less existing reservations; publishing itself never changes it."""
    row = connection.execute(
        """SELECT COALESCE(SUM(MAX(COALESCE(quantity_on_hand,0)-COALESCE(quantity_reserved,0),0)),0)
             FROM inventory WHERE product_id=?""", (product_id,)
    ).fetchone()
    return int(row[0] or 0)


def _existing_listing(connection: sqlite3.Connection, channel: str, product_id: int) -> dict[str, Any] | None:
    listing_table = f"{channel}_listings"
    link_table = f"{channel}_product_links"
    if not (_table_exists(connection, listing_table) and _table_exists(connection, link_table)):
        return None
    listings = _columns(connection, listing_table)
    links = _columns(connection, link_table)
    listing_key = _first(listings, f"{channel}_listing_id", "listing_id", "id")
    link_key = _first(links, f"{channel}_listing_id", "listing_id")
    if not listing_key or not link_key or "product_id" not in links:
        return None
    sku = _first(listings, "seller_sku", "sku")
    catalog = _first(listings, "asin", "walmart_item_id", "external_product_id", "item_id", "wpid")
    price = _first(listings, f"{channel}_price", "listed_price", "price")
    quantity = _first(listings, f"{channel}_quantity", "quantity_available", "quantity")
    select = [f'l."{listing_key}" listing_id']
    select += [f'l."{sku}" seller_sku' if sku else "NULL seller_sku"]
    select += [f'l."{catalog}" external_catalog_id' if catalog else "NULL external_catalog_id"]
    select += [f'l."{price}" current_price' if price else "NULL current_price"]
    select += [f'l."{quantity}" current_quantity' if quantity else "NULL current_quantity"]
    match = "AND lower(COALESCE(x.match_status,'linked')) IN ('linked','matched')" if "match_status" in links else ""
    row = connection.execute(
        f"""SELECT {','.join(select)} FROM {link_table} x JOIN {listing_table} l
               ON l."{listing_key}"=x."{link_key}"
             WHERE x.product_id=? {match} ORDER BY l."{listing_key}" LIMIT 1""", (product_id,)
    ).fetchone()
    return dict(row) if row else None


def _catalog_match(connection: sqlite3.Connection, channel: str, gtin: str) -> dict[str, Any]:
    if not gtin:
        return {"status": "INVALID_IDENTIFIER", "external_catalog_id": None}
    normalized = gtin.lstrip("0") or "0"
    if channel == "walmart" and _table_exists(connection, "walmart_catalog_matches"):
        columns = _columns(connection, "walmart_catalog_matches")
        item = _first(columns, "walmart_item_id", "item_id", "wpid", "us_item_id")
        status = _first(columns, "match_status", "status")
        match_terms = [column for column in ("barcode_exact", "query_value", "barcode_lookup") if column in columns]
        if match_terms:
            where = " OR ".join(f"TRIM(CAST({column} AS TEXT)) IN (?,?)" for column in match_terms)
            params: list[str] = []
            for _ in match_terms:
                params.extend((gtin, normalized))
            row = connection.execute(
                f"SELECT {status or 'NULL'} match_status,{item or 'NULL'} external_catalog_id FROM walmart_catalog_matches WHERE {where} ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
            if row:
                value = _text(row["match_status"]).upper()
                return {"status": "MATCHED" if value in {"MATCH", "MATCHED", "FOUND"} else "NOT_FOUND" if value in {"NOT_FOUND", "NO_MATCH"} else "UNKNOWN", "external_catalog_id": row["external_catalog_id"]}
    if channel == "amazon":
        if _table_exists(connection, "amazon_catalog_match_audit"):
            columns = _columns(connection, "amazon_catalog_match_audit")
            barcode = _first(columns, "barcode", "gtin", "match_value", "identifier")
            asin = _first(columns, "asin", "external_catalog_id")
            status = _first(columns, "match_status", "status", "result")
            if barcode and asin:
                row = connection.execute(
                    f"SELECT {asin} external_catalog_id,{status or 'NULL'} match_status FROM amazon_catalog_match_audit WHERE ltrim(TRIM(CAST({barcode} AS TEXT)),'0')=? ORDER BY rowid DESC LIMIT 1",
                    (normalized,),
                ).fetchone()
                if row:
                    matched = bool(_text(row["external_catalog_id"])) and _text(row["match_status"]).upper() not in {"NOT_FOUND", "NO_MATCH", "FAILED"}
                    return {"status": "MATCHED" if matched else "NOT_FOUND", "external_catalog_id": row["external_catalog_id"]}
        # Preserve ASINs already imported even when the link has not yet been approved.
        if _table_exists(connection, "amazon_listings"):
            columns = _columns(connection, "amazon_listings")
            barcode = _first(columns, "gtin", "upc", "barcode")
            if barcode and "asin" in columns:
                row = connection.execute(
                    f"SELECT asin external_catalog_id FROM amazon_listings WHERE ltrim(TRIM(CAST({barcode} AS TEXT)),'0')=? AND TRIM(COALESCE(asin,''))<>'' LIMIT 1",
                    (normalized,),
                ).fetchone()
                if row:
                    return {"status": "MATCHED", "external_catalog_id": row[0]}
    return {"status": "UNKNOWN", "external_catalog_id": None}


def _stable_sku(channel: str, product_id: int) -> str:
    return f"BH-{'WM' if channel == 'walmart' else 'AMZ'}-{product_id}"


def _sku_conflict(connection: sqlite3.Connection, channel: str, sku: str, product_id: int, gtin: str) -> bool:
    table = f"{channel}_listings"
    links = f"{channel}_product_links"
    if not _table_exists(connection, table):
        return False
    columns = _columns(connection, table)
    sku_column = _first(columns, "seller_sku", "sku")
    listing_key = _first(columns, f"{channel}_listing_id", "listing_id", "id")
    if not sku_column or not listing_key:
        return False
    if _table_exists(connection, links):
        link_columns = _columns(connection, links)
        link_key = _first(link_columns, f"{channel}_listing_id", "listing_id")
        if link_key and "product_id" in link_columns:
            row = connection.execute(
                f"SELECT x.product_id FROM {table} l LEFT JOIN {links} x ON x.{link_key}=l.{listing_key} WHERE l.{sku_column}=? LIMIT 1", (sku,)
            ).fetchone()
            return bool(row and row[0] is not None and int(row[0]) != product_id)
    gtin_column = _first(columns, "gtin", "upc", "barcode")
    if gtin_column:
        row = connection.execute(f"SELECT {gtin_column} FROM {table} WHERE {sku_column}=? LIMIT 1", (sku,)).fetchone()
        return bool(row and _normalized_gtin(row[0]) not in {"", gtin})
    return False


def _validation(product: dict[str, Any], channel: str, gtin: str, price: Any, quantity: int,
                available: int, images: list[dict[str, Any]], combined_quantity: int,
                sku_conflict: bool, submission_type: str) -> list[str]:
    reasons: list[str] = []
    if not _valid_gtin(gtin):
        reasons.append("Missing or invalid GTIN")
    try:
        valid_price = Decimal(str(price)) > 0
    except (InvalidOperation, TypeError, ValueError):
        valid_price = False
    if not valid_price:
        reasons.append("Missing or invalid marketplace price")
    if quantity < 0:
        reasons.append("Marketplace quantity cannot be negative")
    if quantity > available or combined_quantity > available:
        reasons.append("Combined channel quantities exceed BrooksHouse available inventory")
    if not images:
        reasons.append("Missing usable product image")
    if not _text(product.get("brand")):
        reasons.append("Missing brand")
    if sku_conflict:
        reasons.append("Seller SKU is already associated with a different product or GTIN")
    if submission_type == "new_catalog_product" and not _text(product.get("category")):
        reasons.append(f"Missing required {channel.title()} product type/category")
    return reasons


def search_products(connection: sqlite3.Connection, query: str, limit: int = 30) -> list[dict[str, Any]]:
    term = _text(query)
    if not term:
        return []
    wildcard = f"%{term}%"
    numeric_id = int(term) if term.isdigit() else -1
    rows = connection.execute(
        """SELECT DISTINCT p.product_id,p.product_name,p.brand,p.store_price,
                  (SELECT pb.barcode FROM product_barcodes pb WHERE pb.product_id=p.product_id ORDER BY pb.is_primary DESC,pb.barcode_id LIMIT 1) gtin
             FROM products p LEFT JOIN product_barcodes b ON b.product_id=p.product_id
            WHERE p.product_id=? OR p.product_name LIKE ? OR b.barcode LIKE ?
               OR EXISTS (SELECT 1 FROM amazon_product_links apl JOIN amazon_listings al ON al.amazon_listing_id=apl.amazon_listing_id
                           WHERE apl.product_id=p.product_id AND (al.seller_sku LIKE ? OR al.asin LIKE ?))
               OR EXISTS (SELECT 1 FROM walmart_product_links wpl JOIN walmart_listings wl ON wl.walmart_listing_id=wpl.walmart_listing_id
                           WHERE wpl.product_id=p.product_id AND wl.seller_sku LIKE ?)
            ORDER BY p.product_name LIMIT ?""",
        (numeric_id, wildcard, wildcard, wildcard, wildcard, wildcard, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def product_center(connection: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    product_row = connection.execute(
        "SELECT product_id,product_name,brand,description,category,size_value,size_unit,pack_quantity,store_price FROM products WHERE product_id=?",
        (product_id,),
    ).fetchone()
    if not product_row:
        raise KeyError(product_id)
    product = dict(product_row)
    gtin = _primary_gtin(connection, product_id)
    images = _images(connection, product_id)
    available = available_quantity(connection, product_id)
    queues = {
        row["channel"]: dict(row) for row in connection.execute(
            "SELECT * FROM marketplace_publish_queue WHERE product_id=?", (product_id,)
        ).fetchall()
    } if _table_exists(connection, "marketplace_publish_queue") else {}
    channels: dict[str, Any] = {}
    proposed_total = 0
    for channel in CHANNELS:
        listing = _existing_listing(connection, channel, product_id)
        catalog = _catalog_match(connection, channel, gtin)
        queue = queues.get(channel)
        sku = _text((listing or {}).get("seller_sku") or (queue or {}).get("seller_sku")) or _stable_sku(channel, product_id)
        price = (queue or {}).get("proposed_price")
        quantity = int((queue or {}).get("proposed_quantity") or 0)
        proposed_total += quantity
        submission_type = "seller_offer" if catalog["status"] == "MATCHED" else "new_catalog_product"
        channels[channel] = {
            "channel": channel, "configured": _configured(channel), "listing": listing,
            "catalog_status": "MATCHED" if listing else catalog["status"],
            "external_catalog_id": (listing or {}).get("external_catalog_id") or catalog["external_catalog_id"],
            "seller_sku": sku, "current_price": (listing or {}).get("current_price"),
            "proposed_price": price, "proposed_quantity": quantity,
            "submission_type": submission_type, "queue": queue,
        }
    for channel, state in channels.items():
        other_total = proposed_total - int(state["proposed_quantity"])
        reasons = _validation(product, channel, gtin, state["proposed_price"], int(state["proposed_quantity"]),
                              available, images, other_total + int(state["proposed_quantity"]),
                              _sku_conflict(connection, channel, state["seller_sku"], product_id, gtin), state["submission_type"])
        if state["listing"]:
            status = "ALREADY_LISTED"
        elif state["queue"] and state["queue"]["status"] in FINAL_OR_IN_FLIGHT | {"FAILED"}:
            status = state["queue"]["status"]
        else:
            status = "READY" if not reasons else "NEEDS_ATTENTION"
        state.update({"status": status, "validation": reasons})
    events = []
    if _table_exists(connection, "marketplace_publish_events"):
        events = [dict(row) for row in connection.execute(
            """SELECT e.*,q.channel FROM marketplace_publish_events e JOIN marketplace_publish_queue q ON q.publish_id=e.publish_id
                 WHERE q.product_id=? ORDER BY e.event_id DESC LIMIT 50""", (product_id,)
        ).fetchall()]
    return {"product": product, "gtin": gtin, "images": images, "available_quantity": available,
            "remaining_quantity": max(available - proposed_total, 0), "proposed_total": proposed_total,
            "channels": channels, "events": events, "amazon_marketplace_id": os.getenv("AMAZON_MARKETPLACE_ID") or os.getenv("SP_API_MARKETPLACE_ID") or MARKETPLACE_ID}


def prepare(connection: sqlite3.Connection, channel: str, product_id: int, price: Any, quantity: int,
            seller_sku: str = "", image_ids: list[int] | None = None,
            overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError("Unsupported marketplace channel")
    apply_schema(connection)
    center = product_center(connection, product_id)
    state = center["channels"][channel]
    if state["listing"]:
        raise DuplicateSubmission(f"Product is already listed on {channel.title()}")
    gtin = center["gtin"]
    sku = _text(seller_sku) or state["seller_sku"]
    other = sum(int(value["proposed_quantity"] or 0) for name, value in center["channels"].items() if name != channel)
    selected_images = [image for image in center["images"] if not image_ids or int(image["image_id"]) in image_ids]
    reasons = _validation(center["product"], channel, gtin, price, int(quantity), center["available_quantity"],
                          selected_images, other + int(quantity), _sku_conflict(connection, channel, sku, product_id, gtin), state["submission_type"])
    status = "READY" if not reasons else "NEEDS_ATTENTION"
    material = f"{channel}|{product_id}|{sku}|{gtin}"
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    now = _now()
    request_data = {"image_ids": image_ids or [int(image["image_id"]) for image in selected_images], "overrides": overrides or {}}
    existing = connection.execute("SELECT * FROM marketplace_publish_queue WHERE channel=? AND product_id=?", (channel, product_id)).fetchone()
    before = existing["status"] if existing else None
    connection.execute(
        """INSERT INTO marketplace_publish_queue(channel,product_id,seller_sku,gtin,external_catalog_id,catalog_status,
               submission_type,proposed_price,proposed_quantity,status,idempotency_key,validation_json,request_json,created_at,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(channel,product_id) DO UPDATE SET seller_sku=excluded.seller_sku,gtin=excluded.gtin,
               external_catalog_id=excluded.external_catalog_id,catalog_status=excluded.catalog_status,
               submission_type=excluded.submission_type,proposed_price=excluded.proposed_price,
               proposed_quantity=excluded.proposed_quantity,status=excluded.status,validation_json=excluded.validation_json,
               request_json=excluded.request_json,error_message=NULL,updated_at=excluded.updated_at""",
        (channel, product_id, sku, gtin, state["external_catalog_id"], state["catalog_status"], state["submission_type"],
         str(price) if price is not None else None, int(quantity), status, key, _json(reasons), _json(request_data), now, now),
    )
    row = connection.execute("SELECT * FROM marketplace_publish_queue WHERE channel=? AND product_id=?", (channel, product_id)).fetchone()
    _event(connection, int(row["publish_id"]), "PREPARED", before, status,
           {"seller_sku": sku, "gtin": gtin, "price": str(price), "quantity": int(quantity), "validation": reasons})
    connection.commit()
    return dict(row)


def _event(connection: sqlite3.Connection, publish_id: int, event_type: str, before: str | None,
           after: str | None, data: dict[str, Any]) -> None:
    # Only allowlisted operational data is persisted; credentials never enter the event payload.
    connection.execute(
        "INSERT INTO marketplace_publish_events(publish_id,event_type,status_before,status_after,event_data_json,created_at) VALUES(?,?,?,?,?,?)",
        (publish_id, event_type, before, after, _json(data), _now()),
    )


def submit(connection: sqlite3.Connection, publish_id: int, submitters: dict[str, Submitter] | None = None) -> dict[str, Any]:
    apply_schema(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone()
        if not row:
            raise KeyError(publish_id)
        queue = dict(row)
        if queue["status"] in FINAL_OR_IN_FLIGHT:
            raise DuplicateSubmission("This publication is already submitted, processing, published, or already listed")
        center = product_center(connection, int(queue["product_id"]))
        state = center["channels"][queue["channel"]]
        if state["listing"]:
            connection.execute("UPDATE marketplace_publish_queue SET status='ALREADY_LISTED',updated_at=? WHERE publish_id=?", (_now(), publish_id))
            _event(connection, publish_id, "DUPLICATE_BLOCKED", queue["status"], "ALREADY_LISTED", {"reason": "Existing linked listing"})
            connection.commit()
            raise DuplicateSubmission("The product is already linked to a marketplace listing")
        current = prepare_snapshot = _validation(
            center["product"], queue["channel"], center["gtin"], queue["proposed_price"], int(queue["proposed_quantity"]),
            center["available_quantity"], center["images"], center["proposed_total"],
            _sku_conflict(connection, queue["channel"], queue["seller_sku"], int(queue["product_id"]), center["gtin"]), queue["submission_type"])
        if current:
            connection.execute("UPDATE marketplace_publish_queue SET status='NEEDS_ATTENTION',validation_json=?,updated_at=? WHERE publish_id=?", (_json(current), _now(), publish_id))
            _event(connection, publish_id, "VALIDATION_BLOCKED", queue["status"], "NEEDS_ATTENTION", {"validation": current})
            connection.commit()
            raise ValidationBlocked(current)
        submitter = (submitters or {}).get(queue["channel"])
        if submitter is None:
            raise PublishError(f"{queue['channel'].title()} live publishing adapter is not enabled")
        payload = {"channel": queue["channel"], "product_id": queue["product_id"], "seller_sku": queue["seller_sku"],
                   "gtin": center["gtin"], "external_catalog_id": state["external_catalog_id"],
                   "submission_type": queue["submission_type"], "price": str(queue["proposed_price"]),
                   "quantity": int(queue["proposed_quantity"]), "idempotency_key": queue["idempotency_key"],
                   "marketplace_id": center["amazon_marketplace_id"] if queue["channel"] == "amazon" else None,
                   "content": {**center["product"], **_safe_json(queue["request_json"], {}).get("overrides", {})},
                   "images": center["images"]}
        result = submitter(payload)
        new_status = result.status if result.status in VALID_STATUSES else "FAILED"
        connection.execute(
            """UPDATE marketplace_publish_queue SET status=?,external_submission_id=?,external_catalog_id=COALESCE(?,external_catalog_id),
                   submitted_at=CASE WHEN ? IN ('SUBMITTED','PROCESSING','PUBLISHED') THEN ? ELSE submitted_at END,
                   processed_at=CASE WHEN ? IN ('PUBLISHED','FAILED') THEN ? ELSE processed_at END,response_json=?,error_message=?,updated_at=? WHERE publish_id=?""",
            (new_status, result.external_submission_id, result.external_catalog_id, new_status, _now(), new_status, _now(),
             _json(result.response or {}), result.error, _now(), publish_id),
        )
        _event(connection, publish_id, "SUBMISSION_RESULT", queue["status"], new_status,
               {"seller_sku": queue["seller_sku"], "gtin": center["gtin"], "price": str(queue["proposed_price"]),
                "quantity": queue["proposed_quantity"], "external_submission_id": result.external_submission_id,
                "external_catalog_id": result.external_catalog_id, "error": result.error})
        connection.commit()
        return dict(connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone())
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def refresh_status(connection: sqlite3.Connection, publish_id: int,
                   fetchers: dict[str, StatusFetcher] | None = None) -> dict[str, Any]:
    apply_schema(connection)
    row = connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone()
    if not row:
        raise KeyError(publish_id)
    queue = dict(row)
    fetcher = (fetchers or {}).get(queue["channel"])
    if fetcher is None:
        raise PublishError(f"{queue['channel'].title()} status adapter is not enabled")
    result = fetcher(queue)
    new_status = result.status if result.status in VALID_STATUSES else "FAILED"
    connection.execute(
        "UPDATE marketplace_publish_queue SET status=?,response_json=?,error_message=?,last_checked_at=?,processed_at=CASE WHEN ? IN ('PUBLISHED','FAILED') THEN ? ELSE processed_at END,updated_at=? WHERE publish_id=?",
        (new_status, _json(result.response or {}), result.error, _now(), new_status, _now(), _now(), publish_id),
    )
    _event(connection, publish_id, "STATUS_REFRESHED", queue["status"], new_status,
           {"external_submission_id": queue["external_submission_id"], "error": result.error})
    connection.commit()
    return dict(connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone())


def queue_rows(connection: sqlite3.Connection, status_filter: str = "", channel_filter: str = "") -> list[dict[str, Any]]:
    if not _table_exists(connection, "marketplace_publish_queue"):
        return []
    clauses, params = [], []
    if status_filter:
        clauses.append("q.status=?")
        params.append(status_filter.upper())
    if channel_filter in CHANNELS:
        clauses.append("q.channel=?")
        params.append(channel_filter)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"""SELECT q.*,p.product_name FROM marketplace_publish_queue q JOIN products p ON p.product_id=q.product_id
              {where} ORDER BY q.updated_at DESC,q.publish_id DESC LIMIT 250""", params
    ).fetchall()
    return [{**dict(row), "validation": _safe_json(row["validation_json"], [])} for row in rows]
