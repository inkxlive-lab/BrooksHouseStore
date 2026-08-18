from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
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


def _columns(connection, table):
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _safe_days(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 90
    return value if value in {7, 30, 60, 90, 365, 730, 3650} else 90


def _text(value):
    return str(value or "").strip()


def _normalize(value):
    return " ".join(re.findall(r"[a-z0-9]+", _text(value).lower()))


def _barcode(value):
    digits = re.sub(r"\D", "", _text(value))
    return (digits.lstrip("0") or "0") if 8 <= len(digits) <= 14 else ""


def _is_generic(title):
    value = _normalize(title)
    if not value:
        return True
    generic_exact = {
        "custom sale", "open item", "open department", "misc", "miscellaneous",
        "general merchandise", "manual sale", "price override", "sale item",
    }
    return (
        value in generic_exact
        or bool(re.fullmatch(r"\d+(?:\.\d+)?\s*(?:dollar\s*)?(?:kitchen\s*)?sale", value))
        or bool(re.fullmatch(r"(?:kitchen\s*)?sale\s*\d+(?:\.\d+)?", value))
    )


def _source_key(channel, row):
    if channel == "shopify":
        choices = (
            ("variant", row.get("external_variant_id")),
            ("barcode", _barcode(row.get("barcode"))),
            ("sku", row.get("sku")),
            ("product", row.get("external_product_id")),
        )
        fallback = _normalize(f"{row.get('title', '')} {row.get('variant_title', '')}")
    elif channel == "amazon":
        choices = (("sku", row.get("sku")), ("asin", row.get("external_id")))
        fallback = _normalize(row.get("title"))
    else:
        choices = (("barcode", _barcode(row.get("barcode"))), ("sku", row.get("sku")))
        fallback = _normalize(row.get("title"))
    for kind, value in choices:
        value = _text(value)
        if value:
            return f"{channel}:{kind}:{value.lower()}"
    return f"{channel}:title:{fallback or 'unknown'}"


def _ensure_foundation(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS channel_sales_product_rules (
            rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT NOT NULL,
            source_key TEXT NOT NULL UNIQUE,
            source_title TEXT,
            source_sku TEXT,
            source_barcode TEXT,
            product_id INTEGER,
            rule_status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channel_match_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_title TEXT,
            source_sku TEXT,
            source_barcode TEXT,
            product_id INTEGER,
            action_name TEXT NOT NULL,
            match_method TEXT,
            confidence INTEGER,
            affected_lines INTEGER NOT NULL DEFAULT 0,
            affected_units INTEGER NOT NULL DEFAULT 0,
            affected_sales REAL NOT NULL DEFAULT 0,
            source_row_ids_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )


def _shopify_rows(connection, cutoff):
    if not (_table_exists(connection, "shopify_sales_lines") and _table_exists(connection, "shopify_sales_orders")):
        return []
    rows = connection.execute(
        """SELECT l.shopify_line_id row_id,l.shopify_order_id order_id,l.title,
                  l.variant_title,l.sku,l.barcode,l.shopify_product_id external_product_id,
                  l.shopify_variant_id external_variant_id,l.quantity units,l.net_amount sales,
                  o.processed_at sold_at
           FROM shopify_sales_lines l JOIN shopify_sales_orders o
             ON o.shopify_order_id=l.shopify_order_id
           WHERE l.product_id IS NULL AND o.test_order=0 AND o.cancelled_at IS NULL
             AND o.processed_at>=?
             AND lower(COALESCE(l.match_status,'unmatched')) NOT IN ('generic_sale','ignored')""",
        (cutoff,),
    ).fetchall()
    return [dict(row) for row in rows]


def _amazon_rows(connection, cutoff):
    if not (_table_exists(connection, "amazon_order_item_history") and _table_exists(connection, "amazon_order_history")):
        return []
    rows = connection.execute(
        """SELECT i.amazon_order_id||'|'||i.order_item_id row_id,i.amazon_order_id order_id,
                  i.title,NULL variant_title,i.seller_sku sku,NULL barcode,i.asin external_id,
                  i.quantity_ordered units,COALESCE(i.item_total,0) sales,o.created_time sold_at
           FROM amazon_order_item_history i JOIN amazon_order_history o
             ON o.amazon_order_id=i.amazon_order_id
           WHERE i.product_id IS NULL AND o.created_time>=?
             AND upper(COALESCE(o.fulfillment_status,''))<>'CANCELLED'""",
        (cutoff,),
    ).fetchall()
    return [dict(row) for row in rows]


def _walmart_rows(connection, cutoff):
    if not (_table_exists(connection, "walmart_order_lines") and _table_exists(connection, "walmart_orders")):
        return []
    rows = connection.execute(
        """SELECT CAST(wl.order_line_id AS TEXT) row_id,wl.purchase_order_id order_id,
                  wl.item_name title,NULL variant_title,wl.sku,wl.upc barcode,NULL external_id,
                  wl.quantity units,
                  COALESCE(o.order_total,0)*COALESCE(wl.quantity,0)/NULLIF(
                    (SELECT SUM(COALESCE(w2.quantity,0)) FROM walmart_order_lines w2
                     WHERE w2.purchase_order_id=wl.purchase_order_id),0) sales,
                  o.order_date sold_at
           FROM walmart_order_lines wl JOIN walmart_orders o
             ON o.purchase_order_id=wl.purchase_order_id
           WHERE wl.product_id IS NULL
             AND (CASE WHEN trim(COALESCE(o.order_date,'')) GLOB '[0-9]*'
                       AND length(trim(COALESCE(o.order_date,'')))>=13
                       THEN datetime(CAST(o.order_date AS INTEGER)/1000,'unixepoch')
                       ELSE datetime(o.order_date) END)>=datetime(?)
             AND lower(COALESCE(o.walmart_status,'')) NOT LIKE '%cancel%'
             AND NOT EXISTS (
                 SELECT 1 FROM product_barcodes pb
                 WHERE ltrim(pb.barcode,'0')=ltrim(COALESCE(NULLIF(wl.upc,''),wl.sku),'0')
             )""",
        (cutoff,),
    ).fetchall()
    return [dict(row) for row in rows]


def _group_unmatched(connection, days):
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
    loaders = {"shopify": _shopify_rows, "amazon": _amazon_rows, "walmart": _walmart_rows}
    groups = {}
    for channel, loader in loaders.items():
        for row in loader(connection, cutoff):
            key = _source_key(channel, row)
            if key not in groups:
                groups[key] = {
                    "channel": channel, "source_key": key, "title": _text(row.get("title")) or "Unknown item",
                    "variant_title": _text(row.get("variant_title")), "sku": _text(row.get("sku")),
                    "barcode": _text(row.get("barcode")), "external_id": _text(row.get("external_id")),
                    "external_product_id": _text(row.get("external_product_id")),
                    "external_variant_id": _text(row.get("external_variant_id")),
                    "lines": 0, "units": 0, "sales": 0.0, "row_ids": [], "latest_sale": "",
                }
            group = groups[key]
            group["lines"] += 1
            group["units"] += int(row.get("units") or 0)
            group["sales"] += float(row.get("sales") or 0)
            group["row_ids"].append(row["row_id"])
            group["latest_sale"] = max(group["latest_sale"], _text(row.get("sold_at")))
    for group in groups.values():
        group["generic"] = group["channel"] == "shopify" and _is_generic(group["title"])
    return list(groups.values())


def _product_catalog(connection):
    active_filter = "WHERE COALESCE(p.active,1)=1" if "active" in _columns(connection, "products") else ""
    rows = connection.execute(
        f"""SELECT p.product_id,p.product_name,p.brand,
                    GROUP_CONCAT(pb.barcode,'|') barcodes
             FROM products p LEFT JOIN product_barcodes pb ON pb.product_id=p.product_id
             {active_filter} GROUP BY p.product_id,p.product_name,p.brand
             ORDER BY p.product_name,p.product_id"""
    ).fetchall()
    products = []
    for row in rows:
        product = dict(row)
        product["normalized_title"] = _normalize(product["product_name"])
        product["barcode_keys"] = {_barcode(value) for value in _text(product["barcodes"]).split("|") if _barcode(value)}
        products.append(product)
    return products


def _candidate_indexes(products):
    by_barcode, by_title = defaultdict(list), defaultdict(list)
    for product in products:
        for value in product["barcode_keys"]:
            by_barcode[value].append(product)
        if product["normalized_title"]:
            by_title[product["normalized_title"]].append(product)
    return by_barcode, by_title


def _candidate(product, confidence, reason):
    return {"product_id": product["product_id"], "product_name": product["product_name"],
            "brand": product["brand"], "barcodes": product["barcodes"],
            "confidence": confidence, "reason": reason}


def _suggest(group, products, by_barcode, by_title):
    if group["generic"]:
        return []
    barcode_key = _barcode(group["barcode"])
    if barcode_key and len(by_barcode.get(barcode_key, [])) == 1:
        return [_candidate(by_barcode[barcode_key][0], 100, "Exact barcode")]
    title_key = _normalize(group["title"])
    if title_key and len(by_title.get(title_key, [])) == 1:
        return [_candidate(by_title[title_key][0], 92, "Exact normalized title")]
    if not title_key:
        return []
    scored = []
    for product in products:
        candidate_title = product["normalized_title"]
        if not candidate_title:
            continue
        score = SequenceMatcher(None, title_key, candidate_title).ratio()
        if score >= 0.58:
            scored.append((score, product))
    scored.sort(key=lambda item: (item[0], -item[1]["product_id"]), reverse=True)
    return [_candidate(product, round(score * 100), "Similar title") for score, product in scored[:3]]


def _load_queue(days, channel, search, review):
    with _connect() as connection:
        _ensure_foundation(connection)
        groups = _group_unmatched(connection, days)
        products = _product_catalog(connection)
        by_barcode, by_title = _candidate_indexes(products)
        term = _normalize(search)
        filtered = []
        for group in groups:
            if channel in CHANNELS and group["channel"] != channel:
                continue
            haystack = _normalize(" ".join((group["title"], group["variant_title"], group["sku"], group["barcode"])))
            if term and term not in haystack:
                continue
            group["candidates"] = _suggest(group, products, by_barcode, by_title)
            group["best_confidence"] = group["candidates"][0]["confidence"] if group["candidates"] else 0
            if review == "suggested" and not group["candidates"]:
                continue
            if review == "generic" and not group["generic"]:
                continue
            if review == "unresolved" and (group["candidates"] or group["generic"]):
                continue
            filtered.append(group)
        filtered.sort(key=lambda item: (item["generic"], -item["best_confidence"], -item["sales"], item["title"].lower()))
        summary = {
            "groups": len(filtered), "lines": sum(x["lines"] for x in filtered),
            "units": sum(x["units"] for x in filtered), "sales": sum(x["sales"] for x in filtered),
            "suggested": sum(bool(x["candidates"]) for x in filtered),
            "generic": sum(x["generic"] for x in filtered),
        }
        product_options = [
            {"product_id": p["product_id"], "label": f"{p['product_name']}" + (f" — {p['barcodes']}" if p["barcodes"] else "")}
            for p in products
        ]
    return filtered[:150], summary, product_options, len(filtered) > 150


def _find_group(connection, channel, source_key):
    for group in _group_unmatched(connection, 3650):
        if group["channel"] == channel and group["source_key"] == source_key:
            return group
    return None


def _map_group(connection, group, product_id, confidence):
    product = connection.execute("SELECT product_id FROM products WHERE product_id=?", (product_id,)).fetchone()
    if product is None:
        raise ValueError("The selected BrooksHouse product does not exist.")
    channel = group["channel"]
    if channel == "shopify":
        connection.executemany(
            "UPDATE shopify_sales_lines SET product_id=?,match_status='matched',match_method='manual_queue',updated_at=? WHERE shopify_line_id=? AND product_id IS NULL",
            [(product_id, datetime.now().astimezone().isoformat(), row_id) for row_id in group["row_ids"]],
        )
    elif channel == "amazon":
        pairs = [row_id.split("|", 1) for row_id in group["row_ids"]]
        connection.executemany(
            "UPDATE amazon_order_item_history SET product_id=? WHERE amazon_order_id=? AND order_item_id=? AND product_id IS NULL",
            [(product_id, order_id, item_id) for order_id, item_id in pairs],
        )
        if _table_exists(connection, "amazon_product_links") and _table_exists(connection, "amazon_listings"):
            connection.execute(
                """UPDATE amazon_product_links SET product_id=?,match_status='linked',
                          match_method='manual_queue',match_value=?,linked_at=?
                   WHERE amazon_listing_id IN (
                       SELECT amazon_listing_id FROM amazon_listings
                       WHERE (?<>'' AND seller_sku=?) OR (?<>'' AND asin=?)
                   )""",
                (product_id, group["source_key"], datetime.now().astimezone().isoformat(),
                 group["sku"], group["sku"], group["external_id"], group["external_id"]),
            )
    else:
        connection.executemany(
            "UPDATE walmart_order_lines SET product_id=? WHERE order_line_id=? AND product_id IS NULL",
            [(product_id, int(row_id)) for row_id in group["row_ids"]],
        )
    now = datetime.now().astimezone().isoformat()
    connection.execute(
        """INSERT INTO channel_sales_product_rules
           (channel_name,source_key,source_title,source_sku,source_barcode,product_id,rule_status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'active',?,?)
           ON CONFLICT(source_key) DO UPDATE SET product_id=excluded.product_id,
             rule_status='active',updated_at=excluded.updated_at""",
        (channel, group["source_key"], group["title"], group["sku"], group["barcode"], product_id, now, now),
    )
    _audit(connection, group, product_id, "match", "manual_queue", confidence)


def _exclude_generic(connection, group):
    if group["channel"] != "shopify" or not group["generic"]:
        raise ValueError("Only reviewed generic Shopify sales can be excluded.")
    now = datetime.now().astimezone().isoformat()
    connection.executemany(
        "UPDATE shopify_sales_lines SET match_status='generic_sale',match_method='reviewed_generic',updated_at=? WHERE shopify_line_id=? AND product_id IS NULL",
        [(now, row_id) for row_id in group["row_ids"]],
    )
    connection.execute(
        """INSERT INTO channel_sales_product_rules
           (channel_name,source_key,source_title,source_sku,source_barcode,product_id,rule_status,created_at,updated_at)
           VALUES (?,?,?,?,?,NULL,'excluded_generic',?,?)
           ON CONFLICT(source_key) DO UPDATE SET product_id=NULL,rule_status='excluded_generic',updated_at=excluded.updated_at""",
        (group["channel"], group["source_key"], group["title"], group["sku"], group["barcode"], now, now),
    )
    _audit(connection, group, None, "exclude_generic", "reviewed_generic", 100)


def _audit(connection, group, product_id, action, method, confidence):
    connection.execute(
        """INSERT INTO channel_match_audit
           (channel_name,source_key,source_title,source_sku,source_barcode,product_id,
            action_name,match_method,confidence,affected_lines,affected_units,affected_sales,
            source_row_ids_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (group["channel"], group["source_key"], group["title"], group["sku"], group["barcode"],
         product_id, action, method, confidence, group["lines"], group["units"], group["sales"],
         json.dumps(group["row_ids"]), datetime.now().astimezone().isoformat()),
    )


def install_product_matching_queue(app: FastAPI):
    @app.get("/reports/product-matching", response_class=HTMLResponse)
    def product_matching_page(request: Request, days: int = 90, channel: str = "all",
                              search: str = "", review: str = "all", saved: int = 0,
                              excluded: int = 0, error: str = ""):
        days = _safe_days(days)
        channel = channel if channel in {*CHANNELS, "all"} else "all"
        review = review if review in {"all", "suggested", "generic", "unresolved"} else "all"
        groups, summary, products, truncated = _load_queue(days, channel, search, review)
        return TEMPLATES.TemplateResponse(request=request, name="product_matching_queue.html", context={
            "groups": groups, "summary": summary, "products": products, "truncated": truncated,
            "days": days, "channel": channel, "search": search, "review": review,
            "saved": saved, "excluded": excluded, "error": error,
        })

    @app.post("/reports/product-matching/apply")
    async def product_matching_apply(request: Request):
        form = await request.form()
        channel = _text(form.get("channel")).lower()
        source_key = _text(form.get("source_key"))
        action = _text(form.get("action")) or "match"
        return_query = _text(form.get("return_query"))
        try:
            with _connect() as connection:
                _ensure_foundation(connection)
                group = _find_group(connection, channel, source_key)
                if group is None:
                    raise ValueError("Those sales lines were already resolved or could not be found.")
                if action == "exclude_generic":
                    _exclude_generic(connection, group)
                    connection.commit()
                    destination = "/reports/product-matching?excluded=1"
                else:
                    product_id = int(form.get("product_id") or 0)
                    if product_id <= 0:
                        raise ValueError("Choose a BrooksHouse product before confirming the match.")
                    confidence = int(form.get("confidence") or 0)
                    _map_group(connection, group, product_id, confidence)
                    connection.commit()
                    destination = f"/reports/product-matching?saved={group['lines']}"
                if return_query:
                    destination += "&" + return_query.lstrip("?").replace("saved=", "previous_saved=")
                return RedirectResponse(destination, status_code=303)
        except (ValueError, sqlite3.DatabaseError) as exc:
            return RedirectResponse(
                "/reports/product-matching?error=" + quote(str(exc)) + ("&" + return_query if return_query else ""),
                status_code=303,
            )

    @app.post("/reports/product-matching/bulk")
    async def product_matching_bulk(request: Request):
        form = await request.form()
        return_query = _text(form.get("return_query"))
        try:
            assignments = json.loads(_text(form.get("assignments")) or "[]")
            if not isinstance(assignments, list) or not assignments:
                raise ValueError("Select at least one queue item first.")
            if len(assignments) > 150:
                raise ValueError("A batch can contain no more than 150 visible queue items.")
            normalized = []
            seen = set()
            for item in assignments:
                if not isinstance(item, dict):
                    raise ValueError("The batch contained an invalid selection.")
                channel = _text(item.get("channel")).lower()
                source_key = _text(item.get("source_key"))
                action = _text(item.get("action")) or "match"
                identity = (channel, source_key)
                if channel not in CHANNELS or not source_key or identity in seen:
                    raise ValueError("The batch contained a duplicate or invalid queue item.")
                seen.add(identity)
                product_id = int(item.get("product_id") or 0)
                confidence = max(0, min(100, int(item.get("confidence") or 0)))
                if action == "match" and product_id <= 0:
                    raise ValueError("Every selected product match needs a BrooksHouse product ID.")
                if action not in {"match", "exclude_generic"}:
                    raise ValueError("The batch contained an unsupported action.")
                normalized.append((channel, source_key, action, product_id, confidence))

            with _connect() as connection:
                _ensure_foundation(connection)
                available = {
                    (group["channel"], group["source_key"]): group
                    for group in _group_unmatched(connection, 3650)
                }
                selected = []
                for channel, source_key, action, product_id, confidence in normalized:
                    group = available.get((channel, source_key))
                    if group is None:
                        raise ValueError("One selected item was already resolved. Refresh the queue and try again.")
                    if action == "exclude_generic" and not (channel == "shopify" and group["generic"]):
                        raise ValueError("Only reviewed generic Shopify sales can be bulk excluded.")
                    if action == "match" and connection.execute(
                        "SELECT 1 FROM products WHERE product_id=?", (product_id,)
                    ).fetchone() is None:
                        raise ValueError(f"BrooksHouse product #{product_id} does not exist.")
                    selected.append((group, action, product_id, confidence))

                matched_lines = 0
                excluded_groups = 0
                for group, action, product_id, confidence in selected:
                    if action == "exclude_generic":
                        _exclude_generic(connection, group)
                        excluded_groups += 1
                    else:
                        _map_group(connection, group, product_id, confidence)
                        matched_lines += group["lines"]
                connection.commit()

            destination = "/reports/product-matching?"
            if matched_lines:
                destination += f"saved={matched_lines}"
            if excluded_groups:
                destination += ("&" if matched_lines else "") + f"excluded={excluded_groups}"
            if return_query:
                destination += "&" + return_query.lstrip("?").replace("saved=", "previous_saved=")
            return RedirectResponse(destination, status_code=303)
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            return RedirectResponse(
                "/reports/product-matching?error=" + quote(str(exc)) + ("&" + return_query if return_query else ""),
                status_code=303,
            )
