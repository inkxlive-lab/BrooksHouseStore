"""Backfill missing BrooksHouse product pictures using existing UPC lookup code.

Safe defaults:
- Only products that already have a BrooksHouse barcode.
- Only products with NO ProductImage record.
- Never replaces an existing picture.
- Uses the existing app.integrations.product_lookup.lookup_upc_online function.
- Downloads the first returned internet image into local static storage.
- Keeps a lookup log so "not found" barcodes are not repeatedly retried.
"""

from __future__ import annotations
import json

import argparse
import os
import mimetypes
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, quote, quote_plus
from urllib.request import Request, urlopen

from app.integrations.product_lookup import lookup_upc_online


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "app" / "data" / "brookshouse_store.db"
STATIC_DIR = PROJECT_ROOT / "app" / "static"
IMAGE_DIR = STATIC_DIR / "product-images" / "internet"
PUBLIC_PREFIX = "/static/product-images/internet"

LOOKUP_LOG_TABLE = "internet_image_lookup_log"


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_barcode(value: object) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def ensure_lookup_log(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LOOKUP_LOG_TABLE} (
            barcode TEXT PRIMARY KEY,
            product_id INTEGER,
            status TEXT NOT NULL,
            image_url TEXT,
            local_image_url TEXT,
            lookup_source TEXT,
            error TEXT,
            checked_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def table_columns(conn: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()


def column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row["name"]
        for row in table_columns(conn, table_name)
    }


def required_unknown_columns(
    conn: sqlite3.Connection,
    table_name: str,
    supplied: set[str],
) -> list[str]:
    unknown: list[str] = []
    for row in table_columns(conn, table_name):
        name = row["name"]
        not_null = bool(row["notnull"])
        default_value = row["dflt_value"]
        primary_key = bool(row["pk"])

        if primary_key:
            continue

        if not_null and default_value is None and name not in supplied:
            unknown.append(name)

    return unknown


def first_available(columns: set[str], choices: tuple[str, ...]) -> str | None:
    for choice in choices:
        if choice in columns:
            return choice
    return None


def existing_image_product_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        int(row[0])
        for row in conn.execute(
            "SELECT DISTINCT product_id FROM product_images"
        ).fetchall()
        if row[0] is not None
    }


def recent_lookup_status(
    conn: sqlite3.Connection,
    barcode: str,
    retry_days: int,
) -> sqlite3.Row | None:
    row = conn.execute(
        f"""
        SELECT *
        FROM {LOOKUP_LOG_TABLE}
        WHERE barcode = ?
        """,
        (barcode,),
    ).fetchone()

    if row is None:
        return None

    try:
        checked = datetime.fromisoformat(row["checked_at"])
    except Exception:
        return None

    if checked >= datetime.now() - timedelta(days=retry_days):
        return row

    return None


def get_candidates(
    conn: sqlite3.Connection,
    limit: int,
    retry_days: int,
    retry: bool,
) -> list[sqlite3.Row]:
    image_products = existing_image_product_ids(conn)

    rows = conn.execute(
        """
        SELECT
            p.product_id,
            p.product_name,
            pb.barcode
        FROM products p
        JOIN product_barcodes pb
          ON pb.product_id = p.product_id
        WHERE p.active = 1
          AND pb.is_primary = 1
        ORDER BY p.product_id
        """
    ).fetchall()

    candidates: list[sqlite3.Row] = []
    seen_products: set[int] = set()

    for row in rows:
        product_id = int(row["product_id"])

        if product_id in seen_products:
            continue

        seen_products.add(product_id)

        if product_id in image_products:
            continue

        barcode = clean_barcode(row["barcode"])
        if not barcode:
            continue

        if not retry:
            previous = recent_lookup_status(
                conn,
                barcode,
                retry_days=retry_days,
            )
            if previous is not None:
                continue

        candidates.append(row)

        if len(candidates) >= limit:
            break

    return candidates


def guess_extension(image_url: str, content_type: str | None) -> str:
    parsed = urlparse(image_url)
    suffix = Path(parsed.path).suffix.lower()

    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix

    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed

    return ".jpg"


def download_image(
    image_url: str,
    product_id: int,
    barcode: str,
) -> tuple[str, Path]:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    request = Request(
        image_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 BrooksHouseStore/1.0 "
                "(product image cache)"
            )
        },
    )

    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read(12 * 1024 * 1024 + 1)

    if len(data) > 12 * 1024 * 1024:
        raise RuntimeError("Image is larger than 12 MB.")

    if not data:
        raise RuntimeError("Image download returned no data.")

    extension = guess_extension(image_url, content_type)

    safe_barcode = re.sub(r"[^0-9A-Za-z_-]", "", barcode)
    filename = f"{product_id}-{safe_barcode}{extension}"
    destination = IMAGE_DIR / filename
    destination.write_bytes(data)

    public_url = f"{PUBLIC_PREFIX}/{filename}"
    return public_url, destination


