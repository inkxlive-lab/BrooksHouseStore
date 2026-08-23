"""Run Channel Inventory Review candidate searches against SQLite read-only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import traceback
from pathlib import Path
from urllib.parse import urlparse

from app.config import DATABASE_URL
from app.services.channel_inventory_review_workflow import search_products


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--query", action="append", required=True)
    args = parser.parse_args()
    database = Path(args.database).resolve()
    configured = urlparse(DATABASE_URL)
    configured_database = configured.path if configured.scheme == "sqlite" else f"{configured.scheme or 'unknown'} database"
    before = _sha256(database)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        barcode_occurrences = []
        product_id_occurrences = []
        for table_row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            table = str(table_row[0])
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            if "product_id" in columns:
                for match in connection.execute(
                        f'''SELECT rowid,* FROM "{table}" WHERE CAST(product_id AS TEXT)='1929' LIMIT 20'''):
                    product_id_occurrences.append({"table":table,"row":dict(match)})
            for column in columns:
                if "barcode" not in column.casefold() and column.casefold() not in {"upc", "gtin"}:
                    continue
                matches = connection.execute(
                    f'''SELECT rowid,* FROM "{table}" WHERE ltrim(CAST("{column}" AS TEXT),'0')=ltrim(?,'0') LIMIT 20''',
                    ("076753075572",)).fetchall()
                for match in matches:
                    barcode_occurrences.append({"table":table,"column":column,"row":dict(match)})
        direct = {
            "product_1929": [dict(row) for row in connection.execute(
                "SELECT * FROM products WHERE product_id=1929")],
            "barcode_076753075572": [dict(row) for row in connection.execute(
                "SELECT * FROM product_barcodes WHERE CAST(barcode AS TEXT)='076753075572'")],
            "normalized_barcode_076753075572": [dict(row) for row in connection.execute(
                """SELECT pb.*,p.product_name,p.active FROM product_barcodes pb JOIN products p USING(product_id)
                    WHERE ltrim(CAST(pb.barcode AS TEXT),'0')=ltrim('076753075572','0')""")],
            "inventory_product_1929": [dict(row) for row in connection.execute(
                """SELECT i.inventory_id,i.product_id,i.location_id,l.location_name,i.container_id,
                          i.quantity_on_hand,i.quantity_reserved
                     FROM inventory i JOIN inventory_locations l USING(location_id)
                    WHERE i.product_id=1929""")],
            "inventory_row_1929": [dict(row) for row in connection.execute(
                """SELECT i.inventory_id,i.product_id,p.product_name,i.location_id,l.location_name,
                          i.container_id,i.quantity_on_hand,i.quantity_reserved
                     FROM inventory i JOIN products p USING(product_id)
                     JOIN inventory_locations l USING(location_id) WHERE i.inventory_id=1929""")],
            "walmart_bp_lines": [dict(row) for row in connection.execute(
                "SELECT * FROM walmart_order_lines WHERE TRIM(sku)='bp' COLLATE NOCASE")],
            "walmart_bp_listings": [dict(row) for row in connection.execute(
                "SELECT * FROM walmart_listings WHERE TRIM(seller_sku)='bp' COLLATE NOCASE")],
            "channel_listing_bp": [dict(row) for row in connection.execute(
                """SELECT cl.listing_id,cl.barcode_lookup,cl.listing_title,cl.sku,
                          cl.listing_status,cl.listed_price,cl.quantity_available,cl.external_product_id
                     FROM channel_listings cl JOIN sales_channels sc USING(channel_id)
                    WHERE lower(sc.channel_name)='walmart' AND TRIM(cl.sku)='bp' COLLATE NOCASE""")],
            "store_back_room_on_the_table_18": [dict(row) for row in connection.execute(
                """SELECT i.inventory_id,i.product_id,p.product_name,p.active,p.brand,p.description,
                          i.container_id,i.quantity_on_hand,pb.barcode
                     FROM inventory i JOIN products p USING(product_id)
                     JOIN inventory_locations l USING(location_id)
                     LEFT JOIN product_barcodes pb ON pb.product_id=p.product_id
                    WHERE l.location_name='Store Back Room' AND i.container_id='ON-THE-TABLE'
                      AND i.quantity_on_hand=18""")],
            "barcode_occurrences": barcode_occurrences,
            "product_id_occurrences": product_id_occurrences,
        }
    finally:
        connection.close()
    results = []
    for query in args.query:
        try:
            results.append({"query": query, "rows": search_products(database, query)})
        except Exception as exc:
            results.append({"query": query, "exception_type": type(exc).__name__,
                            "exception": str(exc), "traceback": traceback.format_exc()})
    after = _sha256(database)
    print(json.dumps({"configured_database": configured_database, "diagnostic_database": str(database),
                      "direct": direct, "results": results, "sha256_before": before, "sha256_after": after,
                      "zero_mutation": before == after}, indent=2, default=str))


if __name__ == "__main__":
    main()
