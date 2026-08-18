"""Resumable Walmart global-catalog checker for BrooksHouse Store.

Reads distinct barcodes from master_catalog, queries Walmart's published
catalog one identifier at a time, and checkpoints every result in SQLite.
This script performs read-only Walmart API calls; it never creates or updates
Walmart offers, prices, or inventory.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
SEARCH_URL = "https://marketplace.walmartapis.com/v3/items/walmart/search"
SERVICE_NAME = "Walmart Marketplace"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def load_env(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"Environment file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def digits_only(value: Any) -> str:
    if value is None:
        return ""
    return "".join(character for character in str(value).strip() if character.isdigit())


def barcode_lookup(value: Any) -> str:
    digits = digits_only(value)
    return digits.lstrip("0") or ("0" if digits else "")


def valid_gtin(value: str) -> bool:
    """Validate the final GTIN/UPC check digit for 8, 12, 13, or 14 digits."""
    if not value or not value.isdigit() or len(value) not in {8, 12, 13, 14}:
        return False
    digits = [int(character) for character in value]
    body = digits[:-1]
    check_digit = digits[-1]

    total = 0
    # Weight from the rightmost BODY digit: 3, 1, 3, 1...
    for offset, digit in enumerate(reversed(body), start=1):
        total += digit * (3 if offset % 2 == 1 else 1)

    expected = (10 - (total % 10)) % 10
    return check_digit == expected


def walmart_identifier(exact: str) -> tuple[str, str] | None:
    """Return a validated Walmart identifier.

    This catalog contains many UPC values whose leading zeros were stripped
    by spreadsheet/import handling. For 9-11 digit values, restore leading
    zeros to UPC-12 and use them only when the UPC check digit validates.
    """
    if not exact or not exact.isdigit():
        return None

    if 9 <= len(exact) <= 12:
        upc = exact.zfill(12)
        if valid_gtin(upc):
            return "upc", upc
        return None

    if len(exact) in {13, 14}:
        if valid_gtin(exact):
            return "gtin", exact
        return None

    # GTIN-8 is valid, but this Walmart workflow is focused on UPC/EAN/GTIN-14
    # product identifiers from the store/master catalog. Keep shorter values
    # out of live lookups until explicitly repaired.
    return None


def create_results_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS walmart_catalog_matches (
            match_id INTEGER PRIMARY KEY,
            barcode_lookup VARCHAR(50) NOT NULL UNIQUE,
            barcode_exact VARCHAR(50),
            query_type VARCHAR(10),
            query_value VARCHAR(50),
            match_status VARCHAR(30) NOT NULL,
            walmart_item_id VARCHAR(100),
            title VARCHAR(500),
            brand VARCHAR(200),
            product_type VARCHAR(200),
            price_amount NUMERIC(12, 2),
            price_currency VARCHAR(10),
            image_url TEXT,
            standard_upc VARCHAR(50),
            is_marketplace_item BOOLEAN,
            source_data TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_http_status INTEGER,
            error_message TEXT,
            checked_at DATETIME,
            next_retry_at DATETIME,
            in_brookshouse BOOLEAN NOT NULL DEFAULT 0,
            in_master_catalog BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """
    )
    existing_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(walmart_catalog_matches)"
        ).fetchall()
    }
    if "in_brookshouse" not in existing_columns:
        connection.execute(
            "ALTER TABLE walmart_catalog_matches "
            "ADD COLUMN in_brookshouse BOOLEAN NOT NULL DEFAULT 0"
        )
    if "in_master_catalog" not in existing_columns:
        connection.execute(
            "ALTER TABLE walmart_catalog_matches "
            "ADD COLUMN in_master_catalog BOOLEAN NOT NULL DEFAULT 0"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_walmart_catalog_match_status
        ON walmart_catalog_matches (match_status)
        """
    )
    connection.commit()


class WalmartClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self.access_token: str | None = None
        self.expires_at = utc_now()

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "WM_MARKET": "us",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
            "WM_SVC.NAME": SERVICE_NAME,
        }

    def authenticate(self) -> None:
        encoded = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("utf-8")
        response = self.session.post(
            TOKEN_URL,
            headers={
                **self._base_headers(),
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        lifetime = int(payload.get("expires_in") or 900)
        self.expires_at = utc_now() + timedelta(seconds=max(60, lifetime - 60))

    def ensure_token(self) -> None:
        if not self.access_token or utc_now() >= self.expires_at:
            self.authenticate()

    def search(self, query_type: str, query_value: str) -> requests.Response:
        self.ensure_token()
        response = self.session.get(
            SEARCH_URL,
            params={query_type: query_value, "responseFormat": "DEFAULT"},
            headers={
                **self._base_headers(),
                "WM_SEC.ACCESS_TOKEN": str(self.access_token),
            },
            timeout=30,
        )
        if response.status_code == 401:
            self.access_token = None
            self.ensure_token()
            response = self.session.get(
                SEARCH_URL,
                params={query_type: query_value, "responseFormat": "DEFAULT"},
                headers={
                    **self._base_headers(),
                    "WM_SEC.ACCESS_TOKEN": str(self.access_token),
                },
                timeout=30,
            )
        return response


def collect_source_barcodes(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Combine BrooksHouse and master barcodes without losing source identity."""
    combined: dict[str, dict[str, Any]] = {}

    def add(value: Any, source: str) -> None:
        exact = digits_only(value)
        lookup = barcode_lookup(exact)
        if not lookup:
            return
        record = combined.setdefault(
            lookup,
            {
                "barcode_lookup": lookup,
                "barcode_exact": exact,
                "in_brookshouse": 0,
                "in_master_catalog": 0,
            },
        )
        # Prefer a standards-ready identifier when duplicate representations
        # of the same barcode exist in the two local sources.
        current = record["barcode_exact"]
        if len(exact) in {12, 13, 14} and len(current) not in {12, 13, 14}:
            record["barcode_exact"] = exact
        if source == "brookshouse":
            record["in_brookshouse"] = 1
        else:
            record["in_master_catalog"] = 1

    for row in connection.execute(
        "SELECT barcode FROM product_barcodes WHERE barcode IS NOT NULL"
    ):
        add(row[0], "brookshouse")

    for row in connection.execute(
        """
        SELECT COALESCE(
            NULLIF(TRIM(CAST(barcode_exact AS TEXT)), ''),
            NULLIF(TRIM(CAST(barcode_raw AS TEXT)), '')
        )
        FROM master_catalog
        WHERE barcode_exact IS NOT NULL OR barcode_raw IS NOT NULL
        """
    ):
        add(row[0], "master")

    return combined


def choose_source_barcodes(
    connection: sqlite3.Connection,
    batch_size: int,
    retry_errors: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    combined = collect_source_barcodes(connection)
    existing = {
        row["barcode_lookup"]: {
            "match_status": row["match_status"],
            "query_type": row["query_type"],
            "barcode_exact": digits_only(row["barcode_exact"]),
        }
        for row in connection.execute(
            "SELECT barcode_lookup, barcode_exact, query_type, match_status "
            "FROM walmart_catalog_matches"
        )
    }

    # Keep source flags current even when a barcode was checked previously.
    connection.executemany(
        """
        UPDATE walmart_catalog_matches
        SET in_brookshouse = ?, in_master_catalog = ?, updated_at = ?
        WHERE barcode_lookup = ?
        """,
        (
            (
                record["in_brookshouse"],
                record["in_master_catalog"],
                iso_now(),
                lookup,
            )
            for lookup, record in combined.items()
        ),
    )
    connection.commit()

    eligible_retry = {"ERROR", "RATE_LIMITED"} if retry_errors else set()

    def needs_legacy_upc_recheck(lookup: str) -> bool:
        prior = existing.get(lookup)
        if not prior:
            return False
        exact = prior["barcode_exact"]
        return (
            prior["match_status"] == "NOT_FOUND"
            and prior["query_type"] == "gtin"
            and 9 <= len(exact) <= 11
        )

    work = [
        record
        for lookup, record in combined.items()
        if lookup not in existing
        or existing[lookup]["match_status"] == "PENDING"
        or existing[lookup]["match_status"] in eligible_retry
        or needs_legacy_upc_recheck(lookup)
    ]
    work.sort(key=lambda record: (not record["in_brookshouse"], record["barcode_lookup"]))
    return work[:batch_size], combined


def upsert_result(
    connection: sqlite3.Connection,
    *,
    lookup: str,
    exact: str,
    query_type: str | None,
    query_value: str | None,
    status: str,
    http_status: int | None,
    item: dict[str, Any] | None = None,
    complete_payload: dict[str, Any] | None = None,
    error_message: str | None = None,
    next_retry_at: str | None = None,
    in_brookshouse: int = 0,
    in_master_catalog: int = 0,
) -> None:
    item = item or {}
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    images = item.get("images") if isinstance(item.get("images"), list) else []
    image_url = None
    if images and isinstance(images[0], dict):
        image_url = images[0].get("url")
    standard_upcs = item.get("standardUpc")
    standard_upc = standard_upcs[0] if isinstance(standard_upcs, list) and standard_upcs else None
    now = iso_now()
    connection.execute(
        """
        INSERT INTO walmart_catalog_matches (
            barcode_lookup, barcode_exact, query_type, query_value,
            match_status, walmart_item_id, title, brand, product_type,
            price_amount, price_currency, image_url, standard_upc,
            is_marketplace_item, source_data, attempts, last_http_status,
            error_message, checked_at, next_retry_at,
            in_brookshouse, in_master_catalog, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (barcode_lookup) DO UPDATE SET
            barcode_exact = excluded.barcode_exact,
            query_type = excluded.query_type,
            query_value = excluded.query_value,
            match_status = excluded.match_status,
            walmart_item_id = excluded.walmart_item_id,
            title = excluded.title,
            brand = excluded.brand,
            product_type = excluded.product_type,
            price_amount = excluded.price_amount,
            price_currency = excluded.price_currency,
            image_url = excluded.image_url,
            standard_upc = excluded.standard_upc,
            is_marketplace_item = excluded.is_marketplace_item,
            source_data = excluded.source_data,
            attempts = walmart_catalog_matches.attempts + 1,
            last_http_status = excluded.last_http_status,
            error_message = excluded.error_message,
            checked_at = excluded.checked_at,
            next_retry_at = excluded.next_retry_at,
            in_brookshouse = excluded.in_brookshouse,
            in_master_catalog = excluded.in_master_catalog,
            updated_at = excluded.updated_at
        """,
        (
            lookup,
            exact,
            query_type,
            query_value,
            status,
            str(item.get("itemId")) if item.get("itemId") is not None else None,
            item.get("title"),
            item.get("brand"),
            item.get("productType"),
            price.get("amount"),
            price.get("currency"),
            image_url,
            standard_upc,
            item.get("isMarketPlaceItem"),
            json.dumps(complete_payload, separators=(",", ":")) if complete_payload is not None else None,
            http_status,
            error_message,
            now,
            next_retry_at,
            in_brookshouse,
            in_master_catalog,
            now,
            now,
        ),
    )
    connection.commit()


def print_status(
    connection: sqlite3.Connection,
    combined: dict[str, dict[str, Any]] | None = None,
) -> None:
    combined = combined or collect_source_barcodes(connection)
    combined_total = len(combined)
    brookshouse_total = sum(record["in_brookshouse"] for record in combined.values())
    master_total = sum(record["in_master_catalog"] for record in combined.values())
    rows = connection.execute(
        """
        SELECT match_status, COUNT(*) AS count
        FROM walmart_catalog_matches
        GROUP BY match_status
        ORDER BY match_status
        """
    ).fetchall()
    checked = sum(row["count"] for row in rows if row["match_status"] in {"MATCH", "NOT_FOUND"})
    print(f"Distinct combined barcodes: {combined_total:,}")
    print(f"  BrooksHouse product barcodes: {brookshouse_total:,}")
    print(f"  Master Catalog barcodes: {master_total:,}")
    print(f"Completed Walmart checks: {checked:,}")
    print(f"Remaining: {max(0, combined_total - checked):,}")
    for row in rows:
        print(f"  {row['match_status']}: {row['count']:,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="app/data/brookshouse_store.db")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--delay", type=float, default=0.40)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.batch_size < 1 or args.delay < 0:
        raise RuntimeError("batch-size must be positive and delay cannot be negative")

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    create_results_table(connection)

    if args.status:
        print_status(connection)
        connection.close()
        return 0

    load_env(Path(args.env))
    client_id = os.getenv("WALMART_CLIENT_ID")
    client_secret = os.getenv("WALMART_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are required")

    work, combined = choose_source_barcodes(
        connection, args.batch_size, args.retry_errors
    )
    if not work:
        print("No eligible pending barcodes in this batch.")
        print_status(connection, combined)
        connection.close()
        return 0

    client = WalmartClient(client_id, client_secret)
    matched = not_found = errors = invalid = 0
    consecutive_rate_limits = 0
    print(f"Starting resumable Walmart catalog check for {len(work):,} barcodes...")
    print("Press Ctrl+C at any time; completed results are already saved.\n")

    try:
        for position, row in enumerate(work, start=1):
            exact = digits_only(row["barcode_exact"])
            lookup = barcode_lookup(row["barcode_lookup"] or exact)
            source_fields = {
                "in_brookshouse": row["in_brookshouse"],
                "in_master_catalog": row["in_master_catalog"],
            }
            identifier = walmart_identifier(exact)
            if not identifier:
                repairable_length = 9 <= len(exact) <= 14
                status = "NEEDS_BARCODE_REPAIR" if repairable_length else "INVALID_BARCODE"
                message = (
                    "Barcode length is plausible but UPC/GTIN check digit is invalid "
                    "after restoring leading zeros where applicable"
                    if repairable_length
                    else "Barcode cannot be converted to a supported validated UPC/GTIN"
                )
                upsert_result(
                    connection,
                    lookup=lookup,
                    exact=exact,
                    query_type=None,
                    query_value=None,
                    status=status,
                    http_status=None,
                    error_message=message,
                    **source_fields,
                )
                invalid += 1
                continue

            query_type, query_value = identifier
            try:
                response = client.search(query_type, query_value)
            except requests.RequestException as exc:
                retry_at = (utc_now() + timedelta(minutes=5)).isoformat()
                upsert_result(
                    connection,
                    lookup=lookup,
                    exact=exact,
                    query_type=query_type,
                    query_value=query_value,
                    status="ERROR",
                    http_status=None,
                    error_message=str(exc)[:1000],
                    next_retry_at=retry_at,
                    **source_fields,
                )
                errors += 1
                time.sleep(max(args.delay, 1.0))
                continue

            if response.status_code == 429:
                consecutive_rate_limits += 1
                retry_seconds = min(300, max(10, int(response.headers.get("Retry-After", "30") or 30)))
                upsert_result(
                    connection,
                    lookup=lookup,
                    exact=exact,
                    query_type=query_type,
                    query_value=query_value,
                    status="RATE_LIMITED",
                    http_status=429,
                    error_message="Walmart API rate limit reached",
                    next_retry_at=(utc_now() + timedelta(seconds=retry_seconds)).isoformat(),
                    **source_fields,
                )
                print(f"Rate limited. Waiting {retry_seconds} seconds...")
                time.sleep(retry_seconds)
                if consecutive_rate_limits >= 3:
                    print("Stopped safely after three consecutive rate limits. Rerun later with --retry-errors.")
                    break
                continue
                    
            consecutive_rate_limits = 0
            if response.status_code != 200:
                errors += 1
                upsert_result(
                    connection,
                    lookup=lookup,
                    exact=exact,
                    query_type=query_type,
                    query_value=query_value,
                    status="ERROR",
                    http_status=response.status_code,
                    error_message=response.text[:1000],
                    next_retry_at=(utc_now() + timedelta(minutes=10)).isoformat(),
                    **source_fields,
                )
                time.sleep(args.delay)
                continue

            payload = response.json()

            items = payload.get("items") if isinstance(payload, dict) else None

            if isinstance(items, list) and items:
                upsert_result(
                    connection,
                    lookup=lookup,
                    exact=exact,
                    query_type=query_type,
                    query_value=query_value,
                    status="MATCH",
                    http_status=200,
                    item=items[0],
                    complete_payload=payload,
                    **source_fields,
                )
                matched += 1
            else:
                upsert_result(
                    connection,
                    lookup=lookup,
                    exact=exact,
                    query_type=query_type,
                    query_value=query_value,
                    status="NOT_FOUND",
                    http_status=200,
                    complete_payload=payload if isinstance(payload, dict) else {},
                    **source_fields,
                )
                not_found += 1
            if position % 25 == 0 or position == len(work):
                print(
                    f"{position:,}/{len(work):,} | matches {matched:,} | "
                    f"not found {not_found:,} | errors {errors:,} | invalid {invalid:,}"
                )

            time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\nStopped safely. Every completed lookup has been saved.")

    finally:
        print("\nBatch summary")
        print(f"  Matches: {matched:,}")
        print(f"  Not found: {not_found:,}")
        print(f"  Errors: {errors:,}")
        print(f"  Invalid / needs repair: {invalid:,}")
        print_status(connection, combined)
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())