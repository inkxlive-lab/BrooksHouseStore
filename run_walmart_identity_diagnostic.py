"""Read-only Walmart identity diagnostics; emits no customer or shipping data."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


IDENTITY_KEYS = {
    "sku", "upc", "gtin", "itemid", "item_id", "productid", "product_id",
    "lineNumber", "line_number", "productName", "itemName",
}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _identity_fields(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in IDENTITY_KEYS and child not in (None, "", [], {}):
                result[key] = child
            elif isinstance(child, (dict, list)):
                nested = _identity_fields(child)
                if nested not in ({}, []):
                    result[key] = nested
        return result
    if isinstance(value, list):
        return [item for child in value if (item := _identity_fields(child)) not in ({}, [])]
    return {}


def diagnose(database: str | Path, skus: list[str]) -> dict:
    path = Path(database).resolve()
    before = _hash(path)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        wanted = {sku.casefold() for sku in skus}
        line_columns = [row[1] for row in connection.execute("PRAGMA table_info(walmart_order_lines)")]
        listing_columns = [row[1] for row in connection.execute("PRAGMA table_info(walmart_listings)")]
        link_columns = [row[1] for row in connection.execute("PRAGMA table_info(walmart_product_links)")]
        lines = []
        rows = connection.execute(
            """SELECT wol.*, wo.raw_json
                 FROM walmart_order_lines wol
                 JOIN walmart_orders wo ON wo.purchase_order_id=wol.purchase_order_id
                ORDER BY wol.purchase_order_id,wol.order_line_id"""
        ).fetchall()
        for row in rows:
            if str(row["sku"] or "").strip().casefold() not in wanted:
                continue
            raw = json.loads(row["raw_json"] or "{}")
            lines.append({
                "normalized": {key: row[key] for key in line_columns},
                "retained_raw_identity": _identity_fields(raw),
            })
        placeholders = ",".join("?" for _ in wanted)
        listings = [dict(row) for row in connection.execute(
            f"SELECT * FROM walmart_listings WHERE lower(trim(seller_sku)) IN ({placeholders}) ORDER BY walmart_listing_id",
            tuple(sorted(wanted)),
        )]
        links = [dict(row) for row in connection.execute(
            f"""SELECT wpl.* FROM walmart_product_links wpl
                 JOIN walmart_listings wl USING(walmart_listing_id)
                WHERE lower(trim(wl.seller_sku)) IN ({placeholders}) ORDER BY wpl.walmart_product_link_id""",
            tuple(sorted(wanted)),
        )]
        result = {
            "database": str(path),
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "schema": {"walmart_order_lines": line_columns, "walmart_listings": listing_columns,
                       "walmart_product_links": link_columns},
            "lines": lines,
            "listings": listings,
            "links": links,
        }
    finally:
        connection.close()
    after = _hash(path)
    result.update({"sha256_before": before, "sha256_after": after, "zero_mutation": before == after})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--sku", action="append", required=True)
    args = parser.parse_args()
    print(json.dumps(diagnose(args.database, args.sku), indent=2, default=str))


if __name__ == "__main__":
    main()
