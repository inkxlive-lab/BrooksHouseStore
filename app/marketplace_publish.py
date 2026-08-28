"""Manual, fail-closed Walmart + Amazon Marketplace Publish Center."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database_resolution import configured_sqlite_path
from app.migrations.marketplace_publish_schema import schema_installed
from app.services.image_studio import _display_reference


CHANNELS = ("walmart", "amazon")
FINAL_SUBMISSION_STATUSES = {"SUBMITTED", "PROCESSING", "PUBLISHED", "ALREADY LISTED"}
AMAZON_MARKETPLACE_ID = "ATVPDKIKX0DER"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _digits(value: Any) -> str:
    return "".join(character for character in _text(value) if character.isdigit())


def _lookup(value: Any) -> str:
    digits = _digits(value)
    return (digits.lstrip("0") or "0") if digits else ""


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(connection, name):
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')}


def _configuration_available(channel: str) -> bool:
    if channel == "walmart":
        groups = (("WALMART_CLIENT_ID",), ("WALMART_CLIENT_SECRET",))
    else:
        groups = (
            ("AMAZON_LWA_CLIENT_ID", "SP_API_CLIENT_ID", "LWA_APP_ID", "LWA_CLIENT_ID"),
            ("AMAZON_LWA_CLIENT_SECRET", "SP_API_CLIENT_SECRET", "LWA_CLIENT_SECRET"),
            ("AMAZON_REFRESH_TOKEN", "SP_API_REFRESH_TOKEN", "LWA_REFRESH_TOKEN"),
        )
    return all(any(_text(os.getenv(name)) for name in group) for group in groups)


def connect(database: str | Path | None = None) -> sqlite3.Connection:
    path = Path(database).resolve() if database is not None else configured_sqlite_path()
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _product(connection: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    if row is None:
        raise ValueError("Product not found")
    result = dict(row)
    barcode_rows = connection.execute(
        "SELECT * FROM product_barcodes WHERE product_id=? ORDER BY is_primary DESC,barcode_id",
        (product_id,),
    ).fetchall()
    result["barcodes"] = [dict(item) for item in barcode_rows]
    result["gtin"] = next((_digits(item["barcode"]) for item in barcode_rows if _digits(item["barcode"])), "")
    result["available_quantity"] = int(connection.execute(
        "SELECT COALESCE(SUM(MAX(COALESCE(quantity_on_hand,0)-COALESCE(quantity_reserved,0),0)),0) FROM inventory WHERE product_id=?",
        (product_id,),
    ).fetchone()[0] or 0)
    image_columns = _columns(connection, "product_images")
    if image_columns:
        rows = connection.execute(
            "SELECT * FROM product_images WHERE product_id=? ORDER BY is_primary DESC,image_id", (product_id,)
        ).fetchall()
        images = []
        for item in rows:
            record = dict(item)
            source = next((_text(record.get(name)) for name in ("image_url", "image_path", "url", "source_url", "external_url") if _text(record.get(name))), "")
            record["display_url"] = _display_reference(source) if source else ""
            images.append(record)
        result["images"] = images
    else:
        result["images"] = []
    result["primary_image"] = next((image for image in result["images"] if image.get("display_url")), None)
    return result


def _existing_walmart(connection: sqlite3.Connection, product_id: int) -> dict[str, Any] | None:
    if not {"walmart_listings", "walmart_product_links"} <= {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        return None
    listing_columns = _columns(connection, "walmart_listings")
    select = ["wl.walmart_listing_id", "wl.seller_sku"]
    for output, candidates in {
        "external_id": ("walmart_item_id", "item_id", "external_product_id", "wpid"),
        "price": ("walmart_price", "listed_price", "price"),
        "quantity": ("walmart_quantity", "quantity_available", "quantity"),
    }.items():
        column = next((name for name in candidates if name in listing_columns), None)
        select.append(f'wl."{column}" AS {output}' if column else f"NULL AS {output}")
    row = connection.execute(
        f"SELECT {','.join(select)} FROM walmart_product_links wpl JOIN walmart_listings wl USING(walmart_listing_id) "
        "WHERE wpl.product_id=? AND lower(COALESCE(wpl.match_status,'linked')) IN ('linked','matched','manual') "
        "ORDER BY wl.walmart_listing_id LIMIT 1",
        (product_id,),
    ).fetchone()
    return dict(row) if row else None


def _walmart_match(connection: sqlite3.Connection, gtin: str) -> dict[str, Any] | None:
    if not _table_exists(connection, "walmart_catalog_matches") or not gtin:
        return None
    columns = _columns(connection, "walmart_catalog_matches")
    select_names = [name for name in ("item_id", "walmart_item_id", "title", "brand", "description", "image_url", "match_status") if name in columns]
    if not select_names:
        return None
    predicates = [f"{name}=?" for name in ("barcode_lookup", "barcode_exact", "query_value") if name in columns]
    if not predicates:
        return None
    params = [_lookup(gtin) if "lookup" in predicate else gtin for predicate in predicates]
    row = connection.execute(
        f"SELECT {','.join(select_names)} FROM walmart_catalog_matches WHERE ({' OR '.join(predicates)}) "
        + ("AND upper(COALESCE(match_status,''))='MATCH' " if "match_status" in columns else "")
        + "ORDER BY rowid DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row else None


def _existing_amazon(connection: sqlite3.Connection, product_id: int) -> dict[str, Any] | None:
    if not (_table_exists(connection, "amazon_listings") and _table_exists(connection, "amazon_product_links")):
        return None
    row = connection.execute(
        "SELECT al.amazon_listing_id,al.seller_sku,al.asin AS external_id,al.amazon_price AS price,"
        "al.amazon_quantity AS quantity FROM amazon_product_links apl JOIN amazon_listings al USING(amazon_listing_id) "
        "WHERE apl.product_id=? AND lower(COALESCE(apl.match_status,'linked')) IN ('linked','matched','manual') "
        "ORDER BY al.amazon_listing_id LIMIT 1",
        (product_id,),
    ).fetchone()
    return dict(row) if row else None


def _amazon_match(connection: sqlite3.Connection, product_id: int, gtin: str) -> dict[str, Any] | None:
    if not _table_exists(connection, "amazon_catalog_match_audit"):
        return None
    columns = _columns(connection, "amazon_catalog_match_audit")
    if not {"asin", "matched_product_id", "result_status"} <= columns:
        return None
    row = connection.execute(
        "SELECT asin,result_status,identifiers_json FROM amazon_catalog_match_audit "
        "WHERE matched_product_id=? AND lower(result_status) IN ('unique','matched') ORDER BY checked_at DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    if row:
        return {"asin": row["asin"], "result_status": row["result_status"]}
    if gtin and "identifiers_json" in columns:
        for candidate in connection.execute(
            "SELECT asin,result_status,identifiers_json FROM amazon_catalog_match_audit WHERE identifiers_json IS NOT NULL"
        ):
            try:
                values = json.loads(candidate["identifiers_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if _lookup(gtin) in {_lookup(item[1]) for item in values if isinstance(item, (list, tuple)) and len(item) > 1}:
                return {"asin": candidate["asin"], "result_status": candidate["result_status"]}
    return None


def _queue_row(connection: sqlite3.Connection, channel: str, product_id: int) -> dict[str, Any] | None:
    if not schema_installed(connection):
        return None
    row = connection.execute(
        "SELECT * FROM marketplace_publish_queue WHERE channel=? AND product_id=?", (channel, product_id)
    ).fetchone()
    return dict(row) if row else None


def channel_state(connection: sqlite3.Connection, product: dict[str, Any], channel: str) -> dict[str, Any]:
    product_id = int(product["product_id"])
    gtin = product["gtin"]
    existing = _existing_walmart(connection, product_id) if channel == "walmart" else _existing_amazon(connection, product_id)
    match = _walmart_match(connection, gtin) if channel == "walmart" else _amazon_match(connection, product_id, gtin)
    external_id = (existing or {}).get("external_id")
    if channel == "walmart" and match and not external_id:
        external_id = match.get("item_id") or match.get("walmart_item_id")
    if channel == "amazon" and match and not external_id:
        external_id = match.get("asin")
    submission_type = "offer" if external_id else "new_product"
    queue = _queue_row(connection, channel, product_id)
    seller_sku = _text((existing or {}).get("seller_sku")) or _text((queue or {}).get("seller_sku")) or f"BH-{'WM' if channel == 'walmart' else 'AMZ'}-{product_id}"
    price = (queue or {}).get("proposed_price")
    if price is None:
        price = (existing or {}).get("price") if existing else product.get("store_price")
    quantity = int((queue or {}).get("proposed_quantity") or 0)
    selected_image_id = (queue or {}).get("selected_image_id") or ((product.get("primary_image") or {}).get("image_id"))
    return {
        "channel": channel,
        "configured": _configuration_available(channel),
        "marketplace_id": AMAZON_MARKETPLACE_ID if channel == "amazon" else None,
        "existing_listing": existing,
        "catalog_match": match,
        "catalog_status": "ALREADY_LISTED" if existing else ("MATCH" if external_id else "NOT_FOUND"),
        "submission_type": submission_type,
        "external_catalog_id": external_id,
        "seller_sku": seller_sku,
        "proposed_price": price,
        "proposed_quantity": quantity,
        "selected_image_id": selected_image_id,
        "queue": queue,
    }


def _price(value: Any) -> Decimal | None:
    try:
        result = Decimal(_text(value))
    except (InvalidOperation, ValueError):
        return None
    return result.quantize(Decimal("0.01")) if result > 0 else None


def validate_draft(connection: sqlite3.Connection, product: dict[str, Any], state: dict[str, Any], price: Any,
                   quantity: Any, seller_sku: str, selected_image_id: int | None) -> list[str]:
    errors: list[str] = []
    gtin = product["gtin"]
    proposed_price = _price(price)
    try:
        proposed_quantity = int(quantity)
    except (TypeError, ValueError):
        proposed_quantity = -1
    if len(gtin) not in {12, 13, 14}:
        errors.append("A valid 12, 13, or 14 digit UPC/EAN/GTIN is required.")
    if proposed_price is None:
        errors.append("A marketplace price greater than zero is required.")
    if proposed_quantity < 0:
        errors.append("Marketplace quantity cannot be negative.")
    if proposed_quantity > int(product["available_quantity"]):
        errors.append("Channel quantity exceeds genuine BrooksHouse availability.")
    if not seller_sku:
        errors.append("Seller SKU is required.")
    image = next((item for item in product["images"] if item.get("image_id") == selected_image_id and item.get("display_url")), None)
    if image is None:
        errors.append("Select an existing BrooksHouse product image before publishing.")
    if state["submission_type"] == "new_product":
        missing = [label for label, value in (("brand", product.get("brand")), ("description", product.get("description")), ("category", product.get("category"))) if not _text(value)]
        if missing:
            errors.append("New catalog product requires: " + ", ".join(missing) + ".")
        errors.append(
            f"{state['channel'].title()} required product-type attributes are not configured; "
            "new catalog-product submission is blocked."
        )
    if schema_installed(connection):
        other = connection.execute(
            "SELECT COALESCE(SUM(proposed_quantity),0) FROM marketplace_publish_queue WHERE product_id=? AND channel<>? "
            "AND status IN ('DRAFT','NEEDS ATTENTION','READY','SUBMITTED','PROCESSING')",
            (product["product_id"], state["channel"]),
        ).fetchone()[0]
        if proposed_quantity + int(other or 0) > int(product["available_quantity"]):
            errors.append("Combined Walmart and Amazon quantities exceed genuine BrooksHouse availability.")
        collision = connection.execute(
            "SELECT product_id,gtin FROM marketplace_publish_queue WHERE channel=? AND lower(seller_sku)=lower(?) AND product_id<>?",
            (state["channel"], seller_sku, product["product_id"]),
        ).fetchone()
        if collision:
            errors.append("Seller SKU is already assigned to a different BrooksHouse product/GTIN.")
    return errors


def _idempotency(channel: str, product_id: int, gtin: str, seller_sku: str, price: Decimal, quantity: int) -> str:
    raw = f"{channel}|{product_id}|{gtin}|{seller_sku.casefold()}|{price}|{quantity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_draft(connection: sqlite3.Connection, *, channel: str, product_id: int, seller_sku: str,
               proposed_price: Any, proposed_quantity: Any, selected_image_id: int | None) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError("Unsupported marketplace channel")
    if not schema_installed(connection):
        raise RuntimeError("Marketplace Publish Center schema is not installed.")
    product = _product(connection, product_id)
    state = channel_state(connection, product, channel)
    existing_sku = _text((state.get("existing_listing") or {}).get("seller_sku"))
    seller_sku = existing_sku or _text(seller_sku) or state["seller_sku"]
    price = _price(proposed_price)
    try:
        quantity = int(proposed_quantity)
    except (TypeError, ValueError):
        quantity = -1
    errors = validate_draft(connection, product, state, proposed_price, proposed_quantity, seller_sku, selected_image_id)
    status = "ALREADY LISTED" if state["existing_listing"] else ("READY" if not errors else "NEEDS ATTENTION")
    safe_price = price or Decimal("0.00")
    safe_quantity = max(quantity, 0)
    timestamp = _now()
    key = _idempotency(channel, product_id, product["gtin"], seller_sku, safe_price, safe_quantity)
    connection.execute(
        """INSERT INTO marketplace_publish_queue
        (channel,product_id,seller_sku,gtin,external_catalog_id,catalog_status,submission_type,selected_image_id,
         proposed_price,proposed_quantity,fulfillment_type,status,idempotency_key,validation_json,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?, 'merchant',?,?,?,?,?)
        ON CONFLICT(channel,product_id) DO UPDATE SET
          seller_sku=excluded.seller_sku,gtin=excluded.gtin,external_catalog_id=excluded.external_catalog_id,
          catalog_status=excluded.catalog_status,submission_type=excluded.submission_type,
          selected_image_id=excluded.selected_image_id,proposed_price=excluded.proposed_price,
          proposed_quantity=excluded.proposed_quantity,status=excluded.status,idempotency_key=excluded.idempotency_key,
          validation_json=excluded.validation_json,error_message=NULL,updated_at=excluded.updated_at
        WHERE marketplace_publish_queue.status NOT IN ('SUBMITTED','PROCESSING','PUBLISHED','ALREADY LISTED')""",
        (channel, product_id, seller_sku, product["gtin"], state["external_catalog_id"], state["catalog_status"],
         state["submission_type"], selected_image_id, str(safe_price), safe_quantity, status, key,
         json.dumps(errors), timestamp, timestamp),
    )
    row = _queue_row(connection, channel, product_id)
    _event(connection, row, "draft_saved", "blocked" if errors else "ready", errors="; ".join(errors) or None)
    connection.commit()
    return row or {}


def _event(connection: sqlite3.Connection, row: dict[str, Any], operation: str, result: str,
           *, errors: str | None = None, details: dict[str, Any] | None = None) -> None:
    connection.execute(
        """INSERT INTO marketplace_publish_events
        (publish_id,channel,product_id,occurred_at,operation,seller_sku,gtin,external_catalog_id,
         requested_price,requested_quantity,external_submission_id,result,error_details,details_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row.get("publish_id"), row["channel"], row["product_id"], _now(), operation, row.get("seller_sku"),
         row.get("gtin"), row.get("external_catalog_id"), row.get("proposed_price"), row.get("proposed_quantity"),
         row.get("external_submission_id"), result, errors, json.dumps(details or {}, separators=(",", ":"))),
    )