def write_lookup_log(
    conn: sqlite3.Connection,
    *,
    barcode: str,
    product_id: int,
    status: str,
    image_url: str | None = None,
    local_image_url: str | None = None,
    source: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {LOOKUP_LOG_TABLE} (
            barcode,
            product_id,
            status,
            image_url,
            local_image_url,
            lookup_source,
            error,
            checked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            product_id = excluded.product_id,
            status = excluded.status,
            image_url = excluded.image_url,
            local_image_url = excluded.local_image_url,
            lookup_source = excluded.lookup_source,
            error = excluded.error,
            checked_at = excluded.checked_at
        """,
        (
            barcode,
            product_id,
            status,
            image_url,
            local_image_url,
            source,
            error,
            now_text(),
        ),
    )


def add_product_image(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    product_name: str,
    source_image_url: str,
    local_public_url: str,
    barcode: str,
    source_name: str,
) -> None:
    columns = column_names(conn, "product_images")

    values: dict[str, object] = {
        "product_id": product_id,
    }

    # The BrooksHouse UI looks for these URL-style fields.
    url_field = first_available(
        columns,
        ("image_url", "url", "source_url", "external_url"),
    )
    if url_field:
        values[url_field] = local_public_url

    # Keep the original external URL too when a separate field exists.
    original_url_field = first_available(
        columns - ({url_field} if url_field else set()),
        ("source_url", "external_url", "image_url", "url"),
    )
    if original_url_field:
        values[original_url_field] = source_image_url

    path_field = first_available(
        columns,
        ("image_path", "file_path", "local_path", "path"),
    )
    if path_field:
        relative_path = local_public_url.removeprefix("/static/")
        values[path_field] = relative_path

    if "alt_text" in columns:
        values["alt_text"] = product_name

    if "source" in columns:
        values["source"] = source_name

    if "image_source" in columns:
        values["image_source"] = source_name

    if "external_id" in columns:
        values["external_id"] = f"internet:{barcode}"

    if "primary_image" in columns:
        values["primary_image"] = 1

    if "is_primary" in columns:
        values["is_primary"] = 1


    if "image_type" in columns:
        values["image_type"] = "front"
    if "created_at" in columns:
        values["created_at"] = now_text()

    if "updated_at" in columns:
        values["updated_at"] = now_text()

    if url_field is None and path_field is None:
        raise RuntimeError(
            "product_images has no recognized URL/path field. "
            f"Columns: {sorted(columns)}"
        )

    missing_required = required_unknown_columns(
        conn,
        "product_images",
        set(values),
    )
    if missing_required:
        raise RuntimeError(
            "product_images has required field(s) this script does not "
            f"know how to populate: {missing_required}. "
            f"Columns: {sorted(columns)}"
        )

    names = list(values)
    placeholders = ", ".join("?" for _ in names)
    sql = (
        f"INSERT INTO product_images "
        f"({', '.join(names)}) "
        f"VALUES ({placeholders})"
    )

    conn.execute(
        sql,
        tuple(values[name] for name in names),
    )


def upsert_enrichment(
    conn: sqlite3.Connection,
    *,
    barcode: str,
    image_url: str,
    source: str,
) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    if "product_enrichment" not in tables:
        return

    timestamp = now_text()

    conn.execute(
        """
        INSERT INTO product_enrichment (
            barcode,
            image_url,
            lookup_source,
            approved_source,
            last_checked_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            image_url = COALESCE(product_enrichment.image_url, excluded.image_url),
            lookup_source = COALESCE(product_enrichment.lookup_source, excluded.lookup_source),
            approved_source = COALESCE(product_enrichment.approved_source, excluded.approved_source),
            last_checked_at = excluded.last_checked_at,
            updated_at = excluded.updated_at
        """,
        (
            barcode,
            image_url,
            source,
            source,
            timestamp,
            timestamp,
        ),
    )



def lookup_open_facts_image(barcode: str) -> tuple[str | None, str]:
    """
    Fallback barcode lookup across the Open Facts family.

    product_type=all lets one barcode request match food, beauty,
    pet-food, or general Open Products Facts records.
    """
    encoded_barcode = quote(barcode, safe="")
    url = (
        "https://world.openfoodfacts.org/api/v3/product/"
        f"{encoded_barcode}"
        "?product_type=all"
        "&fields=code,product_type,product_name,"
        "image_front_url,image_url,images"
    )

    request = Request(
        url,
        headers={
            "User-Agent": (
                "BrooksHouseStore/1.0 "
                "(local inventory product-image lookup)"
            ),
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(
                response.read().decode("utf-8", errors="replace")
            )
    except Exception:
        return None, "Open Facts"

    if not isinstance(payload, dict):
        return None, "Open Facts"

    product = payload.get("product") or {}

    if not isinstance(product, dict):
        return None, "Open Facts"

    product_type = str(
        product.get("product_type")
        or payload.get("product_type")
        or "product"
    )

    source_name = f"Open Facts:{product_type}"

    for field_name in (
        "image_front_url",
        "image_url",
    ):
        value = product.get(field_name)

        if (
            isinstance(value, str)
            and value.strip().startswith(("http://", "https://"))
        ):
            return value.strip(), source_name

    images = product.get("images")

    if isinstance(images, dict):
        # Prefer a front-facing image if one is present.
        preferred_keys = [
            key
            for key in images
            if str(key).lower().startswith("front")
        ]

        for key in preferred_keys + list(images):
            image_data = images.get(key)

            if isinstance(image_data, str):
                if image_data.startswith(("http://", "https://")):
                    return image_data, source_name

            if isinstance(image_data, dict):
                for candidate_key in (
                    "display",
                    "large",
                    "url",
                    "image_url",
                ):
                    value = image_data.get(candidate_key)

                    if (
                        isinstance(value, str)
                        and value.startswith(("http://", "https://"))
                    ):
                        return value, source_name

    return None, source_name


def get_brave_search_api_key() -> str | None:
    key = os.getenv("BRAVE_SEARCH_API_KEY")

    if key:
        return key.strip()

    env_path = PROJECT_ROOT / ".env"

    if not env_path.exists():
        return None

    try:
        for raw_line in env_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            line = raw_line.strip()

            if (
                not line
                or line.startswith("#")
                or "=" not in line
            ):
                continue

            name, value = line.split("=", 1)

            if name.strip() == "BRAVE_SEARCH_API_KEY":
                return value.strip().strip('"').strip("'") or None

    except Exception:
        return None

    return None


def lookup_brave_image(
    barcode: str,
    product_name: str,
) -> tuple[str | None, str]:
    """
    General image-search fallback.

    Uses the same pattern that worked manually:
        barcode + BrooksHouse product title
    """
    api_key = get_brave_search_api_key()

    if not api_key:
        return None, "Brave Image Search (no API key)"

    query = f'{barcode} "{product_name}"'

    url = (
        "https://api.search.brave.com/res/v1/images/search"
        f"?q={quote_plus(query)}"
        "&country=US"
        "&search_lang=en"
        "&safesearch=strict"
        "&count=10"
    )

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
        },
    )

    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(
                response.read().decode(
                    "utf-8",
                    errors="replace",
                )
            )
    except Exception as exc:
        return None, f"Brave Image Search error: {exc}"

    results = payload.get("results") or []

    if not isinstance(results, list):
        return None, "Brave Image Search"

    for result in results:
        if not isinstance(result, dict):
            continue

        properties = result.get("properties") or {}

        if isinstance(properties, dict):
            original_url = properties.get("url")

            if (
                isinstance(original_url, str)
                and original_url.startswith(
                    ("http://", "https://")
                )
            ):
                return original_url, "Brave Image Search"

        thumbnail = result.get("thumbnail")

        if isinstance(thumbnail, dict):
            for field_name in ("original", "src"):
                value = thumbnail.get(field_name)

                if (
                    isinstance(value, str)
                    and value.startswith(
                        ("http://", "https://")
                    )
                ):
                    return value, "Brave Image Search"

        if isinstance(thumbnail, str):
            if thumbnail.startswith(
                ("http://", "https://")
            ):
                return thumbnail, "Brave Image Search"

    return None, "Brave Image Search"

def lookup_first_image(
    barcode: str,
    product_name: str,
) -> tuple[str | None, str]:
    """
    Lookup order:
      1. UPCitemdb
      2. Open Facts
      3. Brave Image Search using barcode + product title
    """
    result = lookup_upc_online(barcode) or {}

    if isinstance(result, dict):
        source = str(
            result.get("source")
            or result.get("lookup_source")
            or "UPCitemdb"
        )

        images = result.get("images") or []

        if isinstance(images, str):
            images = [images]

        if isinstance(images, (list, tuple)):
            for image in images:
                if (
                    isinstance(image, str)
                    and image.strip().startswith(
                        ("http://", "https://")
                    )
                ):
                    return image.strip(), source

        image_url = (
            result.get("image_url")
            or result.get("image")
            or result.get("thumbnail")
        )

        if (
            isinstance(image_url, str)
            and image_url.startswith(
                ("http://", "https://")
            )
        ):
            return image_url, source

    open_facts_url, open_facts_source = (
        lookup_open_facts_image(barcode)
    )

    if open_facts_url:
        return open_facts_url, open_facts_source

    return lookup_brave_image(
        barcode,
        product_name,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing BrooksHouse product pictures."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum products to inspect this run (default 10).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform internet lookups and save pictures.",
    )
    parser.add_argument(
        "--list-candidates",
        action="store_true",
        help="List candidates only; makes no internet requests.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=11.0,
        help="Seconds between lookups (default 11; safe for UPCitemdb FREE).",
    )
    parser.add_argument(
        "--retry-days",
        type=int,
        default=30,
        help="Do not retry a recently checked barcode for this many days.",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Ignore lookup log and retry previously checked barcodes.",
    )
    args = parser.parse_args()

    limit = max(1, min(args.limit, 500))

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    ensure_lookup_log(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    if "product_images" not in tables:
        raise RuntimeError("product_images table was not found.")

    candidates = get_candidates(
        conn,
        limit=limit,
        retry_days=max(0, args.retry_days),
        retry=args.retry,
    )

    print()
    print("BrooksHouse Internet Product Picture Backfill")
    print("--------------------------------------------")
    print(f"Database: {DB_PATH}")
    print(f"Candidates this run: {len(candidates)}")
    print(
        "ProductImage columns: "
        + ", ".join(sorted(column_names(conn, "product_images")))
    )
    print()

    if not candidates:
        print("No eligible products were found.")
        conn.close()
        return

    if args.list_candidates or not args.apply:
        print("PREVIEW ONLY - no internet requests and no database changes.")
        print()
        for row in candidates:
            print(
                f"{row['product_id']:>5} | "
                f"{row['barcode']:<16} | "
                f"{row['product_name']}"
            )
        print()
        print("To run lookups and save pictures, add --apply.")
        conn.close()
        return

    found = 0
    no_image = 0
    failed = 0

    for index, row in enumerate(candidates, start=1):
        product_id = int(row["product_id"])
        product_name = str(row["product_name"])
        barcode = clean_barcode(row["barcode"])

        print(
            f"[{index}/{len(candidates)}] "
            f"{barcode} | {product_name}"
        )

        try:
            source_image_url, source_name = lookup_first_image(barcode, product_name)

            if not source_image_url:
                no_image += 1
                write_lookup_log(
                    conn,
                    barcode=barcode,
                    product_id=product_id,
                    status="no_image",
                    source=source_name,
                )
                conn.commit()
                print("  No internet picture found.")
            else:
                local_public_url, local_path = download_image(
                    source_image_url,
                    product_id,
                    barcode,
                )

                add_product_image(
                    conn,
                    product_id=product_id,
                    product_name=product_name,
                    source_image_url=source_image_url,
                    local_public_url=local_public_url,
                    barcode=barcode,
                    source_name=source_name,
                )

                upsert_enrichment(
                    conn,
                    barcode=barcode,
                    image_url=local_public_url,
                    source=source_name,
                )

                write_lookup_log(
                    conn,
                    barcode=barcode,
                    product_id=product_id,
                    status="found",
                    image_url=source_image_url,
                    local_image_url=local_public_url,
                    source=source_name,
                )

                conn.commit()
                found += 1
                print(f"  Saved: {local_path}")

        except Exception as exc:
            conn.rollback()
            failed += 1

            try:
                write_lookup_log(
                    conn,
                    barcode=barcode,
                    product_id=product_id,
                    status="error",
                    error=str(exc),
                )
                conn.commit()
            except Exception:
                conn.rollback()

            print(f"  ERROR: {exc}")

        if index < len(candidates):
            time.sleep(max(0.0, args.delay))

    print()
    print("Run complete")
    print("------------")
    print(f"Pictures saved: {found}")
    print(f"No picture found: {no_image}")
    print(f"Errors: {failed}")
    print()
    print(
        "Run the script again later to continue with the next "
        "products missing pictures."
    )

    conn.close()


if __name__ == "__main__":
    main()






