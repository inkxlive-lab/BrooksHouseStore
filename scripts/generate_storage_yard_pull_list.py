"""Generate reconciled read-only Walmart/Amazon fulfillment-yard reports."""

from __future__ import annotations

import html
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATABASE = Path("app/data/brookshouse_store.db").resolve()
REPORT_DIR = Path("reports/fulfillment-yard/2026-08-23").resolve()
REFRESH_FINISHED = "2026-08-23T06:16:15-05:00"
MONDAY = "2026-08-24"


def text(value) -> str:
    return str(value or "").strip()


def lower(value) -> str:
    return text(value).casefold()


def normalize_deadline(value) -> str:
    raw = text(value)
    if raw.isdigit() and len(raw) >= 12:
        return datetime.fromtimestamp(int(raw) / 1000).astimezone().isoformat(timespec="minutes")
    return raw


def table_exists(connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main() -> int:
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    connection = sqlite3.connect(f"file:{DATABASE.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    barcodes = defaultdict(list)
    for row in connection.execute(
        "SELECT product_id, barcode, is_primary FROM product_barcodes ORDER BY is_primary DESC, barcode_id"
    ):
        barcodes[row["product_id"]].append(text(row["barcode"]))

    images = {}
    if table_exists(connection, "product_images"):
        for row in connection.execute(
            "SELECT product_id, image_url FROM product_images WHERE image_url IS NOT NULL AND TRIM(image_url)<>'' ORDER BY is_primary DESC, image_id"
        ):
            images.setdefault(row["product_id"], text(row["image_url"]))

    inventory = defaultdict(list)
    for row in connection.execute(
        """SELECT i.product_id, i.quantity_on_hand, i.quantity_reserved, i.container_id,
                  l.location_name, l.location_type
             FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
            WHERE i.quantity_on_hand<>0 OR i.quantity_reserved<>0
            ORDER BY l.location_name, i.container_id"""
    ):
        inventory[row["product_id"]].append(dict(row))

    product_rows = {
        row["product_id"]: dict(row)
        for row in connection.execute("SELECT product_id, product_name FROM products")
    }

    walmart_links = {}
    for row in connection.execute(
        """SELECT LOWER(TRIM(wl.seller_sku)) sku, wpl.product_id, wpl.match_status
             FROM walmart_listings wl JOIN walmart_product_links wpl USING(walmart_listing_id)"""
    ):
        walmart_links[row["sku"]] = (row["product_id"], text(row["match_status"]))

    amazon_links = {}
    for row in connection.execute(
        """SELECT LOWER(TRIM(al.seller_sku)) sku, LOWER(TRIM(COALESCE(al.asin,''))) asin,
                  apl.product_id, apl.match_status
             FROM amazon_listings al JOIN amazon_product_links apl USING(amazon_listing_id)"""
    ):
        amazon_links[(row["sku"], row["asin"])] = (row["product_id"], text(row["match_status"]))
        amazon_links.setdefault((row["sku"], ""), (row["product_id"], text(row["match_status"])))

    lines = []
    for row in connection.execute(
        """SELECT o.purchase_order_id order_id, o.customer_order_id, o.ship_by_date,
                  o.order_date, o.synced_at, l.order_line_id line_id, l.sku, l.upc,
                  l.item_name, l.quantity, l.product_id, l.line_status
             FROM walmart_orders o JOIN walmart_order_lines l USING(purchase_order_id)
            WHERE LOWER(TRIM(COALESCE(l.line_status,''))) IN ('created','acknowledged')
              AND LOWER(TRIM(COALESCE(o.walmart_status,''))) NOT LIKE '%cancel%'
              AND LOWER(TRIM(COALESCE(o.walmart_status,''))) NOT LIKE '%ship%'
              AND LOWER(TRIM(COALESCE(o.walmart_status,''))) NOT LIKE '%deliver%'"""
    ):
        item = dict(row)
        linked = walmart_links.get(lower(row["sku"]))
        item.update(channel="Walmart", identifier=text(row["sku"]), asin="")
        item["mapped_product_id"] = row["product_id"] or (linked[0] if linked else None)
        item["mapping_status"] = f"Linked ({linked[1] or 'saved'})" if linked else ("Linked (line)" if row["product_id"] else "Unmatched")
        lines.append(item)

    for row in connection.execute(
        """SELECT h.amazon_order_id order_id, h.created_time order_date,
                  h.synced_at, h.fulfillment_status, h.fulfilled_by,
                  i.order_item_id line_id, i.seller_sku sku, i.asin, i.title item_name,
                  i.quantity_ordered quantity, i.product_id
             FROM amazon_order_history h JOIN amazon_order_item_history i USING(amazon_order_id)
            WHERE UPPER(TRIM(COALESCE(h.fulfillment_status,'')))='UNSHIPPED'
              AND UPPER(TRIM(COALESCE(h.fulfilled_by,'')))='MERCHANT'
              AND i.quantity_ordered>0"""
    ):
        item = dict(row)
        linked = amazon_links.get((lower(row["sku"]), lower(row["asin"]))) or amazon_links.get((lower(row["sku"]), ""))
        item.update(channel="Amazon", customer_order_id=row["order_id"], ship_by_date="", upc="")
        item["identifier"] = " / ".join(x for x in (text(row["sku"]), text(row["asin"])) if x)
        item["mapped_product_id"] = row["product_id"] or (linked[0] if linked else None)
        item["mapping_status"] = f"Linked ({linked[1] or 'saved'})" if linked else ("Linked (line)" if row["product_id"] else "Unmatched")
        lines.append(item)

    groups = {}
    for line in lines:
        product_id = line["mapped_product_id"]
        key = f"product:{product_id}" if product_id else f"unmatched:{lower(line['channel'])}:{lower(line['identifier'])}:{lower(line['item_name'])}"
        group = groups.setdefault(key, {
            "key": key, "product_id": product_id, "product_name": "", "identifiers": set(),
            "total": 0, "walmart": 0, "amazon": 0, "orders": defaultdict(int),
            "ship_dates": set(), "lines": [], "mapping_statuses": set(),
        })
        group["total"] += int(line["quantity"] or 0)
        group[lower(line["channel"])] += int(line["quantity"] or 0)
        group["orders"][(line["channel"], text(line["order_id"]))] += int(line["quantity"] or 0)
        group["identifiers"].add(text(line["identifier"]) or "—")
        if text(line.get("ship_by_date")):
            group["ship_dates"].add(normalize_deadline(line["ship_by_date"]))
        group["mapping_statuses"].add(line["mapping_status"])
        group["lines"].append(line)

    results = []
    for group in groups.values():
        product_id = group["product_id"]
        product = product_rows.get(product_id, {})
        group["product_name"] = text(product.get("product_name")) or text(group["lines"][0]["item_name"]) or "Unknown product"
        fallback_barcode = text(group["lines"][0].get("upc"))
        if not fallback_barcode:
            candidate = text(group["lines"][0].get("identifier"))
            fallback_barcode = candidate if candidate.isdigit() and 8 <= len(candidate) <= 14 else "—"
        group["barcode"] = (barcodes.get(product_id) or [fallback_barcode])[0]
        group["image_url"] = images.get(product_id, "")
        stocks = inventory.get(product_id, []) if product_id else []
        group["known_on_hand"] = sum(int(x["quantity_on_hand"] or 0) for x in stocks)
        group["known_reserved"] = sum(int(x["quantity_reserved"] or 0) for x in stocks)
        group["known_available"] = group["known_on_hand"] - group["known_reserved"]
        group["locations"] = [
            f"{text(x['location_name']) or 'Unknown'} / {text(x['container_id']) or 'Loose'} — on hand {x['quantity_on_hand']}, reserved {x['quantity_reserved']}"
            for x in stocks if int(x["quantity_on_hand"] or 0) > 0
        ]
        location_blob = " ".join(group["locations"]).casefold()
        group["known_storage"] = any(word in location_blob for word in ("trailer", "storage", "container", "tote", "yard"))
        group["at_store"] = any("storefront" in text(x["location_name"]).casefold() and int(x["quantity_on_hand"] or 0)>0 for x in stocks)
        group["short"] = not product_id or group["known_available"] < group["total"]
        group["order_count"] = len(group["orders"])
        group["earliest_ship"] = min(group["ship_dates"]) if group["ship_dates"] else ""
        group["run1"] = bool(group["earliest_ship"] and group["earliest_ship"][:10] <= MONDAY)
        group["notes"] = []
        if not product_id:
            group["notes"].append("UNMATCHED — manual mapping review required; no mapping changed")
        if group["known_available"] < group["total"]:
            group["notes"].append(f"SHORT/UNKNOWN: {group['known_available']} known available vs {group['total']} needed")
        if group["amazon"] and not group["ship_dates"]:
            group["notes"].append("Amazon ship-by date is not stored locally; user stated Monday shipment requirement")
        if not group["locations"]:
            group["notes"].append("No positive known inventory location — manual search")
        for field in ("identifiers", "ship_dates", "mapping_statuses"):
            group[field] = sorted(group[field])
        group["orders_display"] = [f"{ch} {oid} ×{qty}" for (ch, oid), qty in sorted(group["orders"].items())]
        del group["orders"]
        results.append(group)

    results.sort(key=lambda g: (0 if g["run1"] else 1, g["earliest_ship"] or "9999", -g["order_count"], 0 if g["locations"] else 1, g["product_name"].casefold()))
    active_orders = sorted({(line["channel"], text(line["order_id"])) for line in lines})
    definite_impossible = sorted({(line["channel"], text(line["order_id"])) for group in results if not group["product_id"] for line in group["lines"]})
    shortage_allocation_pending = sorted({(line["channel"], text(line["order_id"])) for group in results if group["product_id"] and group["short"] for line in group["lines"]})
    minimum_short_orders = len(definite_impossible) + sum(
        min(g["order_count"], max(0, g["total"] - g["known_available"]))
        for g in results if g["product_id"] and g["short"]
    )
    summary = {
        "refresh_finished_at": REFRESH_FINISHED,
        "report_generated_at": generated,
        "active_orders": len(active_orders),
        "active_order_lines": len(lines),
        "units_required": sum(int(line["quantity"] or 0) for line in lines),
        "unique_aggregated_products": len(results),
        "walmart_orders": len({o for ch, o in active_orders if ch == "Walmart"}),
        "walmart_lines": sum(1 for line in lines if line["channel"] == "Walmart"),
        "walmart_units": sum(int(line["quantity"] or 0) for line in lines if line["channel"] == "Walmart"),
        "amazon_orders": len({o for ch, o in active_orders if ch == "Amazon"}),
        "amazon_lines": sum(1 for line in lines if line["channel"] == "Amazon"),
        "amazon_units": sum(int(line["quantity"] or 0) for line in lines if line["channel"] == "Amazon"),
        "unmatched_ambiguous_lines": sum(1 for line in lines if not line["mapped_product_id"]),
        "orders_cannot_complete_minimum": minimum_short_orders,
        "definitely_blocked_unmatched_orders": [f"{ch} {oid}" for ch, oid in definite_impossible],
        "shortage_allocation_pending_orders": [f"{ch} {oid}" for ch, oid in shortage_allocation_pending],
        "reconciliation": "PASS" if sum(g["total"] for g in results) == sum(int(line["quantity"] or 0) for line in lines) else "FAIL",
    }
    payload = {"summary": summary, "products": results}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "storage-yard-pull-list.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (REPORT_DIR / "storage-yard-pull-list.md").write_text(markdown(summary, results), encoding="utf-8")
    (REPORT_DIR / "storage-yard-printable.html").write_text(render_html(summary, results, mobile=False), encoding="utf-8")
    (REPORT_DIR / "storage-yard-mobile.html").write_text(render_html(summary, results, mobile=True), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    connection.close()
    return 0


def markdown(summary, groups) -> str:
    lines = ["# Storage Yard Fulfillment Pull List", "", f"Refresh completed: **{summary['refresh_finished_at']}**", f"Generated: **{summary['report_generated_at']}**", "", "## Reconciliation", ""]
    for key, value in summary.items():
        lines.append(f"- {key.replace('_',' ').title()}: {', '.join(value) if isinstance(value,list) else value}")
    sections = [
        ("A. RUN 1 — Must pull for Monday shipment", lambda g:g["run1"] or (g["amazon"]>0 and not g["earliest_ship"])),
        ("B. RUN 2 — Remaining active orders", lambda g:not g["run1"] and not (g["amazon"]>0 and not g["earliest_ship"])),
        ("C. MULTI-ORDER ITEMS", lambda g:g["order_count"]>1),
        ("D. KNOWN TRAILER/STORAGE ITEMS", lambda g:g["known_storage"]),
        ("E. UNKNOWN LOCATION / MANUAL SEARCH", lambda g:not g["locations"]),
        ("F. UNMATCHED OR AMBIGUOUS PRODUCT MAPPINGS", lambda g:not g["product_id"]),
        ("G. ALREADY AT STORE / ONLINE ORDERS RESERVED", lambda g:g["at_store"] or g["known_reserved"]>0),
        ("H. NEW ORDERS RECEIVED AFTER THE INITIAL REPORT", lambda g:False),
    ]
    for title, predicate in sections:
        lines += ["", f"## {title}", ""]
        selected = [g for g in groups if predicate(g)]
        if not selected:
            lines.append("None at report generation time.")
        for g in selected:
            lines += [f"### ☐ {g['product_name']} — {g['total']} required", "", f"- Product ID: {g['product_id'] or 'UNMATCHED'}; barcode: {g['barcode']}; marketplace ID: {', '.join(g['identifiers'])}", f"- Walmart {g['walmart']}; Amazon {g['amazon']}; completes {g['order_count']} customer order(s)", f"- Orders: {'; '.join(g['orders_display'])}", f"- Ship by: {', '.join(g['ship_dates']) or 'Not stored'}", f"- Inventory: on hand {g['known_on_hand']}; reserved {g['known_reserved']}; available {g['known_available']}", f"- Locations: {'; '.join(g['locations']) or 'UNKNOWN / MANUAL SEARCH'}", f"- Mapping: {', '.join(g['mapping_statuses'])}", f"- Notes: {'; '.join(g['notes']) or 'None'}", ""]
    return "\n".join(lines)


def render_html(summary, groups, mobile=False) -> str:
    rows=[]
    for g in groups:
        img=f'<img src="{html.escape(g["image_url"])}" alt="">' if g["image_url"] else ""
        checks=' '.join(f'<label>☐ {x}</label>' for x in ("Found","Partial","Not Found","Damaged","Loaded"))
        if mobile:
            rows.append(f'<article><h2>{html.escape(g["product_name"])} <b>×{g["total"]}</b></h2><div class="barcode">{html.escape(g["barcode"])}</div><p>{html.escape("; ".join(g["locations"]) or "UNKNOWN LOCATION")}</p><p>{html.escape("; ".join(g["orders_display"]))}</p><div class="controls">{checks}</div></article>')
        else:
            rows.append(f'<tr><td>{checks}</td><td>{img}</td><td><b>{html.escape(g["product_name"])}</b><br>ID {g["product_id"] or "UNMATCHED"}<br>{html.escape(", ".join(g["identifiers"]))}</td><td class="qty">{g["total"]}<small>W {g["walmart"]} / A {g["amazon"]}</small></td><td class="barcode">{html.escape(g["barcode"])}</td><td>{html.escape("; ".join(g["orders_display"]))}</td><td>{html.escape(", ".join(g["ship_dates"]) or "Not stored")}</td><td>{html.escape("; ".join(g["locations"]) or "UNKNOWN")}</td><td>{html.escape("; ".join(g["notes"]))}</td></tr>')
    style='''body{font:18px Arial;margin:16px;color:#132}h1{font-size:30px}.summary{border:3px solid #123b5d;padding:12px}.barcode{font:900 24px Consolas;letter-spacing:2px}.qty{font-size:34px;font-weight:900}.qty small{display:block;font-size:14px}table{width:100%;border-collapse:collapse}th,td{border:2px solid #333;padding:9px;vertical-align:top}th{background:#123b5d;color:white}img{width:80px;height:80px;object-fit:contain}label{display:block;font-weight:800;margin:5px 0}@media print{body{font-size:15px}tr{break-inside:avoid}}'''
    if mobile: style+='article{border:3px solid #123b5d;border-radius:12px;padding:12px;margin:12px 0}article h2{font-size:24px;margin:0 0 8px}.controls{display:grid;grid-template-columns:1fr 1fr;gap:8px}.controls label{background:#e7f2fa;border:2px solid #087ac1;border-radius:9px;padding:14px;text-align:center;font-size:20px}'
    summary_html=f"<div class=\"summary\"><b>REFRESH {summary['refresh_finished_at']}</b><br>{summary['active_orders']} orders · {summary['active_order_lines']} lines · {summary['units_required']} units · {summary['unique_aggregated_products']} products · Reconciliation {summary['reconciliation']}</div>"
    content=''.join(rows) if mobile else '<table><thead><tr><th>Checklist</th><th>Image</th><th>Product</th><th>Total</th><th>Barcode</th><th>Orders</th><th>Ship by</th><th>Known locations</th><th>Warnings</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'
    return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Storage Yard Fulfillment Pull List</title><style>{style}</style></head><body><h1>Storage Yard Fulfillment Pull List</h1>{summary_html}{content}</body></html>'


if __name__ == "__main__":
    raise SystemExit(main())