class PublishAdapter(Protocol):
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def refresh(self, row: dict[str, Any]) -> dict[str, Any]: ...


def submit_publish(connection: sqlite3.Connection, publish_id: int, adapter: PublishAdapter) -> dict[str, Any]:
    if not schema_installed(connection):
        raise RuntimeError("Marketplace Publish Center schema is not installed.")
    connection.execute("BEGIN IMMEDIATE")
    row_raw = connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone()
    if row_raw is None:
        connection.rollback(); raise ValueError("Publish queue record not found")
    row = dict(row_raw)
    if row["status"] in FINAL_SUBMISSION_STATUSES:
        connection.rollback(); return row
    product = _product(connection, int(row["product_id"]))
    state = channel_state(connection, product, row["channel"])
    errors = validate_draft(connection, product, state, row["proposed_price"], row["proposed_quantity"], row["seller_sku"], row["selected_image_id"])
    if errors or row["status"] != "READY":
        connection.rollback(); raise ValueError("Publish record is not ready: " + "; ".join(errors))
    payload = {
        "channel": row["channel"], "product_id": row["product_id"], "seller_sku": row["seller_sku"],
        "gtin": row["gtin"], "external_catalog_id": state["external_catalog_id"],
        "submission_type": state["submission_type"], "price": float(row["proposed_price"]),
        "quantity": row["proposed_quantity"], "image_id": row["selected_image_id"],
    }
    try:
        response = adapter.submit(payload)
        external = _text(response.get("external_submission_id") or response.get("feed_id") or response.get("submission_id"))
        external_catalog_id = _text(response.get("external_catalog_id") or response.get("asin") or response.get("item_id") or row.get("external_catalog_id")) or None
        status = _text(response.get("status") or "SUBMITTED").upper()
        if status not in {"SUBMITTED", "PROCESSING", "PUBLISHED"}:
            status = "SUBMITTED"
        timestamp = _now()
        connection.execute(
            "UPDATE marketplace_publish_queue SET status=?,external_submission_id=?,external_catalog_id=?,request_json=?,response_json=?,submitted_at=?,updated_at=? WHERE publish_id=? AND status='READY'",
            (status, external or None, external_catalog_id, json.dumps(payload, separators=(",", ":")),
             json.dumps(response, separators=(",", ":")), timestamp, timestamp, publish_id),
        )
        updated = dict(connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone())
        _event(connection, updated, "submit", status.lower())
        connection.commit()
        return updated
    except Exception as error:
        timestamp = _now()
        connection.execute(
            "UPDATE marketplace_publish_queue SET status='FAILED',error_message=?,updated_at=? WHERE publish_id=? AND status='READY'",
            (f"{type(error).__name__}: {error}", timestamp, publish_id),
        )
        failed = dict(connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone())
        _event(connection, failed, "submit", "failed", errors=failed["error_message"])
        connection.commit()
        return failed


