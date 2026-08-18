#!/usr/bin/env python3
"""Refresh Amazon listing titles, descriptions, images, and attributes for linked products."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

from amazon_order_history_sync import AmazonClient, first_env, load_env
from app.channel_performance import _ensure_foundation


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "app" / "data" / "brookshouse_store.db"
CATALOG_URL = "https://sellingpartnerapi-na.amazon.com/catalog/2022-04-01/items/{asin}"


def catalog(client, asin, marketplace):
    for attempt in range(1, 6):
        client.ensure_token()
        response = requests.get(
            CATALOG_URL.format(asin=asin),
            params={"marketplaceIds": marketplace, "includedData": "attributes,identifiers,images,summaries"},
            headers={"Accept": "application/json", "x-amz-access-token": str(client.access_token)},
            timeout=60,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code in {429, 500, 502, 503, 504}:
            time.sleep(min(30, 2 ** attempt))
            continue
        raise RuntimeError(f"{response.status_code}: {response.text[:800]}")
    raise RuntimeError("Amazon retries exhausted")


def attribute_values(attributes, key):
    values = []
    raw = attributes.get(key) or []
    if not isinstance(raw, list):
        raw = [raw]
    for item in raw:
        value = item.get("value") if isinstance(item, dict) else item
        if value not in (None, ""):
            values.append(str(value).strip())
    return values


def extract(payload):
    summaries = payload.get("summaries") or []
    summary = summaries[0] if summaries else {}
    attributes = payload.get("attributes") or {}
    title = summary.get("itemName") or next(iter(attribute_values(attributes, "item_name")), None)
    brand = summary.get("brand") or next(iter(attribute_values(attributes, "brand")), None)
    description_parts = []
    description_parts.extend(attribute_values(attributes, "product_description"))
    bullets = attribute_values(attributes, "bullet_point")
    if bullets:
        description_parts.append("\n".join("• " + value for value in bullets))
    images = []
    for group in payload.get("images") or []:
        for item in group.get("images") or []:
            link = item.get("link")
            if link and link not in images:
                images.append(link)
    return title, "\n\n".join(description_parts).strip(), brand, images


def main():
    load_env(ROOT / ".env")
    marketplace = first_env("AMAZON_MARKETPLACE_ID", "SP_API_MARKETPLACE_ID") or "ATVPDKIKX0DER"
    client = AmazonClient(
        first_env("AMAZON_LWA_CLIENT_ID", "SP_API_CLIENT_ID", "LWA_CLIENT_ID"),
        first_env("AMAZON_LWA_CLIENT_SECRET", "SP_API_CLIENT_SECRET", "LWA_CLIENT_SECRET"),
        first_env("AMAZON_REFRESH_TOKEN", "SP_API_REFRESH_TOKEN", "LWA_REFRESH_TOKEN"),
    )
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    _ensure_foundation(connection)
    listings = connection.execute(
        """SELECT apl.product_id, al.asin, al.seller_sku, al.amazon_price
           FROM amazon_product_links apl JOIN amazon_listings al ON al.amazon_listing_id=apl.amazon_listing_id
           WHERE apl.product_id IS NOT NULL AND lower(COALESCE(apl.match_status,'')) IN ('linked','matched')
           ORDER BY al.amazon_listing_id"""
    ).fetchall()
    print(f"Linked Amazon listings: {len(listings)}")
    saved = errors = 0
    for index, row in enumerate(listings, 1):
        try:
            payload = catalog(client, row["asin"], marketplace)
            title, description, brand, images = extract(payload)
            now = datetime.now().isoformat(timespec="seconds")
            connection.execute(
                """INSERT INTO channel_content_snapshots
                   (product_id,channel_name,external_id,sku,title,description,brand,price,primary_image_url,
                    images_json,attributes_json,source_json,synced_at)
                   VALUES (?,'amazon',?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(product_id,channel_name,external_id) DO UPDATE SET
                   sku=excluded.sku,title=excluded.title,description=excluded.description,brand=excluded.brand,
                   price=excluded.price,primary_image_url=excluded.primary_image_url,images_json=excluded.images_json,
                   attributes_json=excluded.attributes_json,source_json=excluded.source_json,synced_at=excluded.synced_at""",
                (row["product_id"], row["asin"], row["seller_sku"], title, description, brand,
                 row["amazon_price"], images[0] if images else None, json.dumps(images),
                 json.dumps(payload.get("attributes") or {}), json.dumps(payload), now),
            )
            connection.commit(); saved += 1
            print(f"[{index}/{len(listings)}] {row['asin']}: saved {title or 'untitled'}")
        except Exception as error:
            errors += 1
            print(f"[{index}/{len(listings)}] {row['asin']}: ERROR {error}")
        time.sleep(0.65)
    connection.close()
    print(f"\nContent snapshots saved: {saved}\nErrors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
