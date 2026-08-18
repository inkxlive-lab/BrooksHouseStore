#!/usr/bin/env python3
"""Safely match Amazon listings to BrooksHouse products through Catalog UPC/EAN/GTIN data."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

from amazon_order_history_sync import AmazonClient, first_env, load_env


CATALOG_URL = "https://sellingpartnerapi-na.amazon.com/catalog/2022-04-01/items/{asin}"


def normalize_barcode(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return (digits.lstrip("0") or "0") if digits else ""


def catalog_item(client: AmazonClient, asin: str, marketplace: str) -> dict:
    for attempt in range(1, 6):
        client.ensure_token()
        response = requests.get(
            CATALOG_URL.format(asin=asin),
            params={"marketplaceIds": marketplace, "includedData": "identifiers,summaries"},
            headers={"Accept": "application/json", "x-amz-access-token": str(client.access_token)},
            timeout=60,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code in (429, 500, 502, 503, 504):
            wait = min(30, 2 ** attempt)
            print(f"  Amazon returned {response.status_code}; retrying in {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Amazon Catalog error for {asin}: {response.status_code} {response.text[:1000]}")
    raise RuntimeError(f"Amazon Catalog retries exhausted for {asin}")


def extract_identifiers(payload: dict) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for group in payload.get("identifiers") or []:
        for item in group.get("identifiers") or []:
            kind = str(item.get("identifierType") or "").upper().strip()
            value = str(item.get("identifier") or "").strip()
            if kind in {"UPC", "EAN", "GTIN"} and value:
                found.add((kind, value))
    return sorted(found)


def product_candidates(conn: sqlite3.Connection, identifiers: list[tuple[str, str]]) -> list[sqlite3.Row]:
    lookups = sorted({normalize_barcode(value) for _, value in identifiers if normalize_barcode(value)})
    if not lookups:
        return []
    placeholders = ",".join("?" for _ in lookups)
    return conn.execute(
        f"""
        SELECT DISTINCT pb.product_id, p.product_name, pb.barcode
        FROM product_barcodes pb
        JOIN products p ON p.product_id = pb.product_id
        WHERE LTRIM(pb.barcode, '0') IN ({placeholders})
        ORDER BY pb.product_id, pb.barcode
        """,
        lookups,
    ).fetchall()


def create_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS amazon_catalog_match_audit (
            amazon_listing_id INTEGER PRIMARY KEY,
            asin TEXT NOT NULL,
            seller_sku TEXT,
            identifiers_json TEXT NOT NULL,
            candidate_product_ids TEXT,
            result_status TEXT NOT NULL,
            matched_product_id INTEGER,
            checked_at TEXT NOT NULL
        )
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write unique links to amazon_product_links")
    parser.add_argument("--limit", type=int, default=0, help="Optional number of listings to test")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    load_env(root / ".env")
    marketplace = first_env("AMAZON_MARKETPLACE_ID", "SP_API_MARKETPLACE_ID") or "ATVPDKIKX0DER"
    client = AmazonClient(
        first_env("AMAZON_LWA_CLIENT_ID", "SP_API_CLIENT_ID", "LWA_CLIENT_ID"),
        first_env("AMAZON_LWA_CLIENT_SECRET", "SP_API_CLIENT_SECRET", "LWA_CLIENT_SECRET"),
        first_env("AMAZON_REFRESH_TOKEN", "SP_API_REFRESH_TOKEN", "LWA_REFRESH_TOKEN"),
    )

    db_path = root / "app" / "data" / "brookshouse_store.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    create_audit_table(conn)

    sql = """
        SELECT al.amazon_listing_id, al.seller_sku, al.asin,
               apl.product_id AS existing_product_id,
               COALESCE(apl.match_status, 'unmatched') AS existing_status
        FROM amazon_listings al
        LEFT JOIN amazon_product_links apl ON apl.amazon_listing_id = al.amazon_listing_id
        ORDER BY al.amazon_listing_id
    """
    listings = conn.execute(sql).fetchall()
    if args.limit > 0:
        listings = listings[: args.limit]

    totals = {"unique": 0, "unmatched": 0, "ambiguous": 0, "preserved": 0, "errors": 0}
    unique_links: list[tuple[int, int, str, str]] = []
    checked_at = datetime.now().isoformat(timespec="seconds")

    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Listings: {len(listings)}")
    print(f"Marketplace: {marketplace}\n")

    for index, listing in enumerate(listings, start=1):
        listing_id = int(listing["amazon_listing_id"])
        asin = str(listing["asin"] or "").strip()
        sku = str(listing["seller_sku"] or "").strip()
        existing_status = str(listing["existing_status"] or "").lower()
        existing_product_id = listing["existing_product_id"]

        if existing_product_id is not None and existing_status in {"linked", "matched", "manual"}:
            totals["preserved"] += 1
            print(f"[{index}/{len(listings)}] {asin} {sku}: preserved existing link -> {existing_product_id}")
            continue

        try:
            payload = catalog_item(client, asin, marketplace)
            identifiers = extract_identifiers(payload)
            candidates = product_candidates(conn, identifiers)
            product_ids = sorted({int(row["product_id"]) for row in candidates})

            if len(product_ids) == 1:
                result = "unique"
                matched_id = product_ids[0]
                matched_values = sorted({row["barcode"] for row in candidates if int(row["product_id"]) == matched_id})
                match_value = ",".join(matched_values)
                totals["unique"] += 1
                unique_links.append((listing_id, matched_id, "amazon_catalog_barcode", match_value))
                print(f"[{index}/{len(listings)}] {asin} {sku}: UNIQUE -> {matched_id} ({candidates[0]['product_name']})")
            elif len(product_ids) > 1:
                result = "ambiguous"
                matched_id = None
                totals["ambiguous"] += 1
                print(f"[{index}/{len(listings)}] {asin} {sku}: AMBIGUOUS -> {product_ids}")
            else:
                result = "unmatched"
                matched_id = None
                totals["unmatched"] += 1
                print(f"[{index}/{len(listings)}] {asin} {sku}: no barcode match")

            conn.execute(
                """
                INSERT INTO amazon_catalog_match_audit (
                    amazon_listing_id, asin, seller_sku, identifiers_json,
                    candidate_product_ids, result_status, matched_product_id, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(amazon_listing_id) DO UPDATE SET
                    asin=excluded.asin,
                    seller_sku=excluded.seller_sku,
                    identifiers_json=excluded.identifiers_json,
                    candidate_product_ids=excluded.candidate_product_ids,
                    result_status=excluded.result_status,
                    matched_product_id=excluded.matched_product_id,
                    checked_at=excluded.checked_at
                """,
                (listing_id, asin, sku, json.dumps(identifiers), json.dumps(product_ids), result, matched_id, checked_at),
            )
        except Exception as error:
            totals["errors"] += 1
            print(f"[{index}/{len(listings)}] {asin} {sku}: ERROR {error}")

        conn.commit()
        time.sleep(0.65)

    if args.apply:
        linked_at = datetime.now().isoformat(timespec="seconds")
        for listing_id, product_id, method, value in unique_links:
            conn.execute(
                """
                INSERT INTO amazon_product_links (
                    amazon_listing_id, product_id, match_status, match_method, match_value, linked_at
                ) VALUES (?, ?, 'linked', ?, ?, ?)
                ON CONFLICT(amazon_listing_id) DO UPDATE SET
                    product_id=excluded.product_id,
                    match_status='linked',
                    match_method=excluded.match_method,
                    match_value=excluded.match_value,
                    linked_at=excluded.linked_at
                """,
                (listing_id, product_id, method, value, linked_at),
            )
        conn.commit()

    print("\n## MATCH SUMMARY")
    print(f"Unique safe matches: {totals['unique']}")
    print(f"No barcode match:    {totals['unmatched']}")
    print(f"Ambiguous matches:   {totals['ambiguous']}")
    print(f"Existing preserved:  {totals['preserved']}")
    print(f"Errors:              {totals['errors']}")
    print(f"Links written:       {len(unique_links) if args.apply else 0}")
    if not args.apply:
        print("\nDry run only. Run again with --apply after reviewing these totals.")

    conn.close()
    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