def refresh_publish(connection: sqlite3.Connection, publish_id: int, adapter: PublishAdapter) -> dict[str, Any]:
    row_raw = connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone()
    if row_raw is None:
        raise ValueError("Publish queue record not found")
    row = dict(row_raw)
    if row["status"] not in {"SUBMITTED", "PROCESSING"}:
        return row
    response = adapter.refresh(row)
    status = _text(response.get("status") or row["status"]).upper()
    if status not in {"SUBMITTED", "PROCESSING", "PUBLISHED", "FAILED"}:
        status = row["status"]
    timestamp = _now()
    connection.execute(
        "UPDATE marketplace_publish_queue SET status=?,response_json=?,error_message=?,processed_at=CASE WHEN ? IN ('PUBLISHED','FAILED') THEN ? ELSE processed_at END,last_checked_at=?,updated_at=? WHERE publish_id=?",
        (status, json.dumps(response, separators=(",", ":")), _text(response.get("error")) or None,
         status, timestamp, timestamp, timestamp, publish_id),
    )
    updated = dict(connection.execute("SELECT * FROM marketplace_publish_queue WHERE publish_id=?", (publish_id,)).fetchone())
    _event(connection, updated, "status_refresh", status.lower(), errors=updated.get("error_message"))
    connection.commit()
    return updated


