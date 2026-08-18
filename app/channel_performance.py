from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "brookshouse_store.db"
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")
CHANNELS = ("shopify", "amazon", "walmart")


def _connect():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(connection, table, column):
    return any(row[1] == column for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _safe_days(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 30
    return value if value in {1, 7, 30, 60, 90, 365, 730} else 30


def _ensure_foundation(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS channel_content_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            external_id TEXT,
            sku TEXT,
            title TEXT,
            description TEXT,
            brand TEXT,
            price REAL,
            primary_image_url TEXT,
            images_json TEXT,
            attributes_json TEXT,
            source_json TEXT,
            synced_at TEXT NOT NULL,
            UNIQUE(product_id, channel_name, external_id)
        );
        CREATE TABLE IF NOT EXISTS channel_content_approvals (
            approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            source_channel TEXT NOT NULL,
            proposed_value TEXT,
            approval_status TEXT NOT NULL DEFAULT 'draft',
            approved_at TEXT,
            pushed_at TEXT,
            push_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _strip_html(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _json(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _nested(data, *paths):
    for path in paths:
        current = data
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, "", [], {}):
            return current
    return None


def _image_from_source(data):
    value = _nested(data, "image_url", "imageUrl", "primaryImageUrl", "image", "featured_image")
    if isinstance(value, dict):
        value = value.get("url") or value.get("src")
    if isinstance(value, str):
        return value
    images = _nested(data, "images", "product.images", "media")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("src") or first.get("imageUrl")
    return None


def _description_from_source(data):
    value = _nested(
        data, "description", "body_html", "bodyHtml", "productDescription",
        "shortDescription", "longDescription", "product.description",
    )
    if isinstance(value, list):
        value = "\n".join(str(x) for x in value)
    return _strip_html(value)


def _primary_barcode(connection, product_id):
    row = connection.execute(
        """SELECT barcode FROM product_barcodes WHERE product_id=?
           ORDER BY is_primary DESC, barcode_id LIMIT 1""", (product_id,)
    ).fetchone()
    return row[0] if row else None


def _barcodes(connection, product_id):
    return [str(row[0]) for row in connection.execute(
        "SELECT barcode FROM product_barcodes WHERE product_id=?", (product_id,)
    ).fetchall() if row[0]]


def _master_image(connection, product_id, barcodes):
    if _table_exists(connection, "product_images"):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(product_images)")}
        image_column = next((x for x in ("image_url", "image_path", "url", "source_url") if x in columns), None)
        if image_column:
            order = "is_primary DESC, image_id" if "is_primary" in columns else "image_id"
            row = connection.execute(
                f"SELECT {image_column} FROM product_images WHERE product_id=? AND {image_column} IS NOT NULL ORDER BY {order} LIMIT 1",
                (product_id,),
            ).fetchone()
            if row and row[0]:
                return row[0]
    if _table_exists(connection, "product_enrichment") and barcodes:
        placeholders = ",".join("?" for _ in barcodes)
        row = connection.execute(
            f"SELECT image_url FROM product_enrichment WHERE barcode IN ({placeholders}) AND image_url IS NOT NULL ORDER BY enrichment_id DESC LIMIT 1",
            barcodes,
        ).fetchone()
        if row:
            return row[0]
    return None


def _empty_content(channel):
    return {"channel": channel, "title": None, "description": None, "brand": None, "image_url": None,
            "price": None, "sku": None, "external_id": None, "status": "Not available", "source": None}


def _content_comparison(connection, product_id):
    _ensure_foundation(connection)
    product = connection.execute(
        "SELECT product_id, product_name, brand, description, store_price FROM products WHERE product_id=?",
        (product_id,),
    ).fetchone()
    if product is None:
        raise KeyError(product_id)
    barcodes = _barcodes(connection, product_id)
    normalized = {str(value).lstrip("0") or "0" for value in barcodes}
    master = {
        "channel": "BrooksHouse", "title": product["product_name"],
        "description": product["description"], "brand": product["brand"],
        "image_url": _master_image(connection, product_id, barcodes),
        "price": product["store_price"], "sku": None, "external_id": str(product_id),
        "status": "Master product", "source": "products",
    }
    result = {"master": master, "shopify": _empty_content("Shopify"),
              "amazon": _empty_content("Amazon"), "walmart": _empty_content("Walmart")}

    snapshots = connection.execute(
        """SELECT * FROM channel_content_snapshots WHERE product_id=?
           ORDER BY synced_at DESC, snapshot_id DESC""", (product_id,)
    ).fetchall()
    for row in snapshots:
        key = str(row["channel_name"] or "").lower()
        if key in result and result[key]["source"] is None:
            result[key] = {"channel": key.title(), "title": row["title"], "description": row["description"],
                           "brand": row["brand"],
                           "image_url": row["primary_image_url"], "price": row["price"], "sku": row["sku"],
                           "external_id": row["external_id"], "status": "Synced content", "source": "snapshot"}

    if _table_exists(connection, "channel_listings") and _table_exists(connection, "sales_channels") and normalized:
        placeholders = ",".join("?" for _ in normalized)
        rows = connection.execute(
            f"""SELECT lower(sc.channel_name) channel_name, cl.*
                FROM channel_listings cl JOIN sales_channels sc ON sc.channel_id=cl.channel_id
                WHERE ltrim(COALESCE(cl.barcode_lookup,cl.barcode_exact,cl.barcode_raw,''),'0') IN ({placeholders})
                ORDER BY cl.last_imported_at DESC""", tuple(normalized)
        ).fetchall()
        for row in rows:
            key = row["channel_name"]
            if key not in result or result[key]["source"] == "snapshot":
                continue
            data = _json(row["source_data"])
            result[key] = {"channel": key.title(), "title": row["listing_title"],
                           "brand": _nested(data, "brand", "vendor", "product.brand", "product.vendor"),
                           "description": _description_from_source(data), "image_url": _image_from_source(data),
                           "price": row["listed_price"], "sku": row["sku"],
                           "external_id": row["external_product_id"], "status": row["listing_status"] or "Imported",
                           "source": "channel_listings"}

    if _table_exists(connection, "amazon_product_links") and _table_exists(connection, "amazon_listings"):
        row = connection.execute(
            """SELECT al.asin, al.seller_sku, al.amazon_price, al.inventory_status,
                      (SELECT i.title FROM amazon_order_item_history i
                       WHERE i.asin=al.asin OR i.seller_sku=al.seller_sku
                       ORDER BY i.synced_at DESC LIMIT 1) title
               FROM amazon_product_links apl JOIN amazon_listings al ON al.amazon_listing_id=apl.amazon_listing_id
               WHERE apl.product_id=? AND lower(COALESCE(apl.match_status,'')) IN ('linked','matched')
               ORDER BY al.amazon_listing_id LIMIT 1""", (product_id,)
        ).fetchone()
        if row and result["amazon"]["source"] is None:
            result["amazon"] = {"channel": "Amazon", "title": row["title"], "description": None, "brand": None,
                                "image_url": None, "price": row["amazon_price"], "sku": row["seller_sku"],
                                "external_id": row["asin"], "status": row["inventory_status"], "source": "amazon_listings"}

    if _table_exists(connection, "walmart_catalog_matches") and normalized and result["walmart"]["source"] != "snapshot":
        placeholders = ",".join("?" for _ in normalized)
        row = connection.execute(
            f"""SELECT * FROM walmart_catalog_matches WHERE barcode_lookup IN ({placeholders})
                ORDER BY CASE WHEN upper(match_status)='MATCH' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1""",
            tuple(normalized),
        ).fetchone()
        if row:
            data = _json(row["source_data"])
            result["walmart"] = {"channel": "Walmart", "title": row["title"],
                                 "brand": _nested(data, "brand", "brandName", "product.brand"),
                                 "description": _description_from_source(data), "image_url": row["image_url"] or _image_from_source(data),
                                 "price": row["price_amount"], "sku": None, "external_id": row["walmart_item_id"],
                                 "status": row["match_status"], "source": "walmart_catalog_matches"}
    return dict(product), barcodes, result


def _set_primary_image(connection, product_id, image_url):
    if not _table_exists(connection, "product_images"):
        raise ValueError("The product_images table is unavailable.")
    info = {row[1]: row for row in connection.execute("PRAGMA table_info(product_images)")}
    image_column = next((name for name in ("image_url", "image_path", "url", "source_url") if name in info), None)
    if image_column is None:
        raise ValueError("The product_images table has no supported image field.")
    if "is_primary" in info:
        connection.execute("UPDATE product_images SET is_primary=0 WHERE product_id=?", (product_id,))
    columns = ["product_id", image_column]
    values = [product_id, image_url]
    if "image_type" in info:
        columns.append("image_type"); values.append("front")
    if "is_primary" in info:
        columns.append("is_primary"); values.append(1)
    now = datetime.now().astimezone().isoformat()
    for timestamp_column in ("created_at", "updated_at"):
        if timestamp_column in info:
            columns.append(timestamp_column); values.append(now)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO product_images ({','.join(columns)}) VALUES ({placeholders})", values
    )


def _recommended_content_choices(content):
    recommendations = {}
    source_priority = {
        "title": ("amazon", "shopify", "walmart"),
        "brand": ("amazon", "walmart", "shopify"),
        "description": ("amazon", "walmart", "shopify"),
        "image": ("walmart", "amazon", "shopify"),
    }
    master = content["master"]
    for field, sources in source_priority.items():
        content_field = "image_url" if field == "image" else field
        if master.get(content_field):
            continue
        for source in sources:
            value = content[source].get(content_field)
            if value is not None and str(value).strip():
                recommendations[field] = source
                break
    return recommendations


def _apply_content_choices(connection, product_id, choices):
    product, _barcodes_value, content = _content_comparison(connection, product_id)
    allowed_sources = set(content)
    changes = []
    product_updates = {}
    field_map = {"title": "product_name", "description": "description", "brand": "brand"}
    for field, product_column in field_map.items():
        source = str(choices.get(field) or "").lower()
        if not source:
            continue
        if source not in allowed_sources:
            raise ValueError(f"Invalid source selected for {field}.")
        value = content[source].get(field)
        if value is None or not str(value).strip():
            raise ValueError(f"{source.title()} has no {field} to apply.")
        value = str(value).strip()
        product_updates[product_column] = value
        changes.append((field, source, value))

    image_source = str(choices.get("image") or "").lower()
    if image_source:
        if image_source not in allowed_sources:
            raise ValueError("Invalid source selected for image.")
        image_url = content[image_source].get("image_url")
        if not image_url:
            raise ValueError(f"{image_source.title()} has no image to apply.")
        _set_primary_image(connection, product_id, image_url)
        changes.append(("primary_image", image_source, image_url))

    if not changes:
        raise ValueError("Select at least one field to update.")
    if product_updates:
        assignments = ",".join(f"{column}=?" for column in product_updates)
        connection.execute(
            f"UPDATE products SET {assignments} WHERE product_id=?",
            [*product_updates.values(), product_id],
        )
    now = datetime.now().astimezone().isoformat()
    for field, source, value in changes:
        connection.execute(
            """INSERT INTO channel_content_approvals
               (product_id,field_name,source_channel,proposed_value,approval_status,
                approved_at,push_status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (product_id, field, source, value, "applied_to_brookshouse", now,
             "not_published", now, now),
        )
    return len(changes)


def _load_report(days, search, sort_by, coverage):
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    with _connect() as connection:
        availability = {"shopify": _table_exists(connection, "shopify_sales_lines"),
                        "amazon": _table_exists(connection, "amazon_order_item_history"),
                        "walmart": _table_exists(connection, "walmart_order_lines")}
        walmart_revenue = availability["walmart"] and _column_exists(connection, "walmart_order_lines", "line_total")
        shopify_sql = "SELECT NULL product_id,0 units,0 revenue,0 orders WHERE 0"
        if availability["shopify"]:
            shopify_sql = """SELECT l.product_id,SUM(l.quantity) units,SUM(l.net_amount) revenue,COUNT(DISTINCT l.shopify_order_id) orders
              FROM shopify_sales_lines l JOIN shopify_sales_orders o ON o.shopify_order_id=l.shopify_order_id
              WHERE l.product_id IS NOT NULL AND o.test_order=0 AND o.cancelled_at IS NULL AND o.processed_at>=? GROUP BY l.product_id"""
        amazon_sql = "SELECT NULL product_id,0 units,0 revenue,0 orders WHERE 0"
        if availability["amazon"]:
            amazon_sql = """SELECT i.product_id,SUM(i.quantity_ordered) units,SUM(COALESCE(i.item_total,0)) revenue,COUNT(DISTINCT i.amazon_order_id) orders
              FROM amazon_order_item_history i JOIN amazon_order_history o ON o.amazon_order_id=i.amazon_order_id
              WHERE i.product_id IS NOT NULL AND o.created_time>=? AND UPPER(COALESCE(o.fulfillment_status,''))<>'CANCELLED' GROUP BY i.product_id"""
        walmart_sql = "SELECT NULL product_id,0 units,0 revenue,0 orders WHERE 0"
        if availability["walmart"]:
            revenue_expr = (
                "SUM(COALESCE(m.line_total,0))"
                if walmart_revenue
                else """SUM(
                    COALESCE(o.order_total,0) * COALESCE(m.quantity,0) /
                    NULLIF((SELECT SUM(COALESCE(wl2.quantity,0))
                            FROM walmart_order_lines wl2
                            WHERE wl2.purchase_order_id=o.purchase_order_id),0)
                )"""
            )
            walmart_sql = f"""WITH mapped AS (SELECT wl.*,COALESCE(wl.product_id,CASE WHEN
              (SELECT COUNT(DISTINCT pb.product_id) FROM product_barcodes pb WHERE ltrim(pb.barcode,'0')=ltrim(COALESCE(NULLIF(wl.upc,''),wl.sku),'0'))=1
              THEN (SELECT MIN(pb.product_id) FROM product_barcodes pb WHERE ltrim(pb.barcode,'0')=ltrim(COALESCE(NULLIF(wl.upc,''),wl.sku),'0')) END) mapped_product_id
              FROM walmart_order_lines wl) SELECT m.mapped_product_id product_id,SUM(m.quantity) units,{revenue_expr} revenue,
              COUNT(DISTINCT m.purchase_order_id) orders FROM mapped m JOIN walmart_orders o ON o.purchase_order_id=m.purchase_order_id
              WHERE m.mapped_product_id IS NOT NULL
                AND (CASE
                       WHEN trim(COALESCE(o.order_date,'')) GLOB '[0-9]*'
                        AND length(trim(COALESCE(o.order_date,''))) >= 13
                       THEN datetime(CAST(o.order_date AS INTEGER)/1000,'unixepoch')
                       ELSE datetime(o.order_date)
                     END) >= datetime(?)
                AND lower(COALESCE(o.walmart_status,'')) NOT LIKE '%cancel%'
              GROUP BY m.mapped_product_id"""
        sql = f"""WITH shopify AS ({shopify_sql}),amazon AS ({amazon_sql}),walmart AS ({walmart_sql}),ids AS
          (SELECT product_id FROM shopify UNION SELECT product_id FROM amazon UNION SELECT product_id FROM walmart)
          SELECT p.product_id,p.product_name,p.brand,(SELECT pb.barcode FROM product_barcodes pb WHERE pb.product_id=p.product_id
          ORDER BY pb.is_primary DESC,pb.barcode_id LIMIT 1) barcode,
          COALESCE(s.units,0) shopify_units,COALESCE(s.revenue,0) shopify_revenue,COALESCE(s.orders,0) shopify_orders,
          COALESCE(a.units,0) amazon_units,COALESCE(a.revenue,0) amazon_revenue,COALESCE(a.orders,0) amazon_orders,
          COALESCE(w.units,0) walmart_units,COALESCE(w.revenue,0) walmart_revenue,COALESCE(w.orders,0) walmart_orders
          FROM ids JOIN products p ON p.product_id=ids.product_id LEFT JOIN shopify s ON s.product_id=p.product_id
          LEFT JOIN amazon a ON a.product_id=p.product_id LEFT JOIN walmart w ON w.product_id=p.product_id
          WHERE (?='' OR lower(p.product_name) LIKE ? OR lower(COALESCE(p.brand,'')) LIKE ? OR EXISTS
          (SELECT 1 FROM product_barcodes pb WHERE pb.product_id=p.product_id AND pb.barcode LIKE ?))"""
        params = [cutoff for channel in CHANNELS if availability[channel]]
        term = search.strip().lower(); like = f"%{term}%"; params.extend([term, like, like, like])
        rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
        for row in rows:
            row["total_units"] = sum(int(row[f"{c}_units"] or 0) for c in CHANNELS)
            row["total_revenue"] = sum(float(row[f"{c}_revenue"] or 0) for c in CHANNELS)
            active = [c for c in CHANNELS if int(row[f"{c}_units"] or 0) > 0]
            row["channel_count"] = len(active)
            channel_units = {c: int(row[f"{c}_units"] or 0) for c in CHANNELS}
            best = max(channel_units, key=channel_units.get)
            row["best_channel"] = best.title() if channel_units[best] > 0 else "No sales"
            for channel in CHANNELS:
                units = int(row[f"{channel}_units"] or 0)
                row[f"{channel}_average_price"] = float(row[f"{channel}_revenue"] or 0) / units if units else 0
        if coverage == "multi": rows = [r for r in rows if r["channel_count"] >= 2]
        elif coverage == "single": rows = [r for r in rows if r["channel_count"] == 1]
        sort_keys = {"total_units": lambda r:(r["total_units"],r["total_revenue"]),
                     "total_revenue": lambda r:(r["total_revenue"],r["total_units"]),
                     "shopify": lambda r:(r["shopify_units"],r["shopify_revenue"]),
                     "amazon": lambda r:(r["amazon_units"],r["amazon_revenue"]),
                     "walmart": lambda r:(r["walmart_units"],r["walmart_revenue"])}
        rows.sort(key=sort_keys.get(sort_by,sort_keys["total_units"]),reverse=True)
        data_quality = {"shopify_unmatched":0,"amazon_unmatched":0,"walmart_unmatched":0}
        if availability["shopify"]: data_quality["shopify_unmatched"] = connection.execute(
            "SELECT COUNT(*) FROM shopify_sales_lines l JOIN shopify_sales_orders o ON o.shopify_order_id=l.shopify_order_id WHERE l.product_id IS NULL AND lower(COALESCE(l.match_status,'unmatched')) NOT IN ('generic_sale','ignored') AND o.processed_at>=?",(cutoff,)).fetchone()[0]
        if availability["amazon"]: data_quality["amazon_unmatched"] = connection.execute(
            "SELECT COUNT(*) FROM amazon_order_item_history i JOIN amazon_order_history o ON o.amazon_order_id=i.amazon_order_id WHERE i.product_id IS NULL AND o.created_time>=?",(cutoff,)).fetchone()[0]
        if availability["walmart"]: data_quality["walmart_unmatched"] = connection.execute(
            """SELECT COUNT(*) FROM walmart_order_lines wl JOIN walmart_orders o ON o.purchase_order_id=wl.purchase_order_id
               WHERE (CASE
                        WHEN trim(COALESCE(o.order_date,'')) GLOB '[0-9]*'
                         AND length(trim(COALESCE(o.order_date,''))) >= 13
                        THEN datetime(CAST(o.order_date AS INTEGER)/1000,'unixepoch')
                        ELSE datetime(o.order_date)
                      END) >= datetime(?)
                 AND wl.product_id IS NULL AND NOT EXISTS (SELECT 1 FROM product_barcodes pb
               WHERE ltrim(pb.barcode,'0')=ltrim(COALESCE(NULLIF(wl.upc,''),wl.sku),'0'))""",(cutoff,)).fetchone()[0]
    summary = {channel:{"units":sum(int(r[f"{channel}_units"] or 0) for r in rows),
                       "revenue":sum(float(r[f"{channel}_revenue"] or 0) for r in rows),
                       "orders":sum(int(r[f"{channel}_orders"] or 0) for r in rows)} for channel in CHANNELS}
    return rows,summary,availability,walmart_revenue,data_quality


def install_channel_performance(app: FastAPI):
    @app.get("/reports/channel-performance", response_class=HTMLResponse)
    def channel_performance_page(request: Request, days: int=30, search: str="", sort_by: str="total_units", coverage: str="all"):
        days=_safe_days(days); coverage=coverage if coverage in {"all","multi","single"} else "all"
        rows,summary,availability,walmart_revenue,data_quality=_load_report(days,search,sort_by,coverage)
        return TEMPLATES.TemplateResponse(request=request,name="channel_performance.html",context={
            "rows":rows,"summary":summary,"availability":availability,"walmart_revenue":walmart_revenue,
            "data_quality":data_quality,"days":days,"search":search,"sort_by":sort_by,"coverage":coverage})

    @app.get("/reports/channel-performance/product/{product_id}", response_class=HTMLResponse)
    def channel_product_detail(request: Request, product_id: int, saved: int=0, error: str=""):
        with _connect() as connection:
            try: product,barcodes,content=_content_comparison(connection,product_id)
            except KeyError: raise HTTPException(status_code=404,detail="Product not found")
        recommendations = _recommended_content_choices(content)
        return TEMPLATES.TemplateResponse(request=request,name="channel_product_compare.html",context={
            "product":product,"barcodes":barcodes,"content":content,"saved":saved,"error":error,
            "recommendations":recommendations})

    @app.post("/reports/channel-performance/product/{product_id}/apply")
    async def channel_product_apply(request: Request, product_id: int):
        form = await request.form()
        choices = {field: form.get(field + "_source") for field in ("title", "description", "brand", "image")}
        try:
            with _connect() as connection:
                changed = _apply_content_choices(connection, product_id, choices)
                connection.commit()
        except KeyError:
            raise HTTPException(status_code=404, detail="Product not found")
        except (ValueError, sqlite3.DatabaseError) as exc:
            from urllib.parse import quote
            return RedirectResponse(
                f"/reports/channel-performance/product/{product_id}?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            f"/reports/channel-performance/product/{product_id}?saved={changed}", status_code=303
        )

    @app.get("/reports/channel-performance/amazon-order/{amazon_order_id}", response_class=HTMLResponse)
    def amazon_order_detail(request: Request, amazon_order_id: str):
        with _connect() as connection:
            order=connection.execute("SELECT * FROM amazon_order_history WHERE amazon_order_id=?",(amazon_order_id,)).fetchone()
            if order is None: raise HTTPException(status_code=404,detail="Amazon order not found")
            lines=connection.execute("SELECT * FROM amazon_order_item_history WHERE amazon_order_id=? ORDER BY order_item_id",(amazon_order_id,)).fetchall()
        return TEMPLATES.TemplateResponse(request=request,name="amazon_order_detail.html",context={"order":dict(order),"lines":[dict(x) for x in lines]})

    from app.product_matching_queue import install_product_matching_queue
    install_product_matching_queue(app)