def publish_page_data(connection: sqlite3.Connection, product_id: int | None = None, queue_filter: str = "all") -> dict[str, Any]:
    products = [dict(row) for row in connection.execute(
        "SELECT product_id,product_name,brand,store_price FROM products WHERE active=1 ORDER BY product_name LIMIT 1000"
    )]
    product = _product(connection, product_id) if product_id else None
    states = {channel: channel_state(connection, product, channel) for channel in CHANNELS} if product else {}
    queue: list[dict[str, Any]] = []
    if schema_installed(connection):
        clauses, params = [], []
        filters = {
            "ready": ("status='READY'", ()), "needs_attention": ("status='NEEDS ATTENTION'", ()),
            "walmart_ready": ("channel='walmart' AND status='READY'", ()),
            "amazon_ready": ("channel='amazon' AND status='READY'", ()),
            "catalog_match": ("catalog_status='MATCH'", ()), "new_product": ("submission_type='new_product'", ()),
            "submitted": ("status='SUBMITTED'", ()), "processing": ("status='PROCESSING'", ()),
            "published": ("status='PUBLISHED'", ()), "failed": ("status='FAILED'", ()),
            "already_listed": ("status='ALREADY LISTED'", ()),
        }
        if queue_filter in filters:
            clauses.append(filters[queue_filter][0]); params.extend(filters[queue_filter][1])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        queue = [dict(row) for row in connection.execute(
            f"SELECT q.*,p.product_name FROM marketplace_publish_queue q JOIN products p USING(product_id) {where} ORDER BY q.updated_at DESC,q.publish_id DESC",
            params,
        )]
    return {"schema_installed": schema_installed(connection), "products": products, "product": product,
            "states": states, "queue": queue, "queue_filter": queue_filter}


def _require_operator(request: Request) -> None:
    user = getattr(request.state, "auth_user", None)
    if user is not None and getattr(user, "role", "") not in {"owner_admin", "manager"}:
        raise HTTPException(status_code=403, detail="Owner/admin or manager access is required.")


def install_marketplace_publish(app: FastAPI, templates: Jinja2Templates) -> None:
    @app.get("/channels/publish", response_class=HTMLResponse)
    def marketplace_publish_page(request: Request, product_id: int | None = None, queue_filter: str = "all",
                                 message: str = "", error: str = ""):
        _require_operator(request)
        with connect() as connection:
            data = publish_page_data(connection, product_id, queue_filter)
        return templates.TemplateResponse(request=request, name="marketplace_publish.html",
                                          context={**data, "message": message, "error": error})

    @app.post("/channels/publish/draft")
    def marketplace_publish_draft(request: Request, product_id: int = Form(...), channel: str = Form(...),
                                  seller_sku: str = Form(""), proposed_price: str = Form(""),
                                  proposed_quantity: str = Form("0"), selected_image_id: int | None = Form(None)):
        _require_operator(request)
        try:
            with connect() as connection:
                row = save_draft(connection, channel=channel, product_id=product_id, seller_sku=seller_sku,
                                 proposed_price=proposed_price, proposed_quantity=proposed_quantity,
                                 selected_image_id=selected_image_id)
            message = f"{channel.title()} draft saved as {row.get('status', 'DRAFT')}."
            return RedirectResponse(f"/channels/publish?product_id={product_id}&message={message.replace(' ', '+')}", status_code=303)
        except Exception as exc:
            return RedirectResponse(f"/channels/publish?product_id={product_id}&error={str(exc).replace(' ', '+')}", status_code=303)

    @app.post("/channels/publish/{publish_id}/submit")
    def marketplace_publish_submit(request: Request, publish_id: int):
        _require_operator(request)
        # Production adapters are intentionally not wired in this phase.  The
        # core submit boundary is exercised only with injected mocks in tests.
        raise HTTPException(status_code=503, detail="Real marketplace submissions are disabled pending explicit activation and approval.")

    templates.env.filters.setdefault("from_json", lambda value: json.loads(value or "[]"))
