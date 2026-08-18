from app.integrations.product_lookup import lookup_upc_online
from datetime import datetime
import re
import json
import traceback
from contextlib import asynccontextmanager
import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Optional
from uuid import uuid4
from urllib.parse import quote_plus
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from app.services.shopify_approval import (
    build_approval_candidates,
    clear_approvals,
    save_selected_approvals,
)

from app.services.shopify_storefront_preview import (
    build_storefront_import_preview,
)

from app.services.shopify_push_preview import (
    build_shopify_push_preview,
    load_shopify_push_settings,
    save_shopify_push_settings,
)
from app.services.shopify_inventory_push import (
    push_approved_shopify_inventory,
)

from app.database.sales_channels import (
    ChannelListing,
    SalesChannel,
)

from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.connection import (
    Base,
    SessionLocal,
    engine,
    get_database,
)
from app.database.master_catalog import MasterCatalog

from app.database.models import (
    Inventory,
    InventoryLocation,
    InventoryTransaction,
    PriceHistory,
    Product,
    ProductBarcode,
    ProductImage,
)
from app.schemas import ProductCreate

class SmartScanApproval(BaseModel):
    barcode: str
    description: str | None = None
    unit_weight: str | None = None
    source: str | None = None

    brand: str | None = None
    category: str | None = None
    model: str | None = None
    image_url: str | None = None

    internet_price_low: float | None = None
    internet_price_high: float | None = None

APP_DIRECTORY = Path(__file__).resolve().parent
STATIC_DIRECTORY = APP_DIRECTORY / "static"
TEMPLATE_DIRECTORY = APP_DIRECTORY / "templates"
IMAGE_DIRECTORY = STATIC_DIRECTORY / "product-images"

IMAGE_DIRECTORY.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)

    migration_key = "default-stocked-price-099-20260817"
    manifest_path = (
        APP_DIRECTORY
        / "migrations"
        / "default_price_review_20260817.json"
    )

    corrected_dollar_prices = 0

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS brookshouse_system_migrations (
                    migration_key TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

        migration_applied = connection.execute(
            text(
                """
                SELECT migration_key
                FROM brookshouse_system_migrations
                WHERE migration_key = :migration_key
                """
            ),
            {"migration_key": migration_key},
        ).scalar_one_or_none()

        if migration_applied is None:
            product_ids = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )

            if len(product_ids) != 165:
                raise RuntimeError(
                    "Expected 165 products in the .99 migration manifest, "
                    f"but found {len(product_ids)}."
                )

            for offset in range(0, len(product_ids), 100):
                batch = product_ids[offset : offset + 100]

                parameters = {
                    f"product_id_{index}": product_id
                    for index, product_id in enumerate(batch)
                }

                placeholders = ", ".join(
                    f":product_id_{index}"
                    for index in range(len(batch))
                )

                result = connection.execute(
                    text(
                        f"""
                        UPDATE products
                        SET store_price = 0.99
                        WHERE product_id IN ({placeholders})
                          AND store_price = 1.00
                        """
                    ),
                    parameters,
                )

                corrected_dollar_prices += result.rowcount

            connection.execute(
                text(
                    """
                    INSERT INTO brookshouse_system_migrations (
                        migration_key
                    )
                    VALUES (
                        :migration_key
                    )
                    """
                ),
                {"migration_key": migration_key},
            )

        eligible_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM products AS product
                WHERE COALESCE(product.store_price, 0) <= 0
                  AND EXISTS (
                      SELECT 1
                      FROM inventory AS inventory_record
                      WHERE inventory_record.product_id = product.product_id
                        AND COALESCE(
                            inventory_record.quantity_on_hand,
                            0
                        ) > 0
                  )
                """
            )
        ).scalar_one()

        connection.execute(
            text(
                """
                UPDATE products
                SET store_price = 0.99
                WHERE COALESCE(store_price, 0) <= 0
                  AND EXISTS (
                      SELECT 1
                      FROM inventory
                      WHERE inventory.product_id = products.product_id
                        AND COALESCE(
                            inventory.quantity_on_hand,
                            0
                        ) > 0
                  )
                """
            )
        )

        connection.execute(
            text(
                """
                DROP TRIGGER IF EXISTS
                inventory_default_store_price_after_insert
                """
            )
        )

        connection.execute(
            text(
                """
                DROP TRIGGER IF EXISTS
                inventory_default_store_price_after_quantity_update
                """
            )
        )

        connection.execute(
            text(
                """
                DROP TRIGGER IF EXISTS
                stocked_product_default_store_price_after_price_update
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TRIGGER
                inventory_default_store_price_after_insert
                AFTER INSERT ON inventory
                WHEN COALESCE(NEW.quantity_on_hand, 0) > 0
                BEGIN
                    UPDATE products
                    SET store_price = 0.99
                    WHERE product_id = NEW.product_id
                      AND COALESCE(store_price, 0) <= 0;
                END
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TRIGGER
                inventory_default_store_price_after_quantity_update
                AFTER UPDATE OF quantity_on_hand, product_id ON inventory
                WHEN COALESCE(NEW.quantity_on_hand, 0) > 0
                BEGIN
                    UPDATE products
                    SET store_price = 0.99
                    WHERE product_id = NEW.product_id
                      AND COALESCE(store_price, 0) <= 0;
                END
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TRIGGER
                stocked_product_default_store_price_after_price_update
                AFTER UPDATE OF store_price ON products
                WHEN COALESCE(NEW.store_price, 0) <= 0
                  AND EXISTS (
                      SELECT 1
                      FROM inventory
                      WHERE inventory.product_id = NEW.product_id
                        AND COALESCE(
                            inventory.quantity_on_hand,
                            0
                        ) > 0
                  )
                BEGIN
                    UPDATE products
                    SET store_price = 0.99
                    WHERE product_id = NEW.product_id;
                END
                """
            )
        )

    print(
        "BrooksHouse .99 price review migration: "
        f"{corrected_dollar_prices} prior defaults corrected; "
        f"{eligible_count} additional blank stocked prices repaired."
    )

    yield
from app.services.amazon_mapping import (
    get_mapping_page_data,
    link_amazon_listing,
    unlink_amazon_listing,
)

from app.services.dashboard_financials import (
    build_financial_summary,
    build_location_financial_summary,
)

from app.services.product_channel_status import (
    get_product_channel_status,
)
from app.services.system_check import build_system_check
from app.services.cloud_health import install_cloud_health
from app.config import should_run_background_jobs
from app.services.search_helpers import (
    clean_search_term,
    sql_wildcard_pattern,
    wildcard_match,
    wildcard_matches_any,
)

app = FastAPI(
    title="BrooksHouse Store Database",
    description="Product, barcode, image, price, and inventory database",
    version="0.3.0",
    lifespan=lifespan,
)

install_cloud_health(app)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIRECTORY),
    name="static",
)

templates = Jinja2Templates(
    directory=TEMPLATE_DIRECTORY,
)
templates = Jinja2Templates(directory="app/templates")

def optional_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None

    cleaned_value = value.strip()

    if not cleaned_value:
        return None

    try:
        number = Decimal(cleaned_value)

    except InvalidOperation as error:
        raise ValueError(
            f'"{cleaned_value}" is not a valid number.'
        ) from error

    if number < 0:
        raise ValueError("Money and size values cannot be negative.")

    return number


def product_to_dictionary(product: Product) -> dict:
    return {
        "product_id": product.product_id,
        "product_name": product.product_name,
        "brand": product.brand,
        "description": product.description,
        "category": product.category,
        "size_value": (
            float(product.size_value)
            if product.size_value is not None
            else None
        ),
        "size_unit": product.size_unit,
        "pack_quantity": product.pack_quantity,
        "suggested_retail_price": (
            float(product.suggested_retail_price)
            if product.suggested_retail_price is not None
            else None
        ),
        "store_price": (
            float(product.store_price)
            if product.store_price is not None
            else None
        ),
        "average_cost": (
            float(product.average_cost)
            if product.average_cost is not None
            else None
        ),
        "taxable": product.taxable,
        "active": product.active,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
        "barcodes": [
            {
                "barcode_id": barcode.barcode_id,
                "barcode": barcode.barcode,
                "barcode_type": barcode.barcode_type,
                "is_primary": barcode.is_primary,
                "quantity_per_scan": barcode.quantity_per_scan,
            }
            for barcode in product.barcodes
        ],
        "images": [
            {
                "image_id": image.image_id,
                "image_path": image.image_path,
                "image_type": image.image_type,
                "is_primary": image.is_primary,
            }
            for image in product.images
        ],
        "inventory": [
            {
                "inventory_id": record.inventory_id,
                "location_id": record.location_id,
                "quantity_on_hand": record.quantity_on_hand,
                "quantity_reserved": record.quantity_reserved,
                "reorder_level": record.reorder_level,
            }
            for record in product.inventory_records
        ],
    }
DB_PATH = Path("app/data/brookshouse_store.db")


def load_walmart_inventory_flags(
    barcode_values: list[str],
) -> dict[str, dict]:
    """
    Read Walmart seller/global-catalog status for normalized barcodes.

    This is deliberately read-only. The Walmart checker may continue
    writing results while Inventory Search reads the latest committed rows.
    """
    normalized_values = sorted(
        {
            str(value).strip().lstrip("0") or "0"
            for value in barcode_values
            if str(value or "").strip()
        }
    )

    if not normalized_values:
        return {}

    flags = {
        value: {
            "seller": None,
            "catalog": None,
        }
        for value in normalized_values
    }

    connection = None

    try:
        connection = sqlite3.connect(
            DB_PATH,
            timeout=30,
        )
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.row_factory = sqlite3.Row

        table_names = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

        # SQLite commonly limits one statement to 999 parameters.
        # Use smaller chunks so large all-location reports remain safe.
        chunk_size = 400

        if {
            "sales_channels",
            "channel_listings",
        }.issubset(table_names):
            for start in range(
                0,
                len(normalized_values),
                chunk_size,
            ):
                chunk = normalized_values[
                    start:start + chunk_size
                ]
                placeholders = ",".join(
                    "?" for _ in chunk
                )

                seller_rows = connection.execute(
                    f"""
                    SELECT
                        cl.barcode_lookup,
                        cl.listing_title,
                        cl.sku,
                        cl.listing_status,
                        cl.listed_price,
                        cl.quantity_available,
                        cl.external_product_id,
                        cl.source_data
                    FROM channel_listings AS cl
                    JOIN sales_channels AS sc
                      ON sc.channel_id = cl.channel_id
                    WHERE LOWER(sc.channel_name) = 'walmart'
                      AND cl.barcode_lookup IN (
                          {placeholders}
                      )
                    ORDER BY
                        CASE
                            WHEN UPPER(
                                COALESCE(
                                    cl.listing_status,
                                    ''
                                )
                            ) = 'PUBLISHED'
                            THEN 0
                            ELSE 1
                        END,
                        cl.listing_id
                    """,
                    chunk,
                ).fetchall()

                for row in seller_rows:
                    lookup = str(
                        row["barcode_lookup"] or ""
                    ).strip().lstrip("0") or "0"

                    # The ordered first result is the most useful status.
                    if flags.get(lookup, {}).get("seller"):
                        continue

                    source_data = {}
                    try:
                        source_data = json.loads(
                            row["source_data"] or "{}"
                        )
                    except (TypeError, ValueError):
                        source_data = {}

                    flags.setdefault(
                        lookup,
                        {"seller": None, "catalog": None},
                    )["seller"] = {
                        "title": row["listing_title"],
                        "sku": row["sku"],
                        "listing_status": (
                            row["listing_status"]
                            or "UNKNOWN"
                        ),
                        "lifecycle_status": (
                            source_data.get(
                                "lifecycleStatus"
                            )
                            or source_data.get(
                                "_brookshouse_import",
                                {},
                            ).get("lifecycle_status")
                        ),
                        "price": row["listed_price"],
                        "quantity": (
                            row["quantity_available"]
                        ),
                        "item_id": (
                            row["external_product_id"]
                        ),
                    }

        if "walmart_catalog_matches" in table_names:
            for start in range(
                0,
                len(normalized_values),
                chunk_size,
            ):
                chunk = normalized_values[
                    start:start + chunk_size
                ]
                placeholders = ",".join(
                    "?" for _ in chunk
                )

                catalog_rows = connection.execute(
                    f"""
                    SELECT
                        barcode_lookup,
                        match_status,
                        walmart_item_id,
                        title,
                        brand,
                        product_type,
                        price_amount,
                        price_currency,
                        image_url,
                        checked_at,
                        error_message
                    FROM walmart_catalog_matches
                    WHERE barcode_lookup IN (
                        {placeholders}
                    )
                    """,
                    chunk,
                ).fetchall()

                for row in catalog_rows:
                    lookup = str(
                        row["barcode_lookup"] or ""
                    ).strip().lstrip("0") or "0"

                    flags.setdefault(
                        lookup,
                        {"seller": None, "catalog": None},
                    )["catalog"] = {
                        "match_status": (
                            row["match_status"]
                            or "PENDING"
                        ),
                        "item_id": row["walmart_item_id"],
                        "title": row["title"],
                        "brand": row["brand"],
                        "product_type": row["product_type"],
                        "price": row["price_amount"],
                        "currency": row["price_currency"],
                        "image_url": row["image_url"],
                        "checked_at": row["checked_at"],
                        "error_message": row["error_message"],
                    }

        return flags

    except sqlite3.Error as error:
        print(
            "WALMART INVENTORY FLAG READ ERROR:",
            error,
        )
        return flags

    finally:
        if connection is not None:
            connection.close()


def summarize_walmart_inventory_flag(
    product_barcodes,
    walmart_flags: dict[str, dict],
) -> dict:
    """Choose the strongest Walmart result across every product barcode."""
    matches = []

    for barcode_record in product_barcodes:
        exact = str(
            getattr(barcode_record, "barcode", "")
            or ""
        ).strip()

        if not exact:
            continue

        lookup = exact.lstrip("0") or "0"
        flag = walmart_flags.get(lookup) or {}

        if flag.get("seller") or flag.get("catalog"):
            matches.append(
                {
                    "barcode": exact,
                    "lookup": lookup,
                    "seller": flag.get("seller"),
                    "catalog": flag.get("catalog"),
                }
            )

    seller_match = next(
        (
            match
            for match in matches
            if match.get("seller")
        ),
        None,
    )

    if seller_match:
        seller = seller_match["seller"]
        published_status = str(
            seller.get("listing_status")
            or "UNKNOWN"
        ).upper()

        is_published = (
            published_status == "PUBLISHED"
        )

        catalog = seller_match.get("catalog") or {}

        return {
            "state": (
                "seller_published"
                if is_published
                else "seller_unpublished"
            ),
            "label": (
                "Walmart Seller Ã¢â‚¬â€ Published"
                if is_published
                else "Walmart Seller Ã¢â‚¬â€ Unpublished"
            ),
            "detail": (
                seller.get("title")
                or "Already in your Walmart seller catalog"
            ),
            "barcode": seller_match["barcode"],
            "sku": seller.get("sku"),
            "listing_status": published_status,
            "lifecycle_status": (
                seller.get("lifecycle_status")
            ),
            "price": seller.get("price"),
            "item_id": seller.get("item_id"),
            "image_url": catalog.get("image_url"),
        }

    catalog_match = next(
        (
            match
            for match in matches
            if (
                match.get("catalog")
                and str(
                    match["catalog"].get(
                        "match_status"
                    )
                ).upper() == "MATCH"
            )
        ),
        None,
    )

    if catalog_match:
        catalog = catalog_match["catalog"]
        return {
            "state": "catalog_match",
            "label": "Walmart Catalog Match",
            "detail": (
                catalog.get("title")
                or "Published product found on Walmart"
            ),
            "barcode": catalog_match["barcode"],
            "sku": None,
            "listing_status": "MATCH",
            "lifecycle_status": None,
            "price": catalog.get("price"),
            "item_id": catalog.get("item_id"),
            "image_url": catalog.get("image_url"),
        }

    not_found_match = next(
        (
            match
            for match in matches
            if (
                match.get("catalog")
                and str(
                    match["catalog"].get(
                        "match_status"
                    )
                ).upper() in {
                    "NOT_FOUND",
                    "INVALID_BARCODE",
                }
            )
        ),
        None,
    )

    if not_found_match:
        catalog = not_found_match["catalog"]
        invalid = (
            str(catalog.get("match_status")).upper()
            == "INVALID_BARCODE"
        )
        return {
            "state": (
                "invalid" if invalid else "not_found"
            ),
            "label": (
                "Invalid Walmart Barcode"
                if invalid
                else "Not Found on Walmart"
            ),
            "detail": (
                "Barcode could not be converted to UPC/GTIN"
                if invalid
                else "No published Walmart catalog match"
            ),
            "barcode": not_found_match["barcode"],
            "sku": None,
            "listing_status": (
                catalog.get("match_status")
            ),
            "lifecycle_status": None,
            "price": None,
            "item_id": None,
            "image_url": None,
        }

    return {
        "state": "pending",
        "label": "Walmart Check Pending",
        "detail": (
            "This barcode has not completed the "
            "Walmart global-catalog check yet"
        ),
        "barcode": None,
        "sku": None,
        "listing_status": "PENDING",
        "lifecycle_status": None,
        "price": None,
        "item_id": None,
        "image_url": None,
    }


def lookup_barcode_local(barcode: str):
    barcode = str(barcode).strip()

    if not barcode:
        return {
            "found": False,
            "error": "No barcode supplied"
        }

    barcode_lookup = barcode.lstrip("0") or "0"
    conn = None

    try:
        conn = sqlite3.connect(
            DB_PATH,
            timeout=30
        )
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # First: BrooksHouse products already created in inventory.
        store_row = cursor.execute(
            """
            SELECT
                p.*,
                pb.barcode AS matched_barcode,
                pb.barcode_type,
                pb.quantity_per_scan,
                COALESCE(
                    (
                        SELECT pi.image_url
                        FROM product_images AS pi
                        WHERE pi.product_id = p.product_id
                          AND pi.image_url IS NOT NULL
                          AND TRIM(pi.image_url) != ''
                        ORDER BY
                            pi.is_primary DESC,
                            pi.image_id ASC
                        LIMIT 1
                    ),
                    (
                        SELECT pi.image_path
                        FROM product_images AS pi
                        WHERE pi.product_id = p.product_id
                          AND pi.image_path IS NOT NULL
                          AND TRIM(pi.image_path) != ''
                        ORDER BY
                            pi.is_primary DESC,
                            pi.image_id ASC
                        LIMIT 1
                    ),
                    (
                        SELECT pe.image_url
                        FROM product_enrichment AS pe
                        WHERE TRIM(pe.barcode) =
                              TRIM(pb.barcode)
                          AND pe.image_url IS NOT NULL
                          AND TRIM(pe.image_url) != ''
                        ORDER BY pe.enrichment_id DESC
                        LIMIT 1
                    )
                ) AS saved_image
            FROM product_barcodes AS pb
            JOIN products AS p
              ON p.product_id = pb.product_id
            WHERE TRIM(CAST(pb.barcode AS TEXT)) = ?
               OR LTRIM(
                    TRIM(CAST(pb.barcode AS TEXT)),
                    '0'
                  ) = ?
            ORDER BY
                CASE
                    WHEN TRIM(CAST(pb.barcode AS TEXT)) = ?
                    THEN 0
                    ELSE 1
                END,
                pb.is_primary DESC
            LIMIT 1
            """,
            (
                barcode,
                barcode_lookup,
                barcode
            )
        ).fetchone()

        store_result = {
            "found": store_row is not None,
            "barcode": barcode,
            "table": "products",
            "match_source": "brookshouse_product",
            "match_type": None,
            "data": dict(store_row) if store_row is not None else {},
        }

        if store_row is not None:
            stored_barcode = str(
                store_row["matched_barcode"] or ""
            ).strip()
            store_result["match_type"] = (
                "exact"
                if stored_barcode == barcode
                else "normalized"
            )

        # Always check the imported 81,000-row catalog, even when a
        # BrooksHouse product exists. Smart Scan displays both sources.
        catalog_row = cursor.execute(
            """
            SELECT *
            FROM master_catalog
            WHERE TRIM(CAST(barcode_exact AS TEXT)) = ?
               OR TRIM(CAST(barcode_raw AS TEXT)) = ?
               OR TRIM(CAST(barcode_lookup AS TEXT)) = ?
               OR TRIM(CAST(barcode_lookup AS TEXT)) = ?
               OR LTRIM(
                    TRIM(CAST(barcode_exact AS TEXT)),
                    '0'
                  ) = ?
               OR LTRIM(
                    TRIM(CAST(barcode_raw AS TEXT)),
                    '0'
                  ) = ?
            ORDER BY
                CASE
                    WHEN TRIM(
                        CAST(barcode_exact AS TEXT)
                    ) = ?
                    THEN 0
                    WHEN TRIM(
                        CAST(barcode_raw AS TEXT)
                    ) = ?
                    THEN 1
                    ELSE 2
                END
            LIMIT 1
            """,
            (
                barcode,
                barcode,
                barcode,
                barcode_lookup,
                barcode_lookup,
                barcode_lookup,
                barcode,
                barcode
            )
        ).fetchone()

        catalog_result = {
            "found": catalog_row is not None,
            "barcode": barcode,
            "table": "master_catalog",
            "match_source": "reference_catalog",
            "match_type": None,
            "data": dict(catalog_row) if catalog_row is not None else {},
        }

        if catalog_row is not None:
            catalog_result["match_type"] = (
                "exact"
                if str(
                    catalog_row["barcode_exact"] or ""
                ).strip() == barcode
                else "normalized"
            )

        # Keep the legacy data field for compatibility while returning
        # both independent local sources to the new three-source UI.
        preferred_result = (
            store_result
            if store_result["found"]
            else catalog_result
        )

        return {
            "found": (
                store_result["found"]
                or catalog_result["found"]
            ),
            "barcode": barcode,
            "store_product": store_result,
            "master_catalog": catalog_result,
            "table": preferred_result["table"],
            "match_source": preferred_result["match_source"],
            "match_type": preferred_result["match_type"],
            "data": preferred_result["data"],
        }

    except Exception as exc:
        return {
            "found": False,
            "barcode": barcode,
            "error": str(exc)
        }

    finally:
        if conn is not None:
            conn.close()


def save_product_record(
    database: Session,
    product_data: ProductCreate,
    image_path: Optional[str] = None,
) -> Product:
    existing_barcode = database.scalar(
        select(ProductBarcode).where(
            ProductBarcode.barcode == product_data.barcode
        )
    )

    if existing_barcode is not None:
        raise ValueError(
            "That barcode is already assigned to another product."
        )

    location = database.get(
        InventoryLocation,
        product_data.location_id,
    )

    if location is None:
        raise ValueError("The selected inventory location was not found.")

    if not location.active:
        raise ValueError("The selected inventory location is inactive.")

    product = Product(
        product_name=product_data.product_name,
        brand=product_data.brand,
        description=product_data.description,
        category=product_data.category,
        size_value=product_data.size_value,
        size_unit=product_data.size_unit,
        pack_quantity=product_data.pack_quantity,
        suggested_retail_price=product_data.suggested_retail_price,
        store_price=product_data.store_price,
        average_cost=product_data.average_cost,
        taxable=product_data.taxable,
        active=True,
    )

    product.barcodes.append(
        ProductBarcode(
            barcode=product_data.barcode,
            barcode_type=product_data.barcode_type,
            is_primary=True,
            quantity_per_scan=product_data.quantity_per_scan,
        )
    )

    if image_path is not None:
        product.images.append(
            ProductImage(
                image_path=image_path,
                image_type="front",
                is_primary=True,
            )
        )

    inventory_record = Inventory(
        product=product,
        location=location,
        quantity_on_hand=product_data.starting_quantity,
        quantity_reserved=0,
        reorder_level=0,
    )

    database.add(product)
    database.add(inventory_record)

    if product_data.starting_quantity > 0:
        database.add(
            InventoryTransaction(
                product=product,
                location=location,
                transaction_type="initial_receiving",
                quantity_change=product_data.starting_quantity,
                unit_cost=product_data.average_cost,
                reference_number="INITIAL-SETUP",
                notes=(
                    product_data.notes
                    or "Starting inventory entered when product was created."
                ),
            )
        )

    if product_data.store_price is not None:
        database.add(
            PriceHistory(
                product=product,
                old_price=None,
                new_price=product_data.store_price,
                price_type="store_price",
                reason="Initial product price",
            )
        )

    try:
        database.commit()
        database.refresh(product)

    except Exception:
        database.rollback()
        raise

    return product



def normalize_container_id(value: str | None) -> str:
    """Convert Container IDs to one consistent format."""
    if not value:
        return ""

    normalized = value.strip().upper()

    # Treat spaces and underscores like hyphens.
    normalized = re.sub(
        r"[\s_]+",
        "-",
        normalized,
    )

    # Remove repeated hyphens.
    normalized = re.sub(
        r"-+",
        "-",
        normalized,
    )

    return normalized


@app.get("/smart-scan", response_class=HTMLResponse)
def smart_scan_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="smart_scan.html",
        context={}
    )

@app.get(
    "/",
    response_class=HTMLResponse,
)
def home():
    return RedirectResponse(
        url="/products",
        status_code=status.HTTP_302_FOUND,
    )




def find_shopify_listings(
    database: Session,
    barcode_values: dict,
) -> tuple[list[ChannelListing], Optional[str]]:
    channel = database.scalar(
        select(SalesChannel).where(
            SalesChannel.channel_name == "Shopify"
        )
    )

    if channel is None:
        return [], None

    exact_barcode = barcode_values["exact"]
    lookup_barcode = barcode_values["lookup"]

    without_check_digit = (
        barcode_values["without_check_digit"]
    )

    lookup_without_check_digit = (
        barcode_values[
            "lookup_without_check_digit"
        ]
    )

    listings = database.scalars(
        select(ChannelListing)
        .where(
            ChannelListing.channel_id
            == channel.channel_id,
            ChannelListing.barcode_exact
            == exact_barcode,
        )
        .order_by(
            ChannelListing.listing_title,
            ChannelListing.listing_id,
        )
    ).all()

    if listings:
        return listings, "exact"

    listings = database.scalars(
        select(ChannelListing)
        .where(
            ChannelListing.channel_id
            == channel.channel_id,
            ChannelListing.barcode_lookup
            == lookup_barcode,
        )
        .order_by(
            ChannelListing.listing_title,
            ChannelListing.listing_id,
        )
    ).all()

    if listings:
        return listings, "leading_zero"

    if without_check_digit:
        listings = database.scalars(
            select(ChannelListing)
            .where(
                ChannelListing.channel_id
                == channel.channel_id,
                ChannelListing.barcode_exact
                == without_check_digit,
            )
            .order_by(
                ChannelListing.listing_title,
                ChannelListing.listing_id,
            )
        ).all()

        if listings:
            return listings, "check_digit_removed"

    if lookup_without_check_digit:
        listings = database.scalars(
            select(ChannelListing)
            .where(
                ChannelListing.channel_id
                == channel.channel_id,
                ChannelListing.barcode_lookup
                == lookup_without_check_digit,
            )
            .order_by(
                ChannelListing.listing_title,
                ChannelListing.listing_id,
            )
        ).all()

        if listings:
            return (
                listings,
                "check_digit_and_leading_zero",
            )

    return [], None


def normalize_scan_barcode(value: str) -> dict:
    cleaned = "".join(
        character
        for character in value.strip()
        if character.isdigit()
    )

    if not cleaned:
        raise ValueError(
            "The barcode must contain numbers."
        )

    lookup = cleaned.lstrip("0") or "0"

    barcode_without_check_digit = None
    lookup_without_check_digit = None

    # UPC-A, EAN-13, UPC-E, and EAN-8 normally include
    # a final check digit. Some catalog exports omit it.
    if len(cleaned) in {8, 12, 13, 14}:
        barcode_without_check_digit = cleaned[:-1]

        lookup_without_check_digit = (
            barcode_without_check_digit.lstrip("0")
            or "0"
        )

    return {
        "exact": cleaned,
        "lookup": lookup,
        "without_check_digit": barcode_without_check_digit,
        "lookup_without_check_digit": lookup_without_check_digit,
    }


@app.get(
    "/scan",
    response_class=HTMLResponse,
)
def scan_item_page(
    request: Request,
):
    return templates.TemplateResponse(
        request=request,
        name="scan.html",
        context={
            "searched": False,
            "scanned_barcode": None,
            "lookup_barcode": None,
            "store_product": None,
            "catalog_matches": [],
            "match_type": None,
            "shopify_matches": [],
            "shopify_match_type": None,
        },
    )
@app.get("/api/smart-scan/{barcode}")
def smart_scan_lookup(barcode: str):
    # Smart Scan intentionally gathers all three independent sources.
    # The frontend displays Internet, BrooksHouse, and Master Catalog
    # together so the user can choose which values to retain.
    online = lookup_upc_online(barcode)
    local = lookup_barcode_local(barcode)

    return {
        "barcode": barcode,
        "online": online,
        "local": local,
        "store_product": local.get("store_product", {}),
        "master_catalog": local.get("master_catalog", {}),
    }


@app.post("/api/smart-scan/approve")
def approve_smart_scan(item: SmartScanApproval):

    barcode = item.barcode.strip()

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        barcode_lookup = barcode.lstrip("0") or "0"

        store_product = cursor.execute(
            """
            SELECT p.product_id
            FROM product_barcodes AS pb
            JOIN products AS p
              ON p.product_id = pb.product_id
            WHERE TRIM(CAST(pb.barcode AS TEXT)) = ?
               OR LTRIM(
                    TRIM(CAST(pb.barcode AS TEXT)),
                    '0'
                  ) = ?
            ORDER BY
                CASE
                    WHEN TRIM(CAST(pb.barcode AS TEXT)) = ?
                    THEN 0
                    ELSE 1
                END,
                pb.is_primary DESC
            LIMIT 1
            """,
            (barcode, barcode_lookup, barcode),
        ).fetchone()

        existing = cursor.execute(
            """
            SELECT *
            FROM master_catalog
            WHERE barcode_exact = ?
               OR barcode_lookup = ?
               OR barcode_raw = ?
            LIMIT 1
            """,
            (barcode, barcode, barcode),
        ).fetchone()

        timestamp = datetime.now().isoformat(timespec="seconds")

        note = (
            f"Smart Scan approved {timestamp}. "
            f"Source: {item.source or 'unknown'}"
        )

        # Approved values belong on the BrooksHouse product. The imported
        # master catalog remains unchanged as a reference source.
        if store_product:
            cursor.execute(
                """
                UPDATE products
                SET product_name = COALESCE(?, product_name),
                    description = COALESCE(?, description),
                    brand = COALESCE(?, brand),
                    category = COALESCE(?, category),
                    updated_at = ?
                WHERE product_id = ?
                """,
                (
                    item.description,
                    item.description,
                    item.brand,
                    item.category,
                    timestamp,
                    store_product["product_id"],
                ),
            )

        cursor.execute(
            """
            INSERT INTO product_enrichment (
                barcode,
                brand,
                category,
                model,
                description,
                image_url,
                internet_price_low,
                internet_price_high,
                lookup_source,
                approved_source,
                last_checked_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(barcode) DO UPDATE SET
                brand = excluded.brand,
                category = excluded.category,
                model = excluded.model,
                description = excluded.description,
                image_url = excluded.image_url,
                internet_price_low = excluded.internet_price_low,
                internet_price_high = excluded.internet_price_high,
                lookup_source = excluded.lookup_source,
                approved_source = excluded.approved_source,
                last_checked_at = excluded.last_checked_at,
                updated_at = excluded.updated_at
            """,
            (
                barcode,
                item.brand,
                item.category,
                item.model,
                item.description,
                item.image_url,
                item.internet_price_low,
                item.internet_price_high,
                item.source,
                item.source,
                timestamp,
                timestamp,
            ),
        )

        conn.commit()

        return {
            "success": True,
            "action": (
                "product_updated"
                if store_product
                else "enrichment_only"
            ),
            "product_id": (
                store_product["product_id"]
                if store_product
                else None
            ),
            "catalog_id": existing["catalog_id"] if existing else None,
            "barcode": barcode,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


class SmartScanCreateProduct(BaseModel):
    barcode: str

    product_name: str
    brand: str | None = None
    description: str | None = None
    category: str | None = None

    size_value: float | None = None
    size_unit: str | None = None
    pack_quantity: int = 1

    suggested_retail_price: float | None = None
    store_price: float | None = None
    average_cost: float | None = None
    image_url: str | None = None

    taxable: bool = True
@app.post("/api/smart-scan/create-product")
def smart_scan_create_product(item: SmartScanCreateProduct):

    barcode = item.barcode.strip()

    if not item.product_name.strip():
        return {
            "success": False,
            "message": "Product name is required."
        }

    with SessionLocal() as database:
        try:
            existing_barcode = database.scalar(
                select(ProductBarcode).where(
                    ProductBarcode.barcode == barcode
                )
            )

            if existing_barcode is not None:
                return {
                    "success": False,
                    "message": (
                        "This barcode is already assigned to "
                        f"{existing_barcode.product.product_name}."
                    ),
                    "product_id": existing_barcode.product_id,
                    "barcode": barcode,
                }

            default_location = database.scalar(
                select(InventoryLocation)
                .where(
                    func.lower(InventoryLocation.location_type)
                    == "catalog"
                )
                .order_by(InventoryLocation.location_id)
            )

            if default_location is None:
                default_location = InventoryLocation(
                    location_name="Active Item / No Inventory",
                    location_type="catalog",
                    description=(
                        "Active products saved in the catalog but not yet "
                        "counted into a physical inventory location."
                    ),
                    active=True,
                )
                database.add(default_location)
                database.flush()

            product_data = ProductCreate(
                barcode=barcode,
                barcode_type="UPC-A",
                product_name=item.product_name.strip(),
                brand=item.brand.strip() if item.brand else None,
                description=item.description.strip() if item.description else None,
                category=item.category.strip() if item.category else None,
                size_value=item.size_value,
                size_unit=item.size_unit.strip() if item.size_unit else None,
                pack_quantity=item.pack_quantity or 1,
                quantity_per_scan=1,
                suggested_retail_price=item.suggested_retail_price,
                store_price=item.store_price,
                average_cost=item.average_cost,
                taxable=item.taxable,
                starting_quantity=0,
                location_id=default_location.location_id,
                notes="Created through Smart Scan.",
            )

            product = save_product_record(
                database=database,
                product_data=product_data,
                image_path=None,
            )

            if item.image_url and item.image_url.strip():
                product.images.append(
                    ProductImage(
                        image_url=item.image_url.strip(),
                        image_type="front",
                        is_primary=True,
                    )
                )
                database.commit()
                database.refresh(product)

            return {
                "success": True,
                "product_id": product.product_id,
                "barcode": barcode,
                "location_id": default_location.location_id,
                "location_name": default_location.location_name,
                "message": "BrooksHouse product created.",
            }

        except (ValueError, IntegrityError) as exc:
            database.rollback()
            return {
                "success": False,
                "message": str(exc),
            }

        except Exception as exc:
            database.rollback()
            return {
                "success": False,
                "message": f"Create product database error: {exc}",
            }



@app.post(
    "/scan",
    response_class=HTMLResponse,
)
def check_scanned_item(
    request: Request,
    barcode: str = Form(...),
    database: Session = Depends(get_database),
):
    try:
        barcode_values = normalize_scan_barcode(
            barcode
        )

        cleaned_barcode = barcode_values["exact"]
        lookup_barcode = barcode_values["lookup"]

        barcode_without_check_digit = (
            barcode_values["without_check_digit"]
        )

        lookup_without_check_digit = (
            barcode_values[
                "lookup_without_check_digit"
            ]
        )

    except ValueError as error:
        return templates.TemplateResponse(
            request=request,
            name="scan.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            context={
                "searched": False,
                "scanned_barcode": barcode,
                "lookup_barcode": None,
                "store_product": None,
                "catalog_matches": [],
                "match_type": None,
                "shopify_matches": [],
                "shopify_match_type": None,
                "error": str(error),
            },
        )

    store_barcode = database.scalar(
        select(ProductBarcode).where(
            ProductBarcode.barcode == cleaned_barcode
        )
    )

    store_match_type = (
        "exact"
        if store_barcode is not None
        else None
    )

    if store_barcode is None:
        store_barcode = database.scalar(
            select(ProductBarcode).where(
                func.ltrim(
                    ProductBarcode.barcode,
                    "0",
                ) == lookup_barcode
            )
        )

        if store_barcode is not None:
            store_match_type = "leading_zero"

    if (
        store_barcode is None
        and barcode_without_check_digit
    ):
        store_barcode = database.scalar(
            select(ProductBarcode).where(
                ProductBarcode.barcode
                == barcode_without_check_digit
            )
        )

        if store_barcode is not None:
            store_match_type = "check_digit_removed"

    if (
        store_barcode is None
        and lookup_without_check_digit
    ):
        store_barcode = database.scalar(
            select(ProductBarcode).where(
                func.ltrim(
                    ProductBarcode.barcode,
                    "0",
                )
                == lookup_without_check_digit
            )
        )

        if store_barcode is not None:
            store_match_type = (
                "check_digit_and_leading_zero"
            )

    store_product = (
        store_barcode.product
        if store_barcode is not None
        else None
    )

    catalog_matches = database.scalars(
        select(MasterCatalog)
        .where(
            MasterCatalog.barcode_exact
            == cleaned_barcode
        )
        .order_by(
            MasterCatalog.description,
            MasterCatalog.catalog_id,
        )
    ).all()

    match_type = "exact"

    if not catalog_matches:
        catalog_matches = database.scalars(
            select(MasterCatalog)
            .where(
                MasterCatalog.barcode_lookup
                == lookup_barcode
            )
            .order_by(
                MasterCatalog.description,
                MasterCatalog.catalog_id,
            )
        ).all()

        if catalog_matches:
            match_type = "leading_zero"

    if (
        not catalog_matches
        and barcode_without_check_digit
    ):
        catalog_matches = database.scalars(
            select(MasterCatalog)
            .where(
                MasterCatalog.barcode_exact
                == barcode_without_check_digit
            )
            .order_by(
                MasterCatalog.description,
                MasterCatalog.catalog_id,
            )
        ).all()

        if catalog_matches:
            match_type = "check_digit_removed"

    if (
        not catalog_matches
        and lookup_without_check_digit
    ):
        catalog_matches = database.scalars(
            select(MasterCatalog)
            .where(
                MasterCatalog.barcode_lookup
                == lookup_without_check_digit
            )
            .order_by(
                MasterCatalog.description,
                MasterCatalog.catalog_id,
            )
        ).all()

        if catalog_matches:
            match_type = (
                "check_digit_and_leading_zero"
            )

    (
        shopify_matches,
        shopify_match_type,
    ) = find_shopify_listings(
        database=database,
        barcode_values=barcode_values,
    )

    return templates.TemplateResponse(
        request=request,
        name="scan.html",
        context={
            "searched": True,
            "scanned_barcode": cleaned_barcode,
            "lookup_barcode": lookup_barcode,
            "store_product": store_product,
            "store_match_type": store_match_type,
            "catalog_matches": catalog_matches,
            "match_type": match_type,
            "barcode_without_check_digit": (
                barcode_without_check_digit
            ),
            "shopify_matches": shopify_matches,
            "shopify_match_type": shopify_match_type,
        },
    )


@app.get(
    "/products",
    response_class=HTMLResponse,
)
def product_list_page(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    database: Session = Depends(get_database),
):
    from sqlalchemy import func

    allowed_page_sizes = (20, 50, 100)

    if per_page not in allowed_page_sizes:
        per_page = 20

    total_products = (
        database.scalar(
            select(func.count())
            .select_from(Product)
        )
        or 0
    )

    total_pages = max(
        1,
        (total_products + per_page - 1) // per_page,
    )

    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    products = database.scalars(
        select(Product)
        .order_by(Product.created_at.desc())
        .offset(offset)
        .limit(per_page)
    ).all()

    if total_products:
        start_item = offset + 1
        end_item = min(
            offset + per_page,
            total_products,
        )
    else:
        start_item = 0
        end_item = 0

    return templates.TemplateResponse(
        request=request,
        name="products.html",
        context={
            "products": products,
            "page": page,
            "per_page": per_page,
            "total_products": total_products,
            "total_pages": total_pages,
            "start_item": start_item,
            "end_item": end_item,
            "allowed_page_sizes": allowed_page_sizes,
        },
    )


@app.get(
    "/products/add",
    response_class=HTMLResponse,
)
def add_product_page(
    request: Request,
    catalog_id: Optional[int] = None,
    barcode: Optional[str] = None,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    form_values = {
        "barcode": barcode or "",
        "product_name": "",
        "brand": "",
        "description": "",
        "average_cost": "",
    }

    if catalog_id is not None:
        catalog_record = database.get(
            MasterCatalog,
            catalog_id,
        )

        if catalog_record is not None:
            form_values.update(
                {
                    "barcode": (
                        barcode
                        or catalog_record.barcode_exact
                        or catalog_record.barcode_raw
                        or ""
                    ),
                    "product_name": (
                        catalog_record.description
                        or ""
                    ),
                    "description": (
                        catalog_record.description
                        or ""
                    ),
                    "average_cost": (
                        str(catalog_record.unit_cost)
                        if catalog_record.unit_cost
                        is not None
                        else ""
                    ),
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="add_product.html",
        context={
            "locations": locations,
            "message": None,
            "error": None,
            "form": form_values,
        },
    )


@app.post(
    "/products/add",
    response_class=HTMLResponse,
)
async def submit_product_page(
    request: Request,
    barcode: str = Form(...),
    barcode_type: str = Form("UPC-A"),
    product_name: str = Form(...),
    brand: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    size_value: str = Form(""),
    size_unit: str = Form(""),
    pack_quantity: int = Form(1),
    quantity_per_scan: int = Form(1),
    suggested_retail_price: str = Form(""),
    store_price: str = Form(""),
    average_cost: str = Form(""),
    starting_quantity: int = Form(0),
    location_id: int = Form(1),
    notes: str = Form(""),
    taxable: Optional[str] = Form(None),
    product_image: Optional[UploadFile] = File(None),
):
    image_file_path: Optional[Path] = None
    public_image_path: Optional[str] = None

    form_values = {
        "barcode": barcode,
        "product_name": product_name,
        "brand": brand,
        "description": description,
    }

    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        try:
            cleaned_barcode = barcode.strip().replace(" ", "")

            if not cleaned_barcode:
                raise ValueError("Barcode is required.")

            if not product_name.strip():
                raise ValueError("Product name is required.")

            product_data = ProductCreate(
                barcode=cleaned_barcode,
                barcode_type=barcode_type,
                product_name=product_name.strip(),
                brand=brand.strip() or None,
                description=description.strip() or None,
                category=category.strip() or None,
                size_value=optional_decimal(size_value),
                size_unit=size_unit.strip() or None,
                pack_quantity=pack_quantity,
                quantity_per_scan=quantity_per_scan,
                suggested_retail_price=optional_decimal(
                    suggested_retail_price
                ),
                store_price=optional_decimal(store_price),
                average_cost=optional_decimal(average_cost),
                taxable=taxable is not None,
                starting_quantity=starting_quantity,
                location_id=location_id,
                notes=notes.strip() or None,
            )

            if (
                product_image is not None
                and product_image.filename
            ):
                if product_image.content_type not in ALLOWED_IMAGE_TYPES:
                    raise ValueError(
                        "The product image must be JPG, PNG, or WEBP."
                    )

                extension = ALLOWED_IMAGE_TYPES[
                    product_image.content_type
                ]

                new_filename = (
                    f"{cleaned_barcode}-{uuid4().hex}{extension}"
                )

                image_file_path = IMAGE_DIRECTORY / new_filename
                image_bytes = await product_image.read()

                maximum_size = 8 * 1024 * 1024

                if len(image_bytes) > maximum_size:
                    raise ValueError(
                        "The product image must be smaller than 8 MB."
                    )

                image_file_path.write_bytes(image_bytes)

                public_image_path = (
                    f"/static/product-images/{new_filename}"
                )

            product = save_product_record(
                database=database,
                product_data=product_data,
                image_path=public_image_path,
            )

            return templates.TemplateResponse(
                request=request,
                name="add_product.html",
                context={
                    "locations": locations,
                    "message": (
                        f"{product.product_name} was saved successfully."
                    ),
                    "error": None,
                    "product_id": product.product_id,
                    "form": {},
                },
            )

        except (
            ValueError,
            IntegrityError,
        ) as error:
            database.rollback()

            if (
                image_file_path is not None
                and image_file_path.exists()
            ):
                image_file_path.unlink()

            return templates.TemplateResponse(
                request=request,
                name="add_product.html",
                status_code=status.HTTP_400_BAD_REQUEST,
                context={
                    "locations": locations,
                    "message": None,
                    "error": str(error),
                    "form": form_values,
                },
            )

        except Exception as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="add_product.html",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                context={
                    "locations": locations,
                    "message": None,
                    "error": (
                        "The product could not be saved. "
                        f"Technical details: {error}"
                    ),
                    "form": form_values,
                },
            )


@app.get(
    "/products/{product_id}",
    response_class=HTMLResponse,
)
def product_detail_page(
    request: Request,
    product_id: int,
    updated: bool = False,
    edit: bool = False,
    error: str = "",
    placement_updated: bool = False,
    placement_error: str = "",
    database: Session = Depends(get_database),
):
    product = database.get(Product, product_id)

    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )

    primary_barcode_record = next(
        (record for record in product.barcodes if record.is_primary),
        product.barcodes[0] if product.barcodes else None,
    )

    active_locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    inventory_rows = sorted(
        product.inventory_records,
        key=lambda record: (
            record.location.location_name.casefold()
            if record.location is not None
            else "",
            normalize_container_id(record.container_id).casefold(),
            record.inventory_id,
        ),
    )

    primary_image = next(
        (
            image
            for image in product.images
            if image.is_primary
        ),
        product.images[0] if product.images else None,
    )

    primary_image_url = None
    enrichment_image_url = None
    walmart_image_url = None

    if primary_image is not None:
        for field_name in (
            "image_url",
            "image_path",
            "url",
            "source_url",
            "external_url",
        ):
            image_value = getattr(primary_image, field_name, None)
            if image_value:
                primary_image_url = str(image_value)
                break

    if primary_image_url is None and product.barcodes:
        enrichment_connection = sqlite3.connect(DB_PATH, timeout=30)
        try:
            for barcode_record in product.barcodes:
                product_barcode = str(barcode_record.barcode or "").strip()
                if not product_barcode:
                    continue

                enrichment_row = enrichment_connection.execute(
                    """
                    SELECT image_url
                    FROM product_enrichment
                    WHERE TRIM(CAST(barcode AS TEXT)) = ?
                       OR LTRIM(TRIM(CAST(barcode AS TEXT)), '0') = ?
                    ORDER BY enrichment_id DESC
                    LIMIT 1
                    """,
                    (
                        product_barcode,
                        product_barcode.lstrip("0") or "0",
                    ),
                ).fetchone()

                if enrichment_row and enrichment_row[0]:
                    enrichment_image_url = str(enrichment_row[0])
                    primary_image_url = enrichment_image_url
                    break
        finally:
            enrichment_connection.close()

    if primary_image_url is None and product.barcodes:
        walmart_barcode_values = [
            barcode_record.barcode
            for barcode_record in product.barcodes
            if barcode_record.barcode
        ]

        if walmart_barcode_values:
            walmart_flags = load_walmart_inventory_flags(
                walmart_barcode_values
            )

            walmart = summarize_walmart_inventory_flag(
                product.barcodes,
                walmart_flags,
            )

            if walmart.get("image_url"):
                walmart_image_url = str(
                    walmart["image_url"]
                )
                primary_image_url = walmart_image_url

    # --------------------------------------------------
    # Direct Walmart image fallback for Product Detail
    # --------------------------------------------------
    if primary_image_url is None and product.barcodes:
        walmart_connection = sqlite3.connect(
            DB_PATH,
            timeout=30,
        )

        try:
            for barcode_record in product.barcodes:
                product_barcode = str(
                    barcode_record.barcode or ""
                ).strip()

                if not product_barcode:
                    continue

                barcode_lookup = (
                    product_barcode.lstrip("0")
                    or "0"
                )

                walmart_row = (
                    walmart_connection.execute(
                        """
                        SELECT image_url
                        FROM walmart_catalog_matches
                        WHERE match_status = 'MATCH'
                          AND image_url IS NOT NULL
                          AND TRIM(image_url) != ''
                          AND (
                                barcode_exact = ?
                                OR query_value = ?
                                OR barcode_lookup = ?
                          )
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (
                            product_barcode,
                            product_barcode,
                            barcode_lookup,
                        ),
                    ).fetchone()
                )

                if walmart_row and walmart_row[0]:
                    walmart_image_url = str(
                        walmart_row[0]
                    ).strip()

                    # Handle a URL accidentally stored in
                    # Markdown-link form:
                    # [https://example.com/image.jpg](https://...)
                    if (
                        walmart_image_url.startswith("[")
                        and "](" in walmart_image_url
                        and walmart_image_url.endswith(")")
                    ):
                        walmart_image_url = (
                            walmart_image_url
                            .split("](", 1)[1][:-1]
                        )

                    primary_image_url = walmart_image_url
                    break

        finally:
            walmart_connection.close()

    transaction_history = database.scalars(
        select(InventoryTransaction)
        .where(
            InventoryTransaction.product_id
            == product.product_id
        )
        .order_by(
            InventoryTransaction.transaction_id.desc()
        )
    ).unique().all()

    return templates.TemplateResponse(
        request=request,
        name="product_detail.html",
        context={
            "product": product,
            "transaction_history": transaction_history,
            "primary_image": primary_image,
            "primary_image_url": primary_image_url,
            "enrichment_image_url": enrichment_image_url,
            "walmart_image_url": walmart_image_url,
            "display_image_count": (
                len(product.images)
                if product.images
                else (
                    1
                    if enrichment_image_url or walmart_image_url
                    else 0
                )
            ),
            "updated": updated,
            "edit_mode": edit,
            "edit_error": error,
            "primary_barcode_value": (
                primary_barcode_record.barcode
                if primary_barcode_record is not None
                else ""
            ),
            "active_locations": active_locations,
            "inventory_rows": inventory_rows,
            "placement_updated": placement_updated,
            "placement_error": placement_error,
        },
    )


@app.post("/products/{product_id}/edit")
def update_product_detail(
    product_id: int,
    product_name: str = Form(...),
    barcode: str = Form(...),
    brand: str = Form(""),
    description: str = Form(""),
    category: str = Form(""),
    size_value: str = Form(""),
    size_unit: str = Form(""),
    pack_quantity: int = Form(1),
    suggested_retail_price: str = Form(""),
    store_price: str = Form(""),
    average_cost: str = Form(""),
    taxable: Optional[str] = Form(None),
    active: Optional[str] = Form(None),
    remove_image_ids: Optional[list[int]] = Form(None),
    remove_fallback_image: Optional[str] = Form(None),
    database: Session = Depends(get_database),
):
    product = database.get(Product, product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )

    try:
        clean_name = product_name.strip()
        clean_barcode = barcode.strip().replace(" ", "")
        if not clean_name:
            raise ValueError("Product name is required.")
        if len(clean_name) > 200:
            raise ValueError("Product name cannot exceed 200 characters.")
        if not clean_barcode:
            raise ValueError("Barcode is required.")
        if len(clean_barcode) > 50:
            raise ValueError("Barcode cannot exceed 50 characters.")
        if pack_quantity < 1:
            raise ValueError("Pack quantity must be at least 1.")

        existing_barcode = database.scalar(
            select(ProductBarcode).where(
                ProductBarcode.barcode == clean_barcode,
                ProductBarcode.product_id != product_id,
            )
        )
        if existing_barcode is not None:
            raise ValueError(
                f"Barcode {clean_barcode} already belongs to another product."
            )

        new_store_price = optional_decimal(store_price)
        old_store_price = product.store_price

        product.product_name = clean_name
        product.brand = brand.strip() or None
        product.description = description.strip() or None
        product.category = category.strip() or None
        product.size_value = optional_decimal(size_value)
        product.size_unit = size_unit.strip() or None
        product.pack_quantity = pack_quantity
        product.suggested_retail_price = optional_decimal(
            suggested_retail_price
        )
        product.store_price = new_store_price
        product.average_cost = optional_decimal(average_cost)
        product.taxable = taxable is not None
        product.active = active is not None

        image_ids_to_remove = set(remove_image_ids or [])
        for image_record in list(product.images):
            if image_record.image_id in image_ids_to_remove:
                database.delete(image_record)

        if remove_fallback_image is not None:
            for barcode_record in product.barcodes:
                product_barcode = str(barcode_record.barcode or "").strip()
                if not product_barcode:
                    continue
                barcode_lookup = product_barcode.lstrip("0") or "0"
                database.execute(
                    text(
                        """
                        UPDATE product_enrichment
                        SET image_url = NULL
                        WHERE TRIM(CAST(barcode AS TEXT)) = :exact
                           OR LTRIM(TRIM(CAST(barcode AS TEXT)), '0') = :lookup
                        """
                    ),
                    {"exact": product_barcode, "lookup": barcode_lookup},
                )
                database.execute(
                    text(
                        """
                        UPDATE walmart_catalog_matches
                        SET image_url = NULL
                        WHERE barcode_exact = :exact
                           OR query_value = :exact
                           OR barcode_lookup = :lookup
                        """
                    ),
                    {"exact": product_barcode, "lookup": barcode_lookup},
                )

        primary_barcode = next(
            (record for record in product.barcodes if record.is_primary),
            product.barcodes[0] if product.barcodes else None,
        )
        if primary_barcode is None:
            product.barcodes.append(
                ProductBarcode(
                    barcode=clean_barcode,
                    barcode_type="UPC-A" if len(clean_barcode) == 12 else "OTHER",
                    is_primary=True,
                    quantity_per_scan=1,
                )
            )
        else:
            primary_barcode.barcode = clean_barcode

        if (
            new_store_price is not None
            and new_store_price != old_store_price
        ):
            database.add(
                PriceHistory(
                    product=product,
                    old_price=old_store_price,
                    new_price=new_store_price,
                    price_type="store_price",
                    reason="Updated from product detail page",
                )
            )

        database.commit()
        return RedirectResponse(
            url=f"/products/{product_id}?updated=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except (ValueError, IntegrityError) as error:
        database.rollback()
        return RedirectResponse(
            url=(
                f"/products/{product_id}?edit=1&error="
                f"{quote_plus(str(error))}"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )


@app.post("/products/{product_id}/inventory/{inventory_id}/fix")
def fix_product_inventory_placement(
    product_id: int,
    inventory_id: int,
    action: str = Form("move"),
    destination_location_id: int = Form(...),
    destination_container_id: str = Form(""),
    quantity_to_move: int = Form(0),
    corrected_quantity: Optional[int] = Form(None),
    reason: str = Form("placement_correction"),
    notes: str = Form(""),
    database: Session = Depends(get_database),
):
    product = database.get(Product, product_id)
    source_inventory = database.get(Inventory, inventory_id)

    try:
        if product is None:
            raise ValueError("Product was not found.")
        if (
            source_inventory is None
            or source_inventory.product_id != product_id
        ):
            raise ValueError("That inventory row does not belong to this product.")

        source_location = source_inventory.location
        source_container = normalize_container_id(
            source_inventory.container_id
        )
        source_before = int(source_inventory.quantity_on_hand or 0)
        clean_reason = reason.strip() or "placement_correction"
        clean_notes = notes.strip()
        reference = (
            f"ITEM-FIX-{product_id}-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )

        if action == "correct_count":
            if corrected_quantity is None or corrected_quantity < 0:
                raise ValueError("Corrected quantity must be zero or greater.")
            if corrected_quantity < int(source_inventory.quantity_reserved or 0):
                raise ValueError(
                    "Corrected quantity cannot be lower than the reserved quantity."
                )
            quantity_change = corrected_quantity - source_before
            if quantity_change == 0:
                raise ValueError("The corrected quantity is already the current quantity.")

            source_inventory.quantity_on_hand = corrected_quantity
            database.add(
                InventoryTransaction(
                    product=product,
                    location=source_location,
                    container_id=source_container,
                    transaction_type="inventory_correction",
                    quantity_change=quantity_change,
                    unit_cost=product.average_cost,
                    reference_number=reference,
                    notes=(
                        f"Product Detail count correction ({clean_reason}). "
                        f"Quantity changed from {source_before} to "
                        f"{corrected_quantity}. {clean_notes}"
                    ).strip(),
                )
            )
        elif action == "move":
            destination_location = database.get(
                InventoryLocation,
                destination_location_id,
            )
            if destination_location is None or not destination_location.active:
                raise ValueError("Select an active destination location.")

            destination_container = normalize_container_id(
                destination_container_id
            )
            if quantity_to_move < 1:
                raise ValueError("Move quantity must be at least 1.")
            movable_quantity = max(
                0,
                source_before - int(source_inventory.quantity_reserved or 0),
            )
            if quantity_to_move > movable_quantity:
                raise ValueError(
                    f"Only {movable_quantity} unreserved unit(s) are available "
                    "to move from this row."
                )
            if (
                source_inventory.location_id == destination_location_id
                and source_container == destination_container
            ):
                raise ValueError(
                    "Choose a different location or container ID."
                )

            destination_records = database.scalars(
                select(Inventory).where(
                    Inventory.product_id == product_id,
                    Inventory.location_id == destination_location_id,
                )
            ).all()
            destination_inventory = next(
                (
                    record
                    for record in destination_records
                    if normalize_container_id(record.container_id)
                    == destination_container
                ),
                None,
            )
            if destination_inventory is None:
                destination_inventory = Inventory(
                    product=product,
                    location=destination_location,
                    container_id=destination_container,
                    quantity_on_hand=0,
                    quantity_reserved=0,
                    reorder_level=0,
                )
                database.add(destination_inventory)
                database.flush()

            destination_before = int(
                destination_inventory.quantity_on_hand or 0
            )
            source_inventory.quantity_on_hand = source_before - quantity_to_move
            destination_inventory.quantity_on_hand = (
                destination_before + quantity_to_move
            )

            movement_description = (
                f"Product Detail placement correction ({clean_reason}) from "
                f"{source_location.location_name} / "
                f"{source_container or 'No container'} to "
                f"{destination_location.location_name} / "
                f"{destination_container or 'No container'}."
            )
            database.add(
                InventoryTransaction(
                    product=product,
                    location=source_location,
                    container_id=source_container,
                    transaction_type="placement_transfer_out",
                    quantity_change=-quantity_to_move,
                    unit_cost=product.average_cost,
                    reference_number=reference,
                    notes=(
                        f"{movement_description} Source changed from "
                        f"{source_before} to {source_inventory.quantity_on_hand}. "
                        f"{clean_notes}"
                    ).strip(),
                )
            )
            database.add(
                InventoryTransaction(
                    product=product,
                    location=destination_location,
                    container_id=destination_container,
                    transaction_type="placement_transfer_in",
                    quantity_change=quantity_to_move,
                    unit_cost=product.average_cost,
                    reference_number=reference,
                    notes=(
                        f"{movement_description} Destination changed from "
                        f"{destination_before} to "
                        f"{destination_inventory.quantity_on_hand}. "
                        f"{clean_notes}"
                    ).strip(),
                )
            )
        else:
            raise ValueError("Choose Move placement or Correct count.")

        database.commit()
        return RedirectResponse(
            url=f"/products/{product_id}?placement_updated=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except (ValueError, IntegrityError) as error:
        database.rollback()
        return RedirectResponse(
            url=(
                f"/products/{product_id}?placement_error="
                f"{quote_plus(str(error))}#inventory-placement"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
def find_store_product_by_scan(
    database: Session,
    barcode: str,
) -> tuple[Optional[Product], str]:
    barcode_values = normalize_scan_barcode(barcode)

    exact_barcode = barcode_values["exact"]
    lookup_barcode = barcode_values["lookup"]

    without_check_digit = (
        barcode_values["without_check_digit"]
    )

    lookup_without_check_digit = (
        barcode_values[
            "lookup_without_check_digit"
        ]
    )

    barcode_record = database.scalar(
        select(ProductBarcode).where(
            ProductBarcode.barcode == exact_barcode
        )
    )

    if barcode_record is not None:
        return barcode_record.product, exact_barcode

    barcode_record = database.scalar(
        select(ProductBarcode).where(
            func.ltrim(
                ProductBarcode.barcode,
                "0",
            ) == lookup_barcode
        )
    )

    if barcode_record is not None:
        return barcode_record.product, exact_barcode

    if without_check_digit:
        barcode_record = database.scalar(
            select(ProductBarcode).where(
                ProductBarcode.barcode
                == without_check_digit
            )
        )

        if barcode_record is not None:
            return barcode_record.product, exact_barcode

    if lookup_without_check_digit:
        barcode_record = database.scalar(
            select(ProductBarcode).where(
                func.ltrim(
                    ProductBarcode.barcode,
                    "0",
                )
                == lookup_without_check_digit
            )
        )

        if barcode_record is not None:
            return barcode_record.product, exact_barcode

    return None, exact_barcode




# ============================================================
# PACKAGING BARCODE / MASTER CASE MAPPER
# ============================================================

def find_exact_product_barcode(
    database: Session,
    barcode: str,
):
    barcode_values = normalize_scan_barcode(
        barcode
    )

    exact_barcode = barcode_values[
        "exact"
    ]

    return database.scalar(
        select(ProductBarcode).where(
            ProductBarcode.barcode
            == exact_barcode
        )
    )




def suggested_pallet_prefix(
    location_name: str,
) -> str:
    raw_name = (location_name or "").strip()

    if not raw_name:
        return "LOC"

    upper_name = raw_name.upper()

    digits = "".join(
        c for c in raw_name
        if c.isdigit()
    )

    if "TRAILER" in upper_name:
        return f"T{digits}" if digits else "TRAILER"

    if "CONTAINER" in upper_name:
        return "SC"

    words = [
        word
        for word in raw_name.replace("-", " ").split()
        if word
    ]

    initials = "".join(
        word[0]
        for word in words[:3]
    ).upper()

    return initials or "LOC"



# ============================================================
# CODE39 BARCODE HELPERS
# ============================================================

CODE39_PATTERNS = {
    "0": "nnnwwnwnn",
    "1": "wnnwnnnnw",
    "2": "nnwwnnnnw",
    "3": "wnwwnnnnn",
    "4": "nnnwwnnnw",
    "5": "wnnwwnnnn",
    "6": "nnwwwnnnn",
    "7": "nnnwnnwnw",
    "8": "wnnwnnwnn",
    "9": "nnwwnnwnn",
    "A": "wnnnnwnnw",
    "B": "nnwnnwnnw",
    "C": "wnwnnwnnn",
    "D": "nnnnwwnnw",
    "E": "wnnnwwnnn",
    "F": "nnwnwwnnn",
    "G": "nnnnnwwnw",
    "H": "wnnnnwwnn",
    "I": "nnwnnwwnn",
    "J": "nnnnwwwnn",
    "K": "wnnnnnnww",
    "L": "nnwnnnnww",
    "M": "wnwnnnnwn",
    "N": "nnnnwnnww",
    "O": "wnnnwnnwn",
    "P": "nnwnwnnwn",
    "Q": "nnnnnnwww",
    "R": "wnnnnnwwn",
    "S": "nnwnnnwwn",
    "T": "nnnnwnwwn",
    "U": "wwnnnnnnw",
    "V": "nwwnnnnnw",
    "W": "wwwnnnnnn",
    "X": "nwnnwnnnw",
    "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn",
    "-": "nwnnnnwnw",
    ".": "wwnnnnwnn",
    " ": "nwwnnnwnn",
    "$": "nwnwnwnnn",
    "/": "nwnwnnnwn",
    "+": "nwnnnwnwn",
    "%": "nnnwnwnwn",
    "*": "nwnnwnwnn",
}

CODE39_ALLOWED = set(CODE39_PATTERNS.keys()) - {"*"}


def sanitize_code39_value(value: str) -> str:
    raw = (value or "").upper()
    sanitized = []

    for character in raw:
        if character in CODE39_ALLOWED:
            sanitized.append(character)
        elif character == "_":
            sanitized.append("-")
        else:
            sanitized.append("-")

    return "".join(sanitized) or "BLANK"


def build_code39_svg(
    value: str,
    narrow: int = 3,
    wide: int = 7,
    height: int = 70,
    quiet_zone: int = 12,
) -> str:
    clean_value = sanitize_code39_value(value)
    encoded = f"*{clean_value}*"

    x_position = quiet_zone
    rects = []

    for character in encoded:
        pattern = CODE39_PATTERNS[character]

        for index, width_code in enumerate(pattern):
            element_width = (
                wide if width_code == "w" else narrow
            )

            is_bar = (index % 2 == 0)

            if is_bar:
                rects.append(
                    f'<rect x="{x_position}" y="0" '
                    f'width="{element_width}" '
                    f'height="{height}" fill="#000000" />'
                )

            x_position += element_width

        x_position += narrow

    total_width = x_position + quiet_zone

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {total_width} {height}" '
        f'width="{total_width}" height="{height}" '
        f'role="img" aria-label="Code39 barcode for {clean_value}">'
        f'{"".join(rects)}'
        f"</svg>"
    )


def attach_label_barcodes(labels: list[dict]) -> list[dict]:
    for label in labels:
        label["barcode_value"] = sanitize_code39_value(
            label.get("code", "")
        )
        label["barcode_svg"] = build_code39_svg(
            label.get("code", "")
        )

    return labels


@app.get(
    "/pallet-labels",
    response_class=HTMLResponse,
)
def pallet_labels_page(
    request: Request,
    label_type: str = "TRAILER_PALLET",
    location_id: Optional[int] = None,
    prefix: str = "",
    positions: int = 32,
    start_number: int = 1,
    rack_number: int = 1,
    shelves: int = 5,
    custom_text: str = "",
    generate: str = "",
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    selected_location = None
    labels = []
    error = None

    allowed_types = {
        "TRAILER_PALLET": "Trailer Pallet",
        "TOTE": "Tote",
        "RACK": "Rack",
        "RACK_SHELF": "Rack Shelf",
        "BIN": "Bin",
        "STORAGE_POSITION": "Storage Container Position",
        "CUSTOM": "Custom",
    }

    clean_label_type = (
        label_type.strip().upper()
        if label_type
        else "TRAILER_PALLET"
    )

    if clean_label_type not in allowed_types:
        clean_label_type = "TRAILER_PALLET"

    if location_id is not None:
        selected_location = database.scalar(
            select(InventoryLocation).where(
                InventoryLocation.location_id == location_id
            )
        )

    if (
        selected_location is not None
        and not prefix.strip()
        and clean_label_type in {
            "TRAILER_PALLET",
            "STORAGE_POSITION",
        }
    ):
        prefix = suggested_pallet_prefix(
            selected_location.location_name
        )

    defaults = {
        "TOTE": "TOTE",
        "RACK": "R",
        "RACK_SHELF": "R",
        "BIN": "BIN",
    }

    if not prefix.strip() and clean_label_type in defaults:
        prefix = defaults[clean_label_type]

    if generate:
        try:
            if positions < 1:
                raise ValueError(
                    "Number of labels must be at least 1."
                )

            if positions > 500:
                raise ValueError(
                    "Keep one print run at 500 labels or fewer."
                )

            if start_number < 1:
                raise ValueError(
                    "Starting number must be at least 1."
                )

            if clean_label_type in {
                "TRAILER_PALLET",
                "STORAGE_POSITION",
            } and selected_location is None:
                raise ValueError(
                    "Choose a trailer or storage location first."
                )

            clean_prefix = prefix.strip().upper()

            if clean_label_type != "CUSTOM" and not clean_prefix:
                raise ValueError(
                    "Enter a label prefix."
                )

            location_name = (
                selected_location.location_name
                if selected_location
                else ""
            )

            if clean_label_type in {
                "TRAILER_PALLET",
                "STORAGE_POSITION",
            }:
                label_name = (
                    "Pallet Position"
                    if clean_label_type == "TRAILER_PALLET"
                    else "Storage Position"
                )

                for number in range(
                    start_number,
                    start_number + positions,
                ):
                    labels.append(
                        {
                            "code": f"{clean_prefix}-P{number:02d}",
                            "type": label_name,
                            "location": location_name,
                            "detail": f"Position {number}",
                        }
                    )

            elif clean_label_type == "TOTE":
                for number in range(
                    start_number,
                    start_number + positions,
                ):
                    labels.append(
                        {
                            "code": f"{clean_prefix}-{number:03d}",
                            "type": "Tote",
                            "location": location_name,
                            "detail": f"Tote {number}",
                        }
                    )

            elif clean_label_type == "BIN":
                for number in range(
                    start_number,
                    start_number + positions,
                ):
                    labels.append(
                        {
                            "code": f"{clean_prefix}-{number:03d}",
                            "type": "Bin",
                            "location": location_name,
                            "detail": f"Bin {number}",
                        }
                    )

            elif clean_label_type == "RACK":
                for number in range(
                    start_number,
                    start_number + positions,
                ):
                    labels.append(
                        {
                            "code": f"{clean_prefix}{number:02d}",
                            "type": "Rack",
                            "location": location_name,
                            "detail": f"Rack {number}",
                        }
                    )

            elif clean_label_type == "RACK_SHELF":
                if rack_number < 1:
                    raise ValueError(
                        "Rack number must be at least 1."
                    )

                if shelves < 1:
                    raise ValueError(
                        "Shelves must be at least 1."
                    )

                if shelves > 100:
                    raise ValueError(
                        "Keep shelves at 100 or fewer."
                    )

                for shelf_number in range(1, shelves + 1):
                    labels.append(
                        {
                            "code": (
                                f"{clean_prefix}{rack_number:02d}"
                                f"-S{shelf_number:02d}"
                            ),
                            "type": "Rack Shelf",
                            "location": location_name,
                            "detail": (
                                f"Rack {rack_number} Ã‚Â· "
                                f"Shelf {shelf_number}"
                            ),
                        }
                    )

            elif clean_label_type == "CUSTOM":
                custom_lines = [
                    line.strip()
                    for line in custom_text.splitlines()
                    if line.strip()
                ]

                if not custom_lines:
                    raise ValueError(
                        "Enter at least one custom label."
                    )

                for custom_code in custom_lines:
                    labels.append(
                        {
                            "code": custom_code,
                            "type": "Custom Label",
                            "location": location_name,
                            "detail": "",
                        }
                    )

            prefix = clean_prefix
            labels = attach_label_barcodes(labels)

        except ValueError as validation_error:
            error = str(validation_error)

    return templates.TemplateResponse(
        request=request,
        name="pallet_labels.html",
        context={
            "locations": locations,
            "selected_location_id": location_id,
            "selected_location": selected_location,
            "label_type": clean_label_type,
            "label_type_name": allowed_types[clean_label_type],
            "prefix": prefix,
            "positions": positions,
            "start_number": start_number,
            "rack_number": rack_number,
            "shelves": shelves,
            "custom_text": custom_text,
            "labels": labels,
            "generated": bool(labels),
            "error": error,
        },
    )




# ============================================================
# BROOKSHOUSE INTERNAL PRODUCT LABELS
# ============================================================


def brookshouse_part_number(product_id: int) -> str:
    return f"BHS-{product_id:06d}"


def brookshouse_internal_barcode(product_id: int) -> str:
    return f"BHS-P-{product_id:06d}"


def product_label_context(
    request: Request,
    database: Session,
    search: str = "",
    selected_product_id: Optional[int] = None,
    copies: int = 1,
    message: Optional[str] = None,
    error: Optional[str] = None,
):
    clean_search = clean_search_term(search)
    products = []
    selected_product = None
    internal_barcode_record = None

    if clean_search:
        search_like = sql_wildcard_pattern(clean_search)
        products = database.scalars(
            select(Product)
            .outerjoin(ProductBarcode)
            .where(
                or_(
                    Product.product_name.ilike(search_like, escape="\\"),
                    Product.brand.ilike(search_like, escape="\\"),
                    ProductBarcode.barcode.ilike(search_like, escape="\\"),
                )
            )
            .distinct()
            .order_by(Product.product_name)
            .limit(50)
        ).all()

    if selected_product_id is not None:
        selected_product = database.get(Product, selected_product_id)

        if selected_product is not None:
            expected_barcode = brookshouse_internal_barcode(
                selected_product.product_id
            )
            internal_barcode_record = database.scalar(
                select(ProductBarcode).where(
                    ProductBarcode.barcode == expected_barcode
                )
            )

    safe_copies = min(max(int(copies or 1), 1), 100)
    label = None

    if selected_product is not None:
        barcode_value = brookshouse_internal_barcode(
            selected_product.product_id
        )
        label = {
            "part_number": brookshouse_part_number(
                selected_product.product_id
            ),
            "barcode": barcode_value,
            "barcode_svg": build_code39_svg(
                barcode_value,
                narrow=3,
                wide=7,
                height=64,
            ),
            "product_name": selected_product.product_name,
            "brand": selected_product.brand or "",
            "store_price": selected_product.store_price,
        }

    return {
        "request": request,
        "search": clean_search,
        "products": products,
        "selected_product": selected_product,
        "selected_product_id": selected_product_id,
        "internal_barcode_record": internal_barcode_record,
        "label": label,
        "copies": safe_copies,
        "message": message,
        "error": error,
    }


@app.get(
    "/product-labels",
    response_class=HTMLResponse,
)
def product_labels_page(
    request: Request,
    search: str = "",
    product_id: Optional[int] = None,
    copies: int = 1,
    database: Session = Depends(get_database),
):
    return templates.TemplateResponse(
        request=request,
        name="product_labels.html",
        context=product_label_context(
            request=request,
            database=database,
            search=search,
            selected_product_id=product_id,
            copies=copies,
        ),
    )


@app.post(
    "/product-labels/assign",
    response_class=HTMLResponse,
)
def assign_internal_product_barcode(
    request: Request,
    product_id: int = Form(...),
    copies: int = Form(1),
    database: Session = Depends(get_database),
):
    product = database.get(Product, product_id)

    if product is None:
        return templates.TemplateResponse(
            request=request,
            name="product_labels.html",
            context=product_label_context(
                request=request,
                database=database,
                selected_product_id=product_id,
                copies=copies,
                error="The selected product was not found.",
            ),
            status_code=404,
        )

    barcode_value = brookshouse_internal_barcode(product.product_id)
    existing = database.scalar(
        select(ProductBarcode).where(
            ProductBarcode.barcode == barcode_value
        )
    )

    if existing is not None and existing.product_id != product.product_id:
        return templates.TemplateResponse(
            request=request,
            name="product_labels.html",
            context=product_label_context(
                request=request,
                database=database,
                selected_product_id=product_id,
                copies=copies,
                error=(
                    "That BrooksHouse barcode is already assigned "
                    "to another product. Nothing was changed."
                ),
            ),
            status_code=409,
        )

    if existing is None:
        has_primary = database.scalar(
            select(ProductBarcode.barcode_id)
            .where(ProductBarcode.product_id == product.product_id)
            .limit(1)
        ) is not None

        database.add(
            ProductBarcode(
                product_id=product.product_id,
                barcode=barcode_value,
                barcode_type="BROOKSHOUSE_INTERNAL",
                is_primary=not has_primary,
                quantity_per_scan=1,
            )
        )
        database.commit()
        message = (
            f"Assigned {barcode_value} to {product.product_name}. "
            "It is ready to scan and print."
        )
    else:
        message = (
            f"{barcode_value} is already assigned to "
            f"{product.product_name}. It is ready to reprint."
        )

    return templates.TemplateResponse(
        request=request,
        name="product_labels.html",
        context=product_label_context(
            request=request,
            database=database,
            selected_product_id=product_id,
            copies=copies,
            message=message,
        ),
    )



@app.get(
    "/barcode-mapper",
    response_class=HTMLResponse,
)
def packaging_barcode_mapper_page(
    request: Request,
    outer_barcode: Optional[str] = None,
    unit_barcode: Optional[str] = None,
    database: Session = Depends(get_database),
):
    product = None
    scanned_unit_barcode = None
    existing_outer_record = None
    error = None

    if unit_barcode:
        try:
            (
                product,
                scanned_unit_barcode,
            ) = find_store_product_by_scan(
                database=database,
                barcode=unit_barcode,
            )

        except ValueError as lookup_error:
            error = str(
                lookup_error
            )

    if outer_barcode:
        try:
            existing_outer_record = (
                find_exact_product_barcode(
                    database,
                    outer_barcode,
                )
            )

        except ValueError as lookup_error:
            if error is None:
                error = str(
                    lookup_error
                )

    return templates.TemplateResponse(
        request=request,
        name="barcode_mapper.html",
        context={
            "outer_barcode": (
                outer_barcode or ""
            ),
            "unit_barcode": (
                scanned_unit_barcode
                or unit_barcode
                or ""
            ),
            "product": product,
            "existing_outer_record": (
                existing_outer_record
            ),
            "message": None,
            "error": error,
        },
    )


@app.post(
    "/barcode-mapper/map",
    response_class=HTMLResponse,
)
def save_packaging_barcode_mapping(
    request: Request,
    outer_barcode: str = Form(...),
    unit_barcode: str = Form(...),
    package_level: str = Form(...),
    quantity_per_scan: int = Form(1),
    contained_barcode: str = Form(""),
    contained_quantity: int = Form(0),
    database: Session = Depends(get_database),
):
    product = None

    try:
        outer_values = normalize_scan_barcode(
            outer_barcode
        )

        unit_values = normalize_scan_barcode(
            unit_barcode
        )

        clean_outer_barcode = outer_values[
            "exact"
        ]

        clean_unit_barcode = unit_values[
            "exact"
        ]

        if (
            clean_outer_barcode
            == clean_unit_barcode
        ):
            raise ValueError(
                "The outside packaging barcode and "
                "unit barcode cannot be the same."
            )

        allowed_levels = {
            "INNER_PACK": "Inner Pack",
            "MASTER_CASE": "Master Case",
            "DISPLAY_CASE": "Display / Case Pack",
            "PALLET": "Pallet",
        }

        clean_package_level = (
            package_level
            .strip()
            .upper()
        )

        if (
            clean_package_level
            not in allowed_levels
        ):
            raise ValueError(
                "Select a valid packaging level."
            )

        (
            product,
            scanned_unit_barcode,
        ) = find_store_product_by_scan(
            database=database,
            barcode=clean_unit_barcode,
        )

        if product is None:
            raise ValueError(
                "The inside/unit barcode is not "
                "mapped to a BrooksHouse product yet. "
                "Scan or create the unit product first."
            )

        final_quantity_per_scan = (
            quantity_per_scan
        )

        hierarchy_message = None

        clean_contained_barcode = ""

        if contained_barcode.strip():
            contained_values = (
                normalize_scan_barcode(
                    contained_barcode
                )
            )

            clean_contained_barcode = (
                contained_values["exact"]
            )

            if contained_quantity < 1:
                raise ValueError(
                    "Enter how many contained packages "
                    "are inside this package."
                )

            contained_record = (
                find_product_barcode_record_by_scan(
                    database,
                    clean_contained_barcode,
                )
            )

            if contained_record is None:
                raise ValueError(
                    "The contained package barcode is not "
                    "mapped yet. Map the inner package first."
                )

            if (
                contained_record.product_id
                != product.product_id
            ):
                raise ValueError(
                    "The contained package barcode belongs "
                    "to a different BrooksHouse product."
                )

            contained_units = max(
                1,
                int(
                    contained_record.quantity_per_scan
                    or 1
                ),
            )

            final_quantity_per_scan = (
                contained_quantity
                * contained_units
            )

            hierarchy_message = (
                f"{contained_quantity} Ãƒâ€” "
                f"{contained_units} retail units "
                f"= {final_quantity_per_scan} "
                "retail units"
            )

        if final_quantity_per_scan < 2:
            raise ValueError(
                "Packaging quantity must represent at least "
                "2 retail units. Unit barcodes remain 1."
            )

        existing_outer_record = (
            find_exact_product_barcode(
                database,
                clean_outer_barcode,
            )
        )

        if (
            existing_outer_record is not None
            and existing_outer_record.product_id
            != product.product_id
        ):
            raise ValueError(
                "That outside barcode is already mapped "
                "to a different BrooksHouse product: "
                f"{existing_outer_record.product.product_name}."
            )

        if existing_outer_record is None:
            existing_outer_record = (
                ProductBarcode(
                    product=product,
                    barcode=clean_outer_barcode,
                    barcode_type=clean_package_level,
                    is_primary=False,
                    quantity_per_scan=(
                        final_quantity_per_scan
                    ),
                )
            )

            database.add(
                existing_outer_record
            )

            action_word = "Mapped"

        else:
            existing_outer_record.barcode_type = (
                clean_package_level
            )

            existing_outer_record.quantity_per_scan = (
                final_quantity_per_scan
            )

            existing_outer_record.is_primary = False

            action_word = "Updated"

        database.commit()
        database.refresh(
            product
        )

        if hierarchy_message:
            message = (
                f"{action_word} {allowed_levels[clean_package_level]} "
                f"barcode {clean_outer_barcode} to "
                f"{product.product_name}. "
                f"{hierarchy_message}."
            )

        else:
            message = (
                f"{action_word} outside barcode "
                f"{clean_outer_barcode} to "
                f"{product.product_name}. "
                f"One scan represents "
                f"{final_quantity_per_scan} retail units "
                f"as {allowed_levels[clean_package_level]}."
            )

        return templates.TemplateResponse(
            request=request,
            name="barcode_mapper.html",
            context={
                "outer_barcode": (
                    clean_outer_barcode
                ),
                "unit_barcode": (
                    scanned_unit_barcode
                ),
                "product": product,
                "existing_outer_record": (
                    existing_outer_record
                ),
                "message": message,
                "error": None,
            },
        )

    except ValueError as error:
        database.rollback()

        return templates.TemplateResponse(
            request=request,
            name="barcode_mapper.html",
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            context={
                "outer_barcode": (
                    outer_barcode
                ),
                "unit_barcode": (
                    unit_barcode
                ),
                "product": product,
                "existing_outer_record": None,
                "message": None,
                "error": str(
                    error
                ),
            },
        )

    except Exception as error:
        database.rollback()

        return templates.TemplateResponse(
            request=request,
            name="barcode_mapper.html",
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            context={
                "outer_barcode": (
                    outer_barcode
                ),
                "unit_barcode": (
                    unit_barcode
                ),
                "product": product,
                "existing_outer_record": None,
                "message": None,
                "error": (
                    "Packaging hierarchy mapping failed: "
                    f"{error}"
                ),
            },
        )


@app.get(
    "/inventory/receive",
    response_class=HTMLResponse,
)
def receive_inventory_page(
    request: Request,
    barcode: Optional[str] = None,
    location_id: Optional[int] = None,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(
            InventoryLocation.active.is_(True)
        )
        .order_by(
            InventoryLocation.location_name
        )
    ).all()

    product = None
    scanned_barcode = None
    searched = False
    error = None

    scan_details = {
        "barcode_record": None,
        "barcode_type": "UNIT",
        "quantity_per_scan": 1,
        "is_package": False,
    }

    if barcode:
        searched = True

        try:
            (
                product,
                scanned_barcode,
            ) = find_store_product_by_scan(
                database=database,
                barcode=barcode,
            )

            if product is not None:
                scan_details = (
                    receive_scan_details(
                        database,
                        barcode,
                        product,
                    )
                )

        except ValueError as lookup_error:
            error = str(
                lookup_error
            )

    return templates.TemplateResponse(
        request=request,
        name="receive_inventory.html",
        context={
            "locations": locations,
            "product": product,
            "scanned_barcode": (
                scanned_barcode
                or barcode
            ),
            "searched": searched,
            "selected_location_id": (
                location_id
            ),
            "scan_barcode_type": (
                scan_details[
                    "barcode_type"
                ]
            ),
            "scan_quantity_per_scan": (
                scan_details[
                    "quantity_per_scan"
                ]
            ),
            "scan_is_package": (
                scan_details[
                    "is_package"
                ]
            ),
            "message": None,
            "error": error,
        },
    )


@app.post(
    "/inventory/receive",
    response_class=HTMLResponse,
)
def submit_inventory_receipt(
    request: Request,
    product_id: int = Form(...),
    barcode: str = Form(""),
    quantity_received: int = Form(...),
    location_id: int = Form(...),
    unit_cost: str = Form(""),
    reference_number: str = Form(""),
    notes: str = Form(""),
):
    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        product = database.get(
            Product,
            product_id,
        )

        location = database.get(
            InventoryLocation,
            location_id,
        )

        try:
            if product is None:
                raise ValueError(
                    "The selected product was not found."
                )

            if location is None:
                raise ValueError(
                    "The selected inventory location was not found."
                )

            if not location.active:
                raise ValueError(
                    "The selected inventory location is inactive."
                )

            if quantity_received < 1:
                raise ValueError(
                    "Quantity received must be at least 1."
                )


            scan_details = (
                receive_scan_details(
                    database,
                    barcode,
                    product,
                )
            )

            quantity_per_scan = (
                scan_details[
                    "quantity_per_scan"
                ]
            )

            scan_barcode_type = (
                scan_details[
                    "barcode_type"
                ]
            )

            scan_is_package = (
                scan_details[
                    "is_package"
                ]
            )

            received_units = (
                quantity_received
                * quantity_per_scan
            )

            received_unit_cost = optional_decimal(
                unit_cost
            )

            inventory_record = database.scalar(
                select(Inventory).where(
                    Inventory.product_id
                    == product.product_id,
                    Inventory.location_id
                    == location.location_id,
                )
            )

            previous_location_quantity = 0

            if inventory_record is None:
                inventory_record = Inventory(
                    product=product,
                    location=location,
                    quantity_on_hand=0,
                    quantity_reserved=0,
                    reorder_level=0,
                )

                database.add(inventory_record)

            else:
                previous_location_quantity = (
                    inventory_record.quantity_on_hand
                )

            total_quantity_before = sum(
                record.quantity_on_hand
                for record
                in product.inventory_records
            )

            inventory_record.quantity_on_hand += (
                received_units
            )

            if received_unit_cost is not None:
                previous_average_cost = (
                    product.average_cost or Decimal("0")
                )

                total_cost_before = (
                    previous_average_cost
                    * Decimal(total_quantity_before)
                )

                received_total_cost = (
                    received_unit_cost
                    * Decimal(received_units)
                )

                new_total_quantity = (
                    total_quantity_before
                    + received_units
                )

                if new_total_quantity > 0:
                    product.average_cost = (
                        total_cost_before
                        + received_total_cost
                    ) / Decimal(new_total_quantity)

            transaction = InventoryTransaction(
                product=product,
                location=location,
                transaction_type="receiving",
                quantity_change=received_units,
                unit_cost=received_unit_cost,
                reference_number=(
                    reference_number.strip()
                    or None
                ),
                notes=(
                    notes.strip()
                    or (
                        (
                            "Inventory received through "
                            "the receiving page. "
                            f"Scanned {quantity_received} "
                            f"{scan_barcode_type} package"
                            f"{'' if quantity_received == 1 else 's'} "
                            f"at {quantity_per_scan} retail unit"
                            f"{'' if quantity_per_scan == 1 else 's'} "
                            "per scan."
                        )
                    )
                ),
            )

            database.add(transaction)
            database.commit()
            database.refresh(product)

            if scan_is_package:
                message = (
                    f"Received {quantity_received} "
                    f"{scan_barcode_type.replace('_', ' ').title()} "
                    f"scan{'' if quantity_received == 1 else 's'} "
                    f"x {quantity_per_scan} = "
                    f"{received_units} retail units of "
                    f"{product.product_name} into "
                    f"{location.location_name}. "
                    f"Location quantity changed from "
                    f"{previous_location_quantity} to "
                    f"{inventory_record.quantity_on_hand}."
                )
            else:
                message = (
                    f"Received {received_units} unit"
                    f"{'' if received_units == 1 else 's'} "
                    f"of {product.product_name} into "
                    f"{location.location_name}. "
                    f"Location quantity changed from "
                    f"{previous_location_quantity} to "
                    f"{inventory_record.quantity_on_hand}."
                )

            return templates.TemplateResponse(
                request=request,
                name="receive_inventory.html",
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_location_id": location_id,
                    "scan_barcode_type": scan_barcode_type,
                    "scan_quantity_per_scan": quantity_per_scan,
                    "scan_is_package": scan_is_package,
                    "message": message,
                    "error": None,
                },
            )

        except ValueError as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="receive_inventory.html",
                status_code=status.HTTP_400_BAD_REQUEST,
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_location_id": location_id,
                    "scan_barcode_type": "UNIT",
                    "scan_quantity_per_scan": 1,
                    "scan_is_package": False,
                    "message": None,
                    "error": str(error),
                },
            )

        except Exception as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="receive_inventory.html",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_location_id": location_id,
                    "scan_barcode_type": "UNIT",
                    "scan_quantity_per_scan": 1,
                    "scan_is_package": False,
                    "message": None,
                    "error": (
                        "Inventory could not be received. "
                        f"Technical details: {error}"
                    ),
                },
            )



@app.get(
    "/inventory/transfer",
    response_class=HTMLResponse,
)
def transfer_inventory_page(
    request: Request,
    barcode: Optional[str] = None,
    source_location_id: Optional[int] = None,
    source_container_id: Optional[str] = None,
    destination_location_id: Optional[int] = None,
    destination_container_id: Optional[str] = None,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    product = None
    scanned_barcode = None
    searched = False
    error = None

    if barcode:
        searched = True

        try:
            product, scanned_barcode = (
                find_store_product_by_scan(
                    database=database,
                    barcode=barcode,
                )
            )

        except ValueError as lookup_error:
            error = str(lookup_error)

    return templates.TemplateResponse(
        request=request,
        name="transfer_inventory.html",
        context={
            "locations": locations,
            "product": product,
            "scanned_barcode": (
                scanned_barcode or barcode
            ),
            "searched": searched,
            "selected_source_location_id": source_location_id,
            "selected_source_container_id": normalize_container_id(
                source_container_id
            ),
            "selected_destination_location_id": destination_location_id,
            "selected_destination_container_id": normalize_container_id(
                destination_container_id
            ),
            "message": None,
            "error": error,
        },
    )


@app.post(
    "/inventory/transfer",
    response_class=HTMLResponse,
)
def submit_inventory_transfer(
    request: Request,
    product_id: int = Form(...),
    barcode: str = Form(""),
    source_location_id: int = Form(...),
    source_container_id: str = Form(""),
    destination_location_id: int = Form(...),
    destination_container_id: str = Form(""),
    quantity_transferred: int = Form(...),
    reference_number: str = Form(""),
    notes: str = Form(""),
):
    # Regular Transfer is location-level replenishment.
    # It can pull from multiple source totes/containers automatically.
    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        product = database.get(
            Product,
            product_id,
        )

        source_location = database.get(
            InventoryLocation,
            source_location_id,
        )

        destination_location = database.get(
            InventoryLocation,
            destination_location_id,
        )

        try:
            clean_source_container_id = normalize_container_id(
                source_container_id
            )
            clean_destination_container_id = normalize_container_id(
                destination_container_id
            )

            if product is None:
                raise ValueError(
                    "The selected product was not found."
                )

            if source_location is None:
                raise ValueError(
                    "The source location was not found."
                )

            if destination_location is None:
                raise ValueError(
                    "The destination location was not found."
                )

            if (
                source_location_id == destination_location_id
                and clean_source_container_id
                == clean_destination_container_id
            ):
                raise ValueError(
                    "Source and destination cannot be the same "
                    "location and pallet/container."
                )

            if quantity_transferred < 1:
                raise ValueError(
                    "Transfer quantity must be at least 1."
                )

            source_inventories = database.scalars(
                select(Inventory)
                .where(
                    Inventory.product_id == product.product_id,
                    Inventory.location_id == source_location.location_id,
                    Inventory.quantity_on_hand > 0,
                )
                .order_by(Inventory.inventory_id)
            ).all()

            if clean_source_container_id:
                source_inventories = [
                    record
                    for record in source_inventories
                    if normalize_container_id(
                        record.container_id
                    )
                    == clean_source_container_id
                ]

            total_source_available = sum(
                int(record.quantity_on_hand or 0)
                for record in source_inventories
            )

            if total_source_available < quantity_transferred:
                source_description = (
                    f"{source_location.location_name} / "
                    f"{clean_source_container_id}"
                    if clean_source_container_id
                    else (
                        f"all containers at "
                        f"{source_location.location_name}"
                    )
                )

                raise ValueError(
                    f"Only {total_source_available} unit(s) are available "
                    f"from {source_description}."
                )

            destination_records = database.scalars(
                select(Inventory).where(
                    Inventory.product_id == product.product_id,
                    Inventory.location_id == destination_location.location_id,
                )
            ).all()

            destination_inventory = next(
                (
                    record
                    for record in destination_records
                    if not (record.container_id or "").strip()
                ),
                None,
            )

            if destination_inventory is None:
                destination_inventory = Inventory(
                    product=product,
                    location=destination_location,
                    container_id="",
                    quantity_on_hand=0,
                    quantity_reserved=0,
                    reorder_level=0,
                )
                database.add(destination_inventory)
                database.flush()

            destination_before = int(
                destination_inventory.quantity_on_hand or 0
            )

            transfer_reference = (
                reference_number.strip()
                or (
                    f"REPLENISH-"
                    f"{source_location.location_id}-"
                    f"{destination_location.location_id}-"
                    f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                )
            )

            base_notes = (
                notes.strip()
                or (
                    f"Inventory transfer from "
                    f"{source_location.location_name}"
                    f"{' / ' + clean_source_container_id if clean_source_container_id else ''} "
                    f"to {destination_location.location_name}"
                    f"{' / ' + clean_destination_container_id if clean_destination_container_id else ''}."
                )
            )

            remaining = quantity_transferred
            source_moves = []

            for source_inventory in source_inventories:
                if remaining <= 0:
                    break

                available = int(
                    source_inventory.quantity_on_hand or 0
                )

                if available <= 0:
                    continue

                quantity_from_record = min(
                    available,
                    remaining,
                )

                source_before = available
                source_after = (
                    source_before - quantity_from_record
                )

                source_inventory.quantity_on_hand = (
                    source_after
                )

                source_container = (
                    source_inventory.container_id or ""
                ).strip()

                source_moves.append(
                    {
                        "container": (
                            source_container or "Unassigned"
                        ),
                        "quantity": quantity_from_record,
                        "before": source_before,
                        "after": source_after,
                    }
                )

                source_note = (
                    f"{base_notes} "
                    f"Pulled {quantity_from_record} unit(s) from "
                    f"{source_container or 'unassigned inventory'}. "
                    f"Source quantity changed from "
                    f"{source_before} to {source_after}."
                )

                database.add(
                    InventoryTransaction(
                        product=product,
                        location=source_location,
                        container_id=source_container,
                        transaction_type="transfer_out",
                        quantity_change=-quantity_from_record,
                        unit_cost=product.average_cost,
                        reference_number=transfer_reference,
                        notes=source_note,
                    )
                )

                remaining -= quantity_from_record

            destination_inventory.quantity_on_hand = (
                destination_before + quantity_transferred
            )

            database.add(
                InventoryTransaction(
                    product=product,
                    location=destination_location,
                    container_id=clean_destination_container_id,
                    transaction_type="transfer_in",
                    quantity_change=quantity_transferred,
                    unit_cost=product.average_cost,
                    reference_number=transfer_reference,
                    notes=(
                        f"{base_notes} "
                        f"Received {quantity_transferred} unit(s). "
                        f"Destination quantity changed from "
                        f"{destination_before} to "
                        f"{destination_inventory.quantity_on_hand}."
                    ),
                )
            )

            database.commit()
            database.refresh(product)

            move_summary = ", ".join(
                f"{move['quantity']} from {move['container']}"
                for move in source_moves
            )

            message = (
                f"Transferred {quantity_transferred} unit"
                f"{'' if quantity_transferred == 1 else 's'} "
                f"of {product.product_name} from "
                f"{source_location.location_name}"
                f"{' / ' + clean_source_container_id if clean_source_container_id else ''} "
                f"to {destination_location.location_name}"
                f"{' / ' + clean_destination_container_id if clean_destination_container_id else ''}. "
                f"Pulled: {move_summary}. "
                f"Destination changed from "
                f"{destination_before} to "
                f"{destination_inventory.quantity_on_hand}."
            )

            return templates.TemplateResponse(
                request=request,
                name="transfer_inventory.html",
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_source_location_id": source_location_id,
                    "selected_source_container_id": clean_source_container_id,
                    "selected_destination_location_id": destination_location_id,
                    "selected_destination_container_id": clean_destination_container_id,
                    "message": message,
                    "error": None,
                },
            )

        except ValueError as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="transfer_inventory.html",
                status_code=status.HTTP_400_BAD_REQUEST,
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_source_location_id": source_location_id,
                    "selected_source_container_id": normalize_container_id(
                        source_container_id
                    ),
                    "selected_destination_location_id": destination_location_id,
                    "selected_destination_container_id": normalize_container_id(
                        destination_container_id
                    ),
                    "message": None,
                    "error": str(error),
                },
            )

        except Exception as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="transfer_inventory.html",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_source_location_id": source_location_id,
                    "selected_source_container_id": normalize_container_id(
                        source_container_id
                    ),
                    "selected_destination_location_id": destination_location_id,
                    "selected_destination_container_id": normalize_container_id(
                        destination_container_id
                    ),
                    "message": None,
                    "error": (
                        "Inventory could not be transferred. "
                        f"Technical details: {error}"
                    ),
                },
            )


@app.get(
    "/inventory/adjust",
    response_class=HTMLResponse,
)
def adjust_inventory_page(
    request: Request,
    barcode: Optional[str] = None,
    location_id: Optional[int] = None,
    container_id: Optional[str] = None,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    product = None
    scanned_barcode = None
    searched = False
    error = None

    if barcode:
        searched = True

        try:
            product, scanned_barcode = (
                find_store_product_by_scan(
                    database=database,
                    barcode=barcode,
                )
            )

        except ValueError as lookup_error:
            error = str(lookup_error)

    return templates.TemplateResponse(
        request=request,
        name="adjust_inventory.html",
        context={
            "locations": locations,
            "product": product,
            "scanned_barcode": (
                scanned_barcode or barcode
            ),
            "searched": searched,
            "selected_location_id": location_id,
            "selected_container_id": normalize_container_id(
                container_id
            ),
            "message": None,
            "error": error,
        },
    )


@app.post(
    "/inventory/adjust",
    response_class=HTMLResponse,
)
def submit_inventory_adjustment(
    request: Request,
    product_id: int = Form(...),
    barcode: str = Form(""),
    location_id: int = Form(...),
    container_id: str = Form(""),
    physical_quantity: int = Form(...),
    reason: str = Form(...),
    reference_number: str = Form(""),
    notes: str = Form(""),
):
    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        product = database.get(
            Product,
            product_id,
        )

        location = database.get(
            InventoryLocation,
            location_id,
        )

        try:
            if product is None:
                raise ValueError(
                    "The selected product was not found."
                )

            if location is None:
                raise ValueError(
                    "The selected inventory location was not found."
                )

            if not location.active:
                raise ValueError(
                    "The selected inventory location is inactive."
                )

            if physical_quantity < 0:
                raise ValueError(
                    "Physical quantity cannot be negative."
                )

            allowed_reasons = {
                "cycle_count",
                "shrinkage",
                "damage",
                "found_inventory",
                "data_entry_error",
                "return",
                "other",
            }

            if reason not in allowed_reasons:
                raise ValueError(
                    "Select a valid adjustment reason."
                )

            normalized_container_id = normalize_container_id(
                container_id
            )

            candidate_inventory_records = database.scalars(
                select(Inventory).where(
                    Inventory.product_id
                    == product.product_id,
                    Inventory.location_id
                    == location.location_id,
                )
            ).all()

            inventory_record = next(
                (
                    record
                    for record in candidate_inventory_records
                    if normalize_container_id(
                        record.container_id
                    )
                    == normalized_container_id
                ),
                None,
            )

            if inventory_record is None:
                inventory_record = Inventory(
                    product=product,
                    location=location,
                    container_id=(
                        normalized_container_id or None
                    ),
                    quantity_on_hand=0,
                    quantity_reserved=0,
                    reorder_level=0,
                )

                database.add(inventory_record)
                database.flush()

            previous_quantity = (
                inventory_record.quantity_on_hand
            )

            quantity_change = (
                physical_quantity - previous_quantity
            )

            inventory_record.quantity_on_hand = (
                physical_quantity
            )

            reason_labels = {
                "cycle_count": "Cycle count correction",
                "shrinkage": "Missing / shrinkage",
                "damage": "Damaged inventory",
                "found_inventory": "Found inventory",
                "data_entry_error": "Previous data-entry error",
                "return": "Customer return correction",
                "other": "Other adjustment",
            }

            adjustment_reference = (
                reference_number.strip()
                or (
                    f"ADJUST-"
                    f"{product.product_id}-"
                    f"{location.location_id}-"
                    f"{normalized_container_id or 'NO-CONTAINER'}"
                )
            )

            adjustment_notes = (
                f"Reason: {reason_labels[reason]}. "
                f"Location: {location.location_name}. "
                f"Container: "
                f"{normalized_container_id or 'No container'}. "
                f"System quantity: {previous_quantity}. "
                f"Physical quantity: {physical_quantity}."
            )

            if notes.strip():
                adjustment_notes += (
                    f" Notes: {notes.strip()}"
                )

            database.add(
                InventoryTransaction(
                    product=product,
                    location=location,
                    transaction_type="adjustment",
                    quantity_change=quantity_change,
                    unit_cost=product.average_cost,
                    reference_number=adjustment_reference,
                    notes=adjustment_notes,
                )
            )

            database.commit()
            database.refresh(product)

            if quantity_change > 0:
                direction_message = (
                    f"increased by {quantity_change}"
                )

            elif quantity_change < 0:
                direction_message = (
                    f"decreased by {abs(quantity_change)}"
                )

            else:
                direction_message = "did not change"

            message = (
                f"Inventory for {product.product_name} at "
                f"{location.location_name}"
                f"{' / ' + normalized_container_id if normalized_container_id else ''} "
                f"was adjusted from "
                f"{previous_quantity} to {physical_quantity}. "
                f"Inventory {direction_message}."
            )

            return templates.TemplateResponse(
                request=request,
                name="adjust_inventory.html",
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_location_id": location_id,
                    "selected_container_id": normalized_container_id,
                    "message": message,
                    "error": None,
                },
            )

        except ValueError as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="adjust_inventory.html",
                status_code=status.HTTP_400_BAD_REQUEST,
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_location_id": location_id,
                    "selected_container_id": normalize_container_id(
                        container_id
                    ),
                    "message": None,
                    "error": str(error),
                },
            )

        except Exception as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="adjust_inventory.html",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                context={
                    "locations": locations,
                    "product": product,
                    "scanned_barcode": barcode,
                    "searched": True,
                    "selected_location_id": location_id,
                    "selected_container_id": normalize_container_id(
                        container_id
                    ),
                    "message": None,
                    "error": (
                        "Inventory could not be adjusted. "
                        f"Technical details: {error}"
                    ),
                },
            )




@app.get(
    "/inventory/adjust/batch",
    response_class=HTMLResponse,
)
def batch_inventory_adjustment_page(
    request: Request,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="batch_adjust_inventory.html",
        context={
            "locations": locations,
            "message": None,
            "error": None,
            "saved_lines": [],
        },
    )


@app.get("/inventory/adjust/batch/lookup")
def batch_inventory_adjustment_lookup(
    barcode: str,
    database: Session = Depends(get_database),
):
    try:
        product, scanned_barcode = (
            find_store_product_by_scan(
                database=database,
                barcode=barcode,
            )
        )

        return {
            "found": True,
            "product_id": product.product_id,
            "product_name": product.product_name,
            "barcode": scanned_barcode,
            "store_price": (
                str(product.store_price)
                if product.store_price is not None
                else None
            ),
        }

    except ValueError as error:
        return {
            "found": False,
            "error": str(error),
        }


@app.post(
    "/inventory/adjust/batch",
    response_class=HTMLResponse,
)
def submit_batch_inventory_adjustment(
    request: Request,
    location_id: int = Form(...),
    container_id: str = Form(""),
    sublocation: str = Form(""),
    reason: str = Form(...),
    reference_number: str = Form(""),
    notes: str = Form(""),
    items_json: str = Form(...),
    scan_events_json: str = Form("[]"),
):
    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        try:
            location = database.get(
                InventoryLocation,
                location_id,
            )

            if location is None:
                raise ValueError(
                    "The selected inventory location was not found."
                )

            if not location.active:
                raise ValueError(
                    "The selected inventory location is inactive."
                )

            allowed_reasons = {
                "new_inventory",
                "found_inventory",
                "return",
                "transfer_correction",
                "data_entry_error",
                "other",
            }

            reason_labels = {
                "new_inventory": "New inventory",
                "found_inventory": "Found inventory",
                "return": "Customer return",
                "transfer_correction": "Transfer correction",
                "data_entry_error": "Previous data-entry error",
                "other": "Other batch adjustment",
            }

            if reason not in allowed_reasons:
                raise ValueError(
                    "Select a valid batch reason."
                )

            try:
                submitted_items = json.loads(
                    items_json
                )
            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                raise ValueError(
                    "The batch item list could not be read."
                )

            if not isinstance(submitted_items, list):
                raise ValueError(
                    "The batch item list is invalid."
                )

            if not submitted_items:
                raise ValueError(
                    "Add at least one item before saving the batch."
                )

            # Combine duplicate products before changing inventory.
            combined_items = {}

            for item in submitted_items:
                if not isinstance(item, dict):
                    raise ValueError(
                        "One of the batch lines is invalid."
                    )

                try:
                    product_id = int(
                        item.get("product_id")
                    )
                    quantity = int(
                        item.get("quantity")
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    raise ValueError(
                        "Every batch line needs a valid product "
                        "and quantity."
                    )

                if quantity < 1:
                    raise ValueError(
                        "Every batch quantity must be at least 1."
                    )

                barcode = str(
                    item.get("barcode") or ""
                ).strip()

                if product_id not in combined_items:
                    combined_items[product_id] = {
                        "quantity": 0,
                        "barcode": barcode,
                    }

                combined_items[product_id]["quantity"] += (
                    quantity
                )

                if barcode:
                    combined_items[product_id][
                        "barcode"
                    ] = barcode

            # Keep only valid scan events that correspond to inventory being
            # committed. A product can never receive more scan credits than
            # the quantity saved for that product.
            try:
                submitted_scan_events = json.loads(scan_events_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                submitted_scan_events = []
            if not isinstance(submitted_scan_events, list):
                submitted_scan_events = []
            accepted_scan_events = []
            accepted_counts = {}
            for scan_event in submitted_scan_events:
                if not isinstance(scan_event, dict):
                    continue
                try:
                    scan_product_id = int(scan_event.get("product_id"))
                except (TypeError, ValueError):
                    continue
                saved_item = combined_items.get(scan_product_id)
                if not saved_item:
                    continue
                current_count = accepted_counts.get(scan_product_id, 0)
                if current_count >= int(saved_item["quantity"]):
                    continue
                event_key = str(scan_event.get("event_key") or "").strip()
                event_barcode = "".join(
                    character
                    for character in str(scan_event.get("barcode") or "")
                    if character.isdigit()
                )
                if not event_key or not event_barcode:
                    continue
                accepted_scan_events.append({
                    "event_key": event_key,
                    "barcode": event_barcode,
                    "product_id": scan_product_id,
                })
                accepted_counts[scan_product_id] = current_count + 1

            batch_reference = (
                reference_number.strip()
                or (
                    "BATCH-ADJUST-"
                    + datetime.now().strftime(
                        "%Y%m%d-%H%M%S"
                    )
                )
            )

            clean_container_id = (
                normalize_container_id(
                    container_id
                    or sublocation
                )
            )

            if (
                location.location_id == 2
                and not clean_container_id
            ):
                raise ValueError(
                    "Back Stock inventory requires a Tote / Container ID. "
                    "Scan or enter the tote label before saving the batch."
                )

            clean_notes = notes.strip()
            saved_lines = []
            total_units = 0

            for product_id, item in combined_items.items():
                product = database.get(
                    Product,
                    product_id,
                )

                if product is None:
                    raise ValueError(
                        f"Product ID {product_id} was not found."
                    )

                quantity_added = item["quantity"]

                inventory_record = database.scalar(
                    select(Inventory).where(
                        Inventory.product_id
                        == product.product_id,
                        Inventory.location_id
                        == location.location_id,
                        Inventory.container_id
                        == clean_container_id,
                    )
                )

                if inventory_record is None:
                    inventory_record = Inventory(
                        product=product,
                        location=location,
                        container_id=clean_container_id,
                        quantity_on_hand=0,
                        quantity_reserved=0,
                        reorder_level=0,
                    )

                    database.add(inventory_record)
                    database.flush()

                previous_quantity = (
                    inventory_record.quantity_on_hand
                    or 0
                )

                new_quantity = (
                    previous_quantity
                    + quantity_added
                )

                inventory_record.quantity_on_hand = (
                    new_quantity
                )

                transaction_notes = (
                    f"Reason: {reason_labels[reason]}. "
                    f"Batch quantity added: {quantity_added}. "
                    f"Previous quantity: {previous_quantity}. "
                    f"New quantity: {new_quantity}."
                )

                if clean_container_id:
                    transaction_notes += (
                        f" Container ID: "
                        f"{clean_container_id}."
                    )

                if clean_notes:
                    transaction_notes += (
                        f" Notes: {clean_notes}"
                    )

                database.add(
                    InventoryTransaction(
                        product=product,
                        location=location,
                        container_id=clean_container_id,
                        transaction_type=(
                            "batch_adjustment_add"
                        ),
                        quantity_change=quantity_added,
                        unit_cost=product.average_cost,
                        reference_number=batch_reference,
                        notes=transaction_notes,
                    )
                )

                saved_lines.append(
                    {
                        "product_name": product.product_name,
                        "barcode": item["barcode"],
                        "quantity_added": quantity_added,
                        "previous_quantity": previous_quantity,
                        "new_quantity": new_quantity,
                    }
                )

                total_units += quantity_added

            database.commit()

            reward_result = None
            auth_user = getattr(request.state, "auth_user", None)
            if auth_user is not None and accepted_scan_events:
                try:
                    from app.kids_helper import award_committed_batch_scans
                    reward_result = award_committed_batch_scans(
                        int(auth_user.user_id),
                        accepted_scan_events,
                        pieces_processed=total_units,
                        batch_key=batch_reference,
                    )
                except Exception as reward_error:
                    # Inventory is already safely committed. A reward failure
                    # must never make the worker think the batch itself failed.
                    print(f"Committed batch reward skipped: {reward_error}")

            message = (
                f"Batch {batch_reference} saved successfully. "
                f"{len(saved_lines)} product"
                f"{'' if len(saved_lines) == 1 else 's'} "
                f"and {total_units} total unit"
                f"{'' if total_units == 1 else 's'} "
                f"were added to {location.location_name}."
            )

            if clean_container_id:
                message += (
                    f" Container ID: "
                    f"{clean_container_id}."
                )

            if reward_result and reward_result.get("events_added"):
                message += (
                    f" Saved scan rewards: {reward_result['points_awarded']} point"
                    f"{'' if reward_result['points_awarded'] == 1 else 's'} awarded; "
                    f"progress {reward_result['progress']} of "
                    f"{reward_result.get('required') or 1}."
                )

            return templates.TemplateResponse(
                request=request,
                name="batch_adjust_inventory.html",
                context={
                    "locations": locations,
                    "message": message,
                    "error": None,
                    "saved_lines": saved_lines,
                },
            )

        except ValueError as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="batch_adjust_inventory.html",
                status_code=status.HTTP_400_BAD_REQUEST,
                context={
                    "locations": locations,
                    "message": None,
                    "error": str(error),
                    "saved_lines": [],
                },
            )

        except Exception as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="batch_adjust_inventory.html",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                context={
                    "locations": locations,
                    "message": None,
                    "error": (
                        "The batch could not be saved. "
                        f"Technical details: {error}"
                    ),
                    "saved_lines": [],
                },
            )




@app.get(
    "/inventory/transfer/batch",
    response_class=HTMLResponse,
)
def batch_inventory_transfer_page(
    request: Request,
):
    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        return templates.TemplateResponse(
            request=request,
            name="batch_transfer_inventory.html",
            context={
                "locations": locations,
                "message": None,
                "error": None,
                "saved_lines": [],
            },
        )


@app.get("/inventory/transfer/batch/lookup")
def batch_inventory_transfer_lookup(
    barcode: str,
    source_location_id: int,
    source_container_id: str = "",
):
    with SessionLocal() as database:
        try:
            clean_source_container_id = (
                normalize_container_id(
                    source_container_id
                )
            )

            product, cleaned_barcode = (
                find_store_product_by_scan(
                    database=database,
                    barcode=barcode,
                )
            )

            source_location = database.get(
                InventoryLocation,
                source_location_id,
            )

            if source_location is None:
                raise ValueError(
                    "The selected source location was not found."
                )

            source_inventory = database.scalar(
                select(Inventory).where(
                    Inventory.product_id
                    == product.product_id,
                    Inventory.location_id
                    == source_location.location_id,
                )
            )

            quantity_available = (
                int(source_inventory.quantity_on_hand or 0)
                if source_inventory is not None
                else 0
            )

            return {
                "found": True,
                "product_id": product.product_id,
                "product_name": product.product_name,
                "brand": product.brand or "",
                "barcode": cleaned_barcode,
                "quantity_available": quantity_available,
                "source_location": (
                    source_location.location_name
                ),
                "source_container_id": (
                    clean_source_container_id
                ),
            }

        except ValueError as error:
            return {
                "found": False,
                "error": str(error),
            }


@app.post(
    "/inventory/transfer/batch",
    response_class=HTMLResponse,
)
def submit_batch_inventory_transfer(
    request: Request,
    source_location_id: int = Form(...),
    destination_location_id: int = Form(...),
    source_container_id: str = Form(""),
    destination_container_id: str = Form(""),
    source_sublocation: str = Form(""),
    destination_sublocation: str = Form(""),
    reference_number: str = Form(""),
    notes: str = Form(""),
    items_json: str = Form(...),
):
    with SessionLocal() as database:
        locations = database.scalars(
            select(InventoryLocation)
            .where(InventoryLocation.active.is_(True))
            .order_by(InventoryLocation.location_name)
        ).all()

        try:
            source_location = database.get(
                InventoryLocation,
                source_location_id,
            )

            destination_location = database.get(
                InventoryLocation,
                destination_location_id,
            )

            if source_location is None:
                raise ValueError(
                    "The selected source location was not found."
                )

            if destination_location is None:
                raise ValueError(
                    "The selected destination location was not found."
                )

            if not source_location.active:
                raise ValueError(
                    "The selected source location is inactive."
                )

            if not destination_location.active:
                raise ValueError(
                    "The selected destination location is inactive."
                )

            try:
                submitted_items = json.loads(
                    items_json
                )
            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                raise ValueError(
                    "The bulk transfer item list could not be read."
                )

            if not isinstance(submitted_items, list):
                raise ValueError(
                    "The bulk transfer item list is invalid."
                )

            if not submitted_items:
                raise ValueError(
                    "Add at least one item before saving "
                    "the transfer."
                )

            combined_items = {}

            for item in submitted_items:
                if not isinstance(item, dict):
                    raise ValueError(
                        "One of the transfer lines is invalid."
                    )

                try:
                    product_id = int(
                        item.get("product_id")
                    )

                    quantity = int(
                        item.get("quantity")
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    raise ValueError(
                        "Every transfer line needs a valid "
                        "product and quantity."
                    )

                if quantity < 1:
                    raise ValueError(
                        "Every transfer quantity must be "
                        "at least 1."
                    )

                barcode = str(
                    item.get("barcode") or ""
                ).strip()

                if product_id not in combined_items:
                    combined_items[product_id] = {
                        "quantity": 0,
                        "barcode": barcode,
                    }

                combined_items[product_id]["quantity"] += (
                    quantity
                )

                if barcode:
                    combined_items[product_id][
                        "barcode"
                    ] = barcode

            clean_source_container_id = (
                normalize_container_id(
                    source_container_id
                    or source_sublocation
                )
            )

            clean_destination_container_id = (
                normalize_container_id(
                    destination_container_id
                    or destination_sublocation
                )
            )

            if (
                source_location.location_id
                == destination_location.location_id
                and clean_source_container_id
                == clean_destination_container_id
            ):
                raise ValueError(
                    "Source and destination cannot be "
                    "the same location and Container ID."
                )

            clean_notes = notes.strip()

            transfer_reference = (
                reference_number.strip()
                or (
                    "BATCH-TRANSFER-"
                    + datetime.now().strftime(
                        "%Y%m%d-%H%M%S"
                    )
                )
            )

            saved_lines = []
            total_units = 0

            # Validate every line before changing inventory.
            validated_lines = []

            for product_id, item in combined_items.items():
                product = database.get(
                    Product,
                    product_id,
                )

                if product is None:
                    raise ValueError(
                        f"Product ID {product_id} "
                        "was not found."
                    )

                quantity_transferred = item["quantity"]

                source_inventory = database.scalar(
                    select(Inventory).where(
                        Inventory.product_id
                        == product.product_id,
                        Inventory.location_id
                        == source_location.location_id,
                        Inventory.container_id
                        == clean_source_container_id,
                    )
                )

                if source_inventory is None:
                    raise ValueError(
                        f"{product.product_name} has no "
                        f"inventory record at "
                        f"{source_location.location_name}."
                    )

                source_quantity = int(
                    source_inventory.quantity_on_hand or 0
                )

                if source_quantity < quantity_transferred:
                    raise ValueError(
                        f"{product.product_name}: only "
                        f"{source_quantity} unit(s) are "
                        f"available at "
                        f"{source_location.location_name}, "
                        f"but {quantity_transferred} were "
                        "requested."
                    )

                destination_inventory = database.scalar(
                    select(Inventory).where(
                        Inventory.product_id
                        == product.product_id,
                        Inventory.location_id
                        == destination_location.location_id,
                        Inventory.container_id
                        == clean_destination_container_id,
                    )
                )

                validated_lines.append(
                    {
                        "product": product,
                        "barcode": item["barcode"],
                        "quantity": quantity_transferred,
                        "source_inventory": source_inventory,
                        "destination_inventory": (
                            destination_inventory
                        ),
                    }
                )

            # Apply changes only after every line passes.
            for line in validated_lines:
                product = line["product"]
                quantity_transferred = line["quantity"]
                source_inventory = line[
                    "source_inventory"
                ]

                destination_inventory = line[
                    "destination_inventory"
                ]

                if destination_inventory is None:
                    destination_inventory = Inventory(
                        product=product,
                        location=destination_location,
                        container_id=(
                            clean_destination_container_id
                        ),
                        quantity_on_hand=0,
                        quantity_reserved=0,
                        reorder_level=0,
                    )

                    database.add(destination_inventory)
                    database.flush()

                source_before = int(
                    source_inventory.quantity_on_hand or 0
                )

                destination_before = int(
                    destination_inventory.quantity_on_hand
                    or 0
                )

                source_after = (
                    source_before
                    - quantity_transferred
                )

                destination_after = (
                    destination_before
                    + quantity_transferred
                )

                source_inventory.quantity_on_hand = (
                    source_after
                )

                destination_inventory.quantity_on_hand = (
                    destination_after
                )

                transfer_notes = (
                    f"Bulk transferred from "
                    f"{source_location.location_name} "
                    f"to "
                    f"{destination_location.location_name}. "
                    f"Quantity transferred: "
                    f"{quantity_transferred}. "
                    f"Source quantity: "
                    f"{source_before} to {source_after}. "
                    f"Destination quantity: "
                    f"{destination_before} to "
                    f"{destination_after}."
                )

                if clean_source_container_id:
                    transfer_notes += (
                        f" Source Container ID: "
                        f"{clean_source_container_id}."
                    )

                if clean_destination_container_id:
                    transfer_notes += (
                        f" Destination Container ID: "
                        f"{clean_destination_container_id}."
                    )

                if clean_notes:
                    transfer_notes += (
                        f" Notes: {clean_notes}"
                    )

                database.add(
                    InventoryTransaction(
                        product=product,
                        location=source_location,
                        container_id=(
                            clean_source_container_id
                        ),
                        transaction_type="transfer_out",
                        quantity_change=(
                            -quantity_transferred
                        ),
                        unit_cost=product.average_cost,
                        reference_number=(
                            transfer_reference
                        ),
                        notes=transfer_notes,
                    )
                )

                database.add(
                    InventoryTransaction(
                        product=product,
                        location=destination_location,
                        container_id=(
                            clean_destination_container_id
                        ),
                        transaction_type="transfer_in",
                        quantity_change=(
                            quantity_transferred
                        ),
                        unit_cost=product.average_cost,
                        reference_number=(
                            transfer_reference
                        ),
                        notes=transfer_notes,
                    )
                )

                saved_lines.append(
                    {
                        "product_name": (
                            product.product_name
                        ),
                        "barcode": line["barcode"],
                        "quantity_transferred": (
                            quantity_transferred
                        ),
                        "source_before": source_before,
                        "source_after": source_after,
                        "destination_before": (
                            destination_before
                        ),
                        "destination_after": (
                            destination_after
                        ),
                    }
                )

                total_units += quantity_transferred

            database.commit()

            message = (
                f"Bulk transfer {transfer_reference} "
                f"saved successfully. "
                f"{len(saved_lines)} product"
                f"{'' if len(saved_lines) == 1 else 's'} "
                f"and {total_units} total unit"
                f"{'' if total_units == 1 else 's'} "
                f"were moved from "
                f"{source_location.location_name} to "
                f"{destination_location.location_name}."
            )

            return templates.TemplateResponse(
                request=request,
                name="batch_transfer_inventory.html",
                context={
                    "locations": locations,
                    "message": message,
                    "error": None,
                    "saved_lines": saved_lines,
                },
            )

        except ValueError as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="batch_transfer_inventory.html",
                status_code=status.HTTP_400_BAD_REQUEST,
                context={
                    "locations": locations,
                    "message": None,
                    "error": str(error),
                    "saved_lines": [],
                },
            )

        except Exception as error:
            database.rollback()

            return templates.TemplateResponse(
                request=request,
                name="batch_transfer_inventory.html",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                context={
                    "locations": locations,
                    "message": None,
                    "error": (
                        "The bulk transfer could not be saved. "
                        f"Technical details: {error}"
                    ),
                    "saved_lines": [],
                },
            )


@app.get(
    "/inventory/replenishment",
    response_class=HTMLResponse,
)
def inventory_replenishment_report(
    request: Request,
    source_location: str = "",
    destination_location_id: Optional[int] = None,
    storefront_minimum: int = 2,
    backstock_minimum: int = 2,
    page: int = 1,
    page_size: int = 15,
    database: Session = Depends(get_database),
):
    storefront_minimum = max(1, min(int(storefront_minimum or 2), 999))
    backstock_minimum = max(1, min(int(backstock_minimum or 2), 999))

    if page_size not in {15, 25, 50, 100}:
        page_size = 15

    page = max(1, int(page or 1))

    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    storefront_location = next(
        (
            location
            for location in locations
            if location.location_name.strip().casefold()
            == "brookshouse storefront"
        ),
        None,
    )

    backroom_location = next(
        (
            location
            for location in locations
            if location.location_name.strip().casefold()
            == "store back room"
        ),
        None,
    )

    error = None

    if storefront_location is None:
        error = "The BrooksHouse Storefront inventory location could not be found."
    elif backroom_location is None:
        error = "The Store Back Room inventory location could not be found."

    excluded_terms = (
        "damage",
        "return",
        "quarantine",
        "missing",
        "shrink",
        "prob",
        "online orders",
        "reserved",
    )

    excluded_types = {
        "catalog",
        "hold",
        "reserved",
        "store",
    }

    deep_reserve_locations = []

    if storefront_location is not None and backroom_location is not None:
        for location in locations:
            if location.location_id in {
                storefront_location.location_id,
                backroom_location.location_id,
            }:
                continue

            location_name = (location.location_name or "").casefold()
            location_type = (location.location_type or "").strip().casefold()

            if location_type in excluded_types:
                continue

            if any(term in location_name for term in excluded_terms):
                continue

            deep_reserve_locations.append(location)

    deep_reserve_ids = {
        location.location_id
        for location in deep_reserve_locations
    }

    source_choices = []

    if backroom_location is not None:
        source_choices.append(
            {
                "value": str(backroom_location.location_id),
                "location_id": backroom_location.location_id,
                "label": backroom_location.location_name,
                "tier": "RESERVE",
            }
        )

    for location in deep_reserve_locations:
        source_choices.append(
            {
                "value": str(location.location_id),
                "location_id": location.location_id,
                "label": location.location_name,
                "tier": "DEEP RESERVE",
            }
        )

    if deep_reserve_locations:
        source_choices.append(
            {
                "value": "deep_all",
                "location_id": None,
                "label": "All Deep Reserve",
                "tier": "DEEP RESERVE",
            }
        )

    if not source_location and backroom_location is not None:
        source_location = str(backroom_location.location_id)

    selected_source_kind = None
    selected_source_location = None
    source_ids = set()

    if source_location == "deep_all":
        selected_source_kind = "deep"
        source_ids = set(deep_reserve_ids)
    else:
        try:
            parsed_source_id = int(source_location)
        except (TypeError, ValueError):
            parsed_source_id = None

        if (
            parsed_source_id is not None
            and backroom_location is not None
            and parsed_source_id == backroom_location.location_id
        ):
            selected_source_kind = "reserve"
            selected_source_location = backroom_location
            source_ids = {backroom_location.location_id}

        elif (
            parsed_source_id is not None
            and parsed_source_id in deep_reserve_ids
        ):
            selected_source_kind = "deep"
            selected_source_location = next(
                (
                    location
                    for location in deep_reserve_locations
                    if location.location_id == parsed_source_id
                ),
                None,
            )
            source_ids = {parsed_source_id}

    if selected_source_kind is None and not error:
        error = "Choose a valid replenishment source location."

    if (
        destination_location_id is None
        and selected_source_kind == "reserve"
        and storefront_location is not None
    ):
        destination_location_id = storefront_location.location_id

    elif (
        destination_location_id is None
        and selected_source_kind == "deep"
        and backroom_location is not None
    ):
        destination_location_id = backroom_location.location_id

    destination_choices = []

    if storefront_location is not None:
        destination_choices.append(storefront_location)

    if backroom_location is not None:
        destination_choices.append(backroom_location)

    destination_location = next(
        (
            location
            for location in destination_choices
            if location.location_id == destination_location_id
        ),
        None,
    )

    if destination_location is None and not error:
        error = "Choose a valid replenishment destination."

    inventory_records = database.scalars(
        select(Inventory)
    ).all()

    inventory_by_product = {}

    for record in inventory_records:
        inventory_by_product.setdefault(
            record.product_id,
            [],
        ).append(record)

    products = database.scalars(
        select(Product).order_by(Product.product_name)
    ).unique().all()

    rows = []

    if (
        not error
        and storefront_location is not None
        and backroom_location is not None
        and destination_location is not None
    ):
        for product in products:
            if getattr(product, "active", True) is False:
                continue

            product_inventory = inventory_by_product.get(
                product.product_id,
                [],
            )

            storefront_quantity = sum(
                max(int(record.quantity_on_hand or 0), 0)
                for record in product_inventory
                if record.location_id == storefront_location.location_id
            )

            backroom_quantity = sum(
                max(int(record.quantity_on_hand or 0), 0)
                for record in product_inventory
                if record.location_id == backroom_location.location_id
            )

            deep_reserve_quantity = sum(
                max(int(record.quantity_on_hand or 0), 0)
                for record in product_inventory
                if record.location_id in deep_reserve_ids
            )

            source_breakdown = []

            for record in product_inventory:
                if record.location_id not in source_ids:
                    continue

                quantity = max(
                    int(record.quantity_on_hand or 0),
                    0,
                )

                if quantity <= 0:
                    continue

                source_breakdown.append(
                    {
                        "location_id": record.location_id,
                        "location_name": record.location.location_name,
                        "container_id": record.container_id or "",
                        "quantity": quantity,
                    }
                )

            source_breakdown.sort(
                key=lambda item: (
                    -item["quantity"],
                    item["location_name"].casefold(),
                    item["container_id"].casefold(),
                )
            )

            source_quantity = sum(
                item["quantity"]
                for item in source_breakdown
            )

            if source_quantity <= 0:
                continue

            emergency = (
                storefront_quantity <= 0
                and backroom_quantity <= 0
                and deep_reserve_quantity > 0
            )

            flow_type = ""
            destination_quantity = 0
            quantity_needed = 0

            if (
                selected_source_kind == "reserve"
                and destination_location.location_id
                == storefront_location.location_id
            ):
                flow_type = "RESERVE_TO_PICK"
                destination_quantity = storefront_quantity
                quantity_needed = max(
                    storefront_minimum - storefront_quantity,
                    0,
                )

            elif (
                selected_source_kind == "deep"
                and destination_location.location_id
                == backroom_location.location_id
            ):
                flow_type = "DEEP_TO_RESERVE"
                destination_quantity = backroom_quantity
                quantity_needed = max(
                    backstock_minimum - backroom_quantity,
                    0,
                )

            elif (
                selected_source_kind == "deep"
                and destination_location.location_id
                == storefront_location.location_id
            ):
                flow_type = "EMERGENCY_DEEP_TO_PICK"
                destination_quantity = storefront_quantity

                if not emergency:
                    continue

                quantity_needed = max(
                    storefront_minimum - storefront_quantity,
                    1,
                )
            else:
                continue

            if quantity_needed <= 0:
                continue

            suggested_quantity = min(
                quantity_needed,
                source_quantity,
            )

            barcode = ""

            if product.barcodes:
                primary_barcode = next(
                    (
                        barcode_record.barcode
                        for barcode_record in product.barcodes
                        if getattr(barcode_record, "is_primary", False)
                    ),
                    None,
                )

                barcode = (
                    primary_barcode
                    or product.barcodes[0].barcode
                    or ""
                )

            best_source = (
                source_breakdown[0]
                if source_breakdown
                else None
            )

            transfer_source_location_id = (
                best_source["location_id"]
                if best_source
                else None
            )

            rows.append(
                {
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "brand": product.brand,
                    "barcode": barcode,
                    "flow_type": flow_type,
                    "source_quantity": source_quantity,
                    "destination_quantity": destination_quantity,
                    "storefront_quantity": storefront_quantity,
                    "backroom_quantity": backroom_quantity,
                    "deep_reserve_quantity": deep_reserve_quantity,
                    "quantity_needed": quantity_needed,
                    "suggested_quantity": suggested_quantity,
                    "source_breakdown": source_breakdown,
                    "best_source": best_source,
                    "emergency": emergency,
                    "transfer_source_location_id": transfer_source_location_id,
                    "transfer_destination_location_id": destination_location.location_id,
                }
            )

    rows.sort(
        key=lambda row: (
            0 if row["emergency"] else 1,
            row["destination_quantity"],
            row["product_name"].casefold(),
        )
    )

    total_rows = len(rows)

    total_pages = max(
        1,
        (total_rows + page_size - 1) // page_size,
    )

    page = min(page, total_pages)

    start_index = (page - 1) * page_size
    end_index = min(
        start_index + page_size,
        total_rows,
    )

    page_rows = rows[start_index:end_index]

    summary = {
        "ready_products": total_rows,
        "ready_units": sum(
            row["suggested_quantity"]
            for row in rows
        ),
        "emergency_products": sum(
            1
            for row in rows
            if row["emergency"]
        ),
    }

    pagination = {
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "previous_page": max(page - 1, 1),
        "next_page": min(page + 1, total_pages),
        "start_row": start_index + 1 if total_rows else 0,
        "end_row": end_index if total_rows else 0,
    }

    if source_location == "deep_all":
        source_label = "All Deep Reserve"
    elif selected_source_location is not None:
        source_label = selected_source_location.location_name
    else:
        source_label = ""

    destination_label = (
        destination_location.location_name
        if destination_location
        else ""
    )

    return templates.TemplateResponse(
        request=request,
        name="replenishment_report.html",
        context={
            "storefront_location": storefront_location,
            "backroom_location": backroom_location,
            "deep_reserve_locations": deep_reserve_locations,
            "source_choices": source_choices,
            "source_location": source_location,
            "selected_source_kind": selected_source_kind,
            "selected_source_location": selected_source_location,
            "source_label": source_label,
            "destination_choices": destination_choices,
            "destination_location_id": destination_location_id,
            "destination_location": destination_location,
            "destination_label": destination_label,
            "storefront_minimum": storefront_minimum,
            "backstock_minimum": backstock_minimum,
            "rows": page_rows,
            "summary": summary,
            "pagination": pagination,
            "error": error,
        },
    )



DEFAULT_PYTHON_LAB_CODE = '''import sqlite3

database_path = "app/data/brookshouse_store.db"
database_uri = f"file:{database_path}?mode=ro"

connection = sqlite3.connect(database_uri, uri=True)
connection.row_factory = sqlite3.Row

rows = connection.execute(
    """
    SELECT product_id, product_name, store_price
    FROM products
    ORDER BY product_id DESC
    LIMIT 10
    """
).fetchall()

for row in rows:
    print(dict(row))

connection.close()
'''


def python_lab_is_local_client(request: Request) -> bool:
    return (
        request.client is not None
        and request.client.host in {"127.0.0.1", "::1"}
    )


@app.get(
    "/tools/python",
    response_class=HTMLResponse,
)
def python_lab_page(request: Request):
    is_local_client = python_lab_is_local_client(request)

    return templates.TemplateResponse(
        request=request,
        name="python_lab.html",
        context={
            "python_text": DEFAULT_PYTHON_LAB_CODE,
            "stdout": "",
            "stderr": "",
            "return_code": None,
            "message": None,
            "error": (
                None
                if is_local_client
                else (
                    "Python Lab can only run on the BrooksHouse "
                    "server computer at 127.0.0.1."
                )
            ),
            "is_local_client": is_local_client,
        },
    )


@app.post(
    "/tools/python",
    response_class=HTMLResponse,
)
async def python_lab_run(
    request: Request,
    python_text: str = Form(...),
    confirmation: str = Form(""),
):
    import ast as _ast
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    cleaned_python = (python_text or "").strip()
    is_local_client = python_lab_is_local_client(request)
    stdout = ""
    stderr = ""
    return_code = None
    message = None
    error = None

    allowed_modules = {
        "collections",
        "csv",
        "datetime",
        "decimal",
        "functools",
        "itertools",
        "json",
        "math",
        "random",
        "re",
        "sqlite3",
        "statistics",
        "string",
        "textwrap",
        "time",
    }

    blocked_calls = {
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "input",
        "open",
        "__import__",
    }

    if not is_local_client:
        error = (
            "Python execution is disabled on phones, Zebra devices, "
            "and other network clients. Open this page at "
            "http://127.0.0.1:8001/tools/python on the server computer."
        )
    elif not cleaned_python:
        error = "Enter Python code to run."
    elif len(cleaned_python) > 12000:
        error = "Python Lab accepts a maximum of 12,000 characters per run."
    elif confirmation.strip().upper() != "RUN PYTHON":
        error = "Type RUN PYTHON in the confirmation box before running code."
    else:
        try:
            syntax_tree = _ast.parse(cleaned_python, mode="exec")

            for node in _ast.walk(syntax_tree):
                if isinstance(node, _ast.Import):
                    for alias in node.names:
                        top_module = alias.name.split(".", 1)[0]
                        if top_module not in allowed_modules:
                            raise ValueError(
                                f'Import "{top_module}" is not allowed in Python Lab.'
                            )

                if isinstance(node, _ast.ImportFrom):
                    top_module = (node.module or "").split(".", 1)[0]
                    if top_module not in allowed_modules:
                        raise ValueError(
                            f'Import "{top_module}" is not allowed in Python Lab.'
                        )

                if isinstance(node, _ast.Call):
                    if (
                        isinstance(node.func, _ast.Name)
                        and node.func.id in blocked_calls
                    ):
                        raise ValueError(
                            f'Function "{node.func.id}" is disabled in Python Lab.'
                        )

                if (
                    isinstance(node, _ast.Attribute)
                    and node.attr.startswith("__")
                ):
                    raise ValueError(
                        "Double-underscore attribute access is disabled in Python Lab."
                    )

            run_environment = {"PYTHONIOENCODING": "utf-8"}

            for environment_name in (
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
            ):
                environment_value = _os.environ.get(environment_name)
                if environment_value:
                    run_environment[environment_name] = environment_value

            completed = _subprocess.run(
                [_sys.executable, "-I", "-S", "-c", cleaned_python],
                cwd=str(APP_DIRECTORY.parent),
                env=run_environment,
                stdin=_subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

            stdout = (completed.stdout or "")[:100000]
            stderr = (completed.stderr or "")[:100000]
            return_code = completed.returncode

            if completed.returncode == 0:
                message = "Python completed successfully."
            else:
                error = f"Python exited with code {completed.returncode}."

        except _subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "")[:100000]
            stderr = (exc.stderr or "")[:100000]
            error = "Python stopped after the 10-second safety timeout."
        except (SyntaxError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request=request,
        name="python_lab.html",
        context={
            "python_text": cleaned_python,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "message": message,
            "error": error,
            "is_local_client": is_local_client,
        },
    )



@app.post(
    "/tools/python/saved/walmart-orders",
    response_class=HTMLResponse,
)
async def python_lab_saved_walmart_orders(
    request: Request,
    mode: str = Form("preview"),
):
    import subprocess as _subprocess
    import sys as _sys

    is_local_client = python_lab_is_local_client(request)
    stdout = ""
    stderr = ""
    return_code = None
    message = None
    error = None

    if not is_local_client:
        error = (
            "Saved Python tools can only run on the BrooksHouse "
            "server computer at 127.0.0.1."
        )
    elif mode not in {"preview", "apply"}:
        error = "Unknown Walmart saved-script mode."
    else:
        command = [
            _sys.executable,
            "walmart_open_orders_to_reserved.py",
        ]

        if mode == "apply":
            command.append("--apply")

        try:
            completed = _subprocess.run(
                command,
                cwd=str(APP_DIRECTORY.parent),
                stdin=_subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
            )

            stdout = (completed.stdout or "")[:100000]
            stderr = (completed.stderr or "")[:100000]
            return_code = completed.returncode

            if completed.returncode == 0:
                if mode == "apply":
                    message = (
                        "Walmart Open Orders -> Reserved completed "
                        "in APPLY mode."
                    )
                else:
                    message = (
                        "Walmart Open Orders -> Reserved completed "
                        "in PREVIEW mode. No inventory quantities "
                        "were changed."
                    )
            else:
                error = (
                    "Walmart saved script exited with code "
                    f"{completed.returncode}."
                )

        except _subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "")[:100000]
            stderr = (exc.stderr or "")[:100000]
            error = (
                "Walmart saved script stopped after the "
                "90-second safety timeout."
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request=request,
        name="python_lab.html",
        context={
            "python_text": DEFAULT_PYTHON_LAB_CODE,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "message": message,
            "error": error,
            "is_local_client": is_local_client,
        },
    )



def _marketplace_stats_table_exists(connection, table_name: str) -> bool:
    return connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND lower(name) = lower(?)
        LIMIT 1
        """,
        (table_name,),
    ).fetchone() is not None


def _marketplace_stats_scalar(connection, sql: str, parameters: tuple = ()) -> int:
    try:
        row = connection.execute(sql, parameters).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)
    except Exception:
        return 0


def build_marketplace_stats() -> dict:
    import sqlite3 as _sqlite3
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    connection = _sqlite3.connect("app/data/brookshouse_store.db")
    connection.row_factory = _sqlite3.Row

    def table_exists(name: str) -> bool:
        return connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND lower(name) = lower(?)
            LIMIT 1
            """,
            (name,),
        ).fetchone() is not None

    def scalar(sql: str, parameters: tuple = ()) -> int:
        try:
            row = connection.execute(sql, parameters).fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    def money(sql: str, parameters: tuple = ()) -> float:
        try:
            row = connection.execute(sql, parameters).fetchone()
            return float(row[0] or 0.0) if row else 0.0
        except Exception:
            return 0.0

    try:
        stats = {
            "active_products": scalar("SELECT COUNT(*) FROM products WHERE active = 1"),
            "total_units": scalar("SELECT COALESCE(SUM(quantity_on_hand),0) FROM inventory"),
            "reserved_units": scalar("SELECT COALESCE(SUM(quantity_on_hand),0) FROM inventory WHERE location_id = 5"),
            "reserved_products": scalar("SELECT COUNT(*) FROM inventory WHERE location_id = 5 AND COALESCE(quantity_on_hand,0) > 0"),
            "amazon_listings": 0,
            "amazon_linked": 0,
            "amazon_processed_lines": 0,
            "amazon_reserved_units": 0,
            "walmart_listings": 0,
            "walmart_linked": 0,
            "walmart_processed_lines": 0,
            "walmart_reserved_units": 0,
            "shopify_listings": 0,
            "shopify_linked": 0,
            "marketplace_linked_total": 0,
            "amazon_7d_orders": 0,
            "amazon_7d_units": 0,
            "amazon_7d_sales": 0.0,
            "amazon_30d_orders": 0,
            "amazon_30d_units": 0,
            "amazon_30d_sales": 0.0,
            "amazon_90d_orders": 0,
            "amazon_90d_units": 0,
            "amazon_90d_sales": 0.0,
            "amazon_history_orders": 0,
            "amazon_history_units": 0,
            "amazon_history_sales": 0.0,
            "amazon_status_rows": [],
            "amazon_top_products": [],
        }

        if table_exists("amazon_listings"):
            stats["amazon_listings"] = scalar("SELECT COUNT(*) FROM amazon_listings")

        if table_exists("amazon_product_links"):
            stats["amazon_linked"] = scalar(
                "SELECT COUNT(DISTINCT product_id) FROM amazon_product_links WHERE lower(COALESCE(match_status,'')) = 'linked'"
            )

        if table_exists("amazon_order_inventory_sync"):
            stats["amazon_processed_lines"] = scalar("SELECT COUNT(*) FROM amazon_order_inventory_sync")
            stats["amazon_reserved_units"] = scalar("SELECT COALESCE(SUM(quantity_added),0) FROM amazon_order_inventory_sync")

        if table_exists("walmart_listings"):
            stats["walmart_listings"] = scalar("SELECT COUNT(*) FROM walmart_listings")

        if table_exists("walmart_product_links"):
            stats["walmart_linked"] = scalar(
                "SELECT COUNT(DISTINCT product_id) FROM walmart_product_links WHERE lower(COALESCE(match_status,'')) = 'linked'"
            )

        if table_exists("walmart_order_inventory_sync"):
            stats["walmart_processed_lines"] = scalar("SELECT COUNT(*) FROM walmart_order_inventory_sync")
            stats["walmart_reserved_units"] = scalar("SELECT COALESCE(SUM(quantity_added),0) FROM walmart_order_inventory_sync")

        if table_exists("sales_channels") and table_exists("channel_listings"):
            stats["shopify_listings"] = scalar(
                """
                SELECT COUNT(*)
                FROM channel_listings cl
                JOIN sales_channels sc ON sc.channel_id = cl.channel_id
                WHERE lower(sc.channel_name) = 'shopify'
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(channel_listings)").fetchall()}
            if "product_id" in columns:
                stats["shopify_linked"] = scalar(
                    """
                    SELECT COUNT(DISTINCT cl.product_id)
                    FROM channel_listings cl
                    JOIN sales_channels sc ON sc.channel_id = cl.channel_id
                    WHERE lower(sc.channel_name) = 'shopify'
                      AND cl.product_id IS NOT NULL
                    """
                )

        stats["marketplace_linked_total"] = (
            stats["amazon_linked"]
            + stats["walmart_linked"]
            + stats["shopify_linked"]
        )

        if table_exists("amazon_order_history"):
            now = _datetime.now()

            for days, prefix in ((7, "amazon_7d"), (30, "amazon_30d"), (90, "amazon_90d")):
                cutoff = (now - _timedelta(days=days)).isoformat(timespec="seconds")

                stats[prefix + "_orders"] = scalar(
                    "SELECT COUNT(*) FROM amazon_order_history WHERE created_time >= ? AND COALESCE(fulfillment_status,'') != 'CANCELLED'",
                    (cutoff,),
                )
                stats[prefix + "_units"] = scalar(
                    "SELECT COALESCE(SUM(unit_count),0) FROM amazon_order_history WHERE created_time >= ? AND COALESCE(fulfillment_status,'') != 'CANCELLED'",
                    (cutoff,),
                )
                stats[prefix + "_sales"] = money(
                    "SELECT COALESCE(SUM(order_total),0) FROM amazon_order_history WHERE created_time >= ? AND COALESCE(fulfillment_status,'') != 'CANCELLED'",
                    (cutoff,),
                )

            stats["amazon_history_orders"] = scalar(
                "SELECT COUNT(*) FROM amazon_order_history WHERE COALESCE(fulfillment_status,'') != 'CANCELLED'"
            )
            stats["amazon_history_units"] = scalar(
                "SELECT COALESCE(SUM(unit_count),0) FROM amazon_order_history WHERE COALESCE(fulfillment_status,'') != 'CANCELLED'"
            )
            stats["amazon_history_sales"] = money(
                "SELECT COALESCE(SUM(order_total),0) FROM amazon_order_history WHERE COALESCE(fulfillment_status,'') != 'CANCELLED'"
            )

            rows = connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(fulfillment_status),''),'(blank)') AS status,
                    COUNT(*) AS orders,
                    COALESCE(SUM(unit_count),0) AS units,
                    COALESCE(SUM(order_total),0) AS sales
                FROM amazon_order_history
                GROUP BY COALESCE(NULLIF(TRIM(fulfillment_status),''),'(blank)')
                ORDER BY orders DESC, status
                """
            ).fetchall()

            stats["amazon_status_rows"] = [
                {
                    "status": row["status"],
                    "orders": int(row["orders"] or 0),
                    "units": int(row["units"] or 0),
                    "sales": float(row["sales"] or 0.0),
                }
                for row in rows
            ]

        if table_exists("amazon_order_item_history"):
            rows = connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(title),''),NULLIF(TRIM(seller_sku),''),NULLIF(TRIM(asin),''),'Unknown product') AS title,
                    seller_sku,
                    COALESCE(SUM(quantity_ordered),0) AS units,
                    COUNT(*) AS lines,
                    COALESCE(SUM(item_total),0) AS sales
                FROM amazon_order_item_history AS item
                JOIN amazon_order_history AS order_header
                  ON order_header.amazon_order_id = item.amazon_order_id
                WHERE UPPER(COALESCE(order_header.fulfillment_status,'')) <> 'CANCELLED'
                GROUP BY
                    COALESCE(NULLIF(TRIM(item.title),''),NULLIF(TRIM(item.seller_sku),''),NULLIF(TRIM(item.asin),''),'Unknown product'),
                    item.seller_sku
                ORDER BY units DESC, lines DESC
                LIMIT 15
                """
            ).fetchall()

            stats["amazon_top_products"] = [
                {
                    "title": row["title"],
                    "seller_sku": row["seller_sku"],
                    "units": int(row["units"] or 0),
                    "lines": int(row["lines"] or 0),
                    "sales": float(row["sales"] or 0.0),
                }
                for row in rows
            ]

        return stats

    finally:
        connection.close()


@app.get(
    "/channels/stats",
    response_class=HTMLResponse,
)
def marketplace_stats_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="marketplace_stats.html",
        context={
            "stats": build_marketplace_stats(),
        },
    )


@app.post(
    "/tools/python/saved/amazon-orders",
    response_class=HTMLResponse,
)
async def python_lab_saved_amazon_orders(
    request: Request,
    mode: str = Form("preview"),
):
    import subprocess as _subprocess
    import sys as _sys

    is_local_client = python_lab_is_local_client(request)
    stdout = ""
    stderr = ""
    return_code = None
    message = None
    error = None

    if not is_local_client:
        error = (
            "Saved Python tools can only run on the BrooksHouse "
            "server computer at 127.0.0.1."
        )
    elif mode not in {"preview", "apply"}:
        error = "Unknown Amazon saved-script mode."
    else:
        command = [
            _sys.executable,
            "amazon_open_orders_to_reserved.py",
        ]

        if mode == "apply":
            command.append("--apply")

        try:
            completed = _subprocess.run(
                command,
                cwd=str(APP_DIRECTORY.parent),
                stdin=_subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )

            stdout = (completed.stdout or "")[:100000]
            stderr = (completed.stderr or "")[:100000]
            return_code = completed.returncode

            if completed.returncode == 0:
                message = (
                    "Amazon Open Orders -> Reserved completed in APPLY mode."
                    if mode == "apply"
                    else (
                        "Amazon Open Orders -> Reserved completed in PREVIEW mode. "
                        "No inventory quantities were changed."
                    )
                )
            else:
                error = (
                    "Amazon saved script exited with code "
                    f"{completed.returncode}."
                )

        except _subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "")[:100000]
            stderr = (exc.stderr or "")[:100000]
            error = "Amazon saved script stopped after the 120-second safety timeout."
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request=request,
        name="python_lab.html",
        context={
            "python_text": DEFAULT_PYTHON_LAB_CODE,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "message": message,
            "error": error,
            "is_local_client": is_local_client,
        },
    )


@app.post(
    "/tools/python/saved/amazon-history",
    response_class=HTMLResponse,
)
async def python_lab_saved_amazon_history(
    request: Request,
    days: int = Form(365),
):
    import subprocess as _subprocess
    import sys as _sys

    is_local_client = python_lab_is_local_client(request)
    stdout = ""
    stderr = ""
    return_code = None
    message = None
    error = None

    if not is_local_client:
        error = (
            "Saved Python tools can only run on the BrooksHouse "
            "server computer at 127.0.0.1."
        )
    elif days not in {30, 90, 365, 730}:
        error = "Unsupported Amazon history lookback."
    else:
        command = [
            _sys.executable,
            "amazon_order_history_sync.py",
            "--days",
            str(days),
        ]

        try:
            completed = _subprocess.run(
                command,
                cwd=str(APP_DIRECTORY.parent),
                stdin=_subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )

            stdout = (completed.stdout or "")[:100000]
            stderr = (completed.stderr or "")[:100000]
            return_code = completed.returncode

            if completed.returncode == 0:
                message = (
                    f"Amazon order history sync completed "
                    f"for the last {days} days."
                )
            else:
                error = (
                    "Amazon history script exited with code "
                    f"{completed.returncode}."
                )

        except _subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "")[:100000]
            stderr = (exc.stderr or "")[:100000]
            error = (
                "Amazon history sync reached the five-minute browser timeout. "
                "Already committed pages remain saved; rerun if needed."
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request=request,
        name="python_lab.html",
        context={
            "python_text": DEFAULT_PYTHON_LAB_CODE,
            "stdout": stdout,
            "stderr": stderr,
            "return_code": return_code,
            "message": message,
            "error": error,
            "is_local_client": is_local_client,
        },
    )


@app.get(
    "/tools/sql",
    response_class=HTMLResponse,
)
def sql_console_page(
    request: Request,
):
    return templates.TemplateResponse(
        request=request,
        name="sql_console.html",
        context={
            "sql_text": (
                "SELECT name, type\n"
                "FROM sqlite_master\n"
                "WHERE type IN ('table', 'view')\n"
                "ORDER BY type, name;"
            ),
            "result_sets": [],
            "message": None,
            "error": None,
            "write_mode": False,
            "backup_path": None,
            "is_local_client": (
                request.client is not None
                and request.client.host
                in {"127.0.0.1", "::1"}
            ),
        },
    )


@app.post(
    "/tools/sql",
    response_class=HTMLResponse,
)
async def sql_console_run(
    request: Request,
    sql_text: str = Form(...),
    allow_changes: str = Form(""),
    confirmation: str = Form(""),
):
    import sqlite3 as _sqlite3
    from datetime import datetime as _datetime
    from pathlib import Path as _Path

    cleaned_sql = (sql_text or "").strip()
    write_mode = allow_changes == "yes"

    is_local_client = (
        request.client is not None
        and request.client.host
        in {"127.0.0.1", "::1"}
    )

    result_sets = []
    message = None
    error = None
    backup_path = None

    def split_sql_statements(script_text):
        statements = []
        buffer = ""

        for line in script_text.splitlines(True):
            buffer += line

            if _sqlite3.complete_statement(buffer):
                statement = buffer.strip()

                if statement:
                    statements.append(statement)

                buffer = ""

        if buffer.strip():
            statements.append(buffer.strip())

        return statements

    if not cleaned_sql:
        error = "Enter a SQL query or script."

    else:
        statements = split_sql_statements(
            cleaned_sql
        )

        if not statements:
            error = "No executable SQL statement was found."

        elif len(statements) > 25:
            error = (
                "For safety, the SQL Console runs "
                "a maximum of 25 statements at once."
            )

        elif write_mode and not is_local_client:
            error = (
                "Database-changing SQL is only allowed "
                "from the BrooksHouse server computer."
            )

        elif (
            write_mode
            and confirmation.strip().upper()
            != "RUN WRITE SQL"
        ):
            error = (
                'Type RUN WRITE SQL in the confirmation '
                "box before running database changes."
            )

        else:
            db_file = _Path(DB_PATH).resolve()

            try:
                if write_mode:
                    backup_directory = (
                        db_file.parent.parent.parent
                        / "backups"
                        / "sql_console"
                    )

                    backup_directory.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    stamp = _datetime.now().strftime(
                        "%Y%m%d-%H%M%S"
                    )

                    backup_file = (
                        backup_directory
                        / (
                            "brookshouse_store-before-sql-"
                            f"{stamp}.db"
                        )
                    )

                    source_connection = _sqlite3.connect(
                        str(db_file),
                        timeout=30,
                    )

                    try:
                        destination_connection = (
                            _sqlite3.connect(
                                str(backup_file)
                            )
                        )

                        try:
                            source_connection.backup(
                                destination_connection
                            )
                        finally:
                            destination_connection.close()

                    finally:
                        source_connection.close()

                    backup_path = str(
                        backup_file
                    )

                    connection = _sqlite3.connect(
                        str(db_file),
                        timeout=30,
                    )

                else:
                    db_uri = (
                        "file:"
                        + db_file.as_posix()
                        + "?mode=ro"
                    )

                    connection = _sqlite3.connect(
                        db_uri,
                        uri=True,
                        timeout=30,
                    )

                connection.row_factory = (
                    _sqlite3.Row
                )

                connection.execute(
                    "PRAGMA busy_timeout = 30000"
                )

                try:
                    cursor = connection.cursor()
                    total_affected = 0

                    for statement_number, statement in enumerate(
                        statements,
                        start=1,
                    ):
                        before_changes = (
                            connection.total_changes
                        )

                        cursor.execute(
                            statement
                        )

                        changed = (
                            connection.total_changes
                            - before_changes
                        )

                        total_affected += changed

                        if cursor.description:
                            columns = [
                                description[0]
                                for description
                                in cursor.description
                            ]

                            fetched_rows = (
                                cursor.fetchmany(501)
                            )

                            truncated = (
                                len(fetched_rows) > 500
                            )

                            fetched_rows = (
                                fetched_rows[:500]
                            )

                            result_sets.append(
                                {
                                    "statement_number": (
                                        statement_number
                                    ),
                                    "statement": (
                                        statement
                                    ),
                                    "columns": columns,
                                    "rows": [
                                        [
                                            row[column]
                                            for column
                                            in columns
                                        ]
                                        for row
                                        in fetched_rows
                                    ],
                                    "row_count": len(
                                        fetched_rows
                                    ),
                                    "truncated": (
                                        truncated
                                    ),
                                    "affected": (
                                        changed
                                    ),
                                }
                            )

                        else:
                            result_sets.append(
                                {
                                    "statement_number": (
                                        statement_number
                                    ),
                                    "statement": (
                                        statement
                                    ),
                                    "columns": [],
                                    "rows": [],
                                    "row_count": 0,
                                    "truncated": False,
                                    "affected": (
                                        changed
                                    ),
                                }
                            )

                    if write_mode:
                        connection.commit()

                        message = (
                            f"SQL script completed. "
                            f"{len(statements)} statement(s); "
                            f"{total_affected} row change(s). "
                            "A database backup was created first."
                        )

                    else:
                        message = (
                            f"Read-only SQL completed. "
                            f"{len(statements)} statement(s)."
                        )

                except Exception:
                    if write_mode:
                        connection.rollback()

                    raise

                finally:
                    connection.close()

            except Exception as exc:
                error = (
                    f"{type(exc).__name__}: {exc}"
                )

    return templates.TemplateResponse(
        request=request,
        name="sql_console.html",
        context={
            "sql_text": cleaned_sql,
            "result_sets": result_sets,
            "message": message,
            "error": error,
            "write_mode": write_mode,
            "backup_path": backup_path,
            "is_local_client": is_local_client,
        },
    )


@app.get(
    "/dashboard",
    response_class=HTMLResponse,
)
def inventory_dashboard(
    request: Request,
    database: Session = Depends(get_database),
):
    low_stock_limit = 10

    products = database.scalars(
        select(Product)
        .where(Product.active.is_(True))
        .order_by(Product.product_name)
    ).unique().all()

    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    inventory_records = database.scalars(
        select(Inventory)
    ).all()

    total_products = len(products)

    total_units = sum(
        record.quantity_on_hand or 0
        for record in inventory_records
    )

    inventory_value = Decimal("0")

    for record in inventory_records:
        quantity = Decimal(record.quantity_on_hand or 0)
        average_cost = record.product.average_cost or Decimal("0")
        inventory_value += quantity * average_cost

    location_totals = []

    for location in locations:
        location_records = [
            record
            for record in inventory_records
            if record.location_id == location.location_id
        ]

        location_units = sum(
            record.quantity_on_hand or 0
            for record in location_records
        )
        location_value = Decimal("0")
        product_ids = set()

        for record in location_records:
            quantity = record.quantity_on_hand or 0

            if quantity > 0:
                product_ids.add(record.product_id)

            average_cost = record.product.average_cost or Decimal("0")
            location_value += Decimal(quantity) * average_cost

        location_totals.append(
            {
                "location_id": location.location_id,
                "location_name": location.location_name,
                "location_type": location.location_type,
                "product_count": len(product_ids),
                "total_units": location_units,
                "total_value": location_value,
            }
        )

    location_totals.sort(
        key=lambda row: row["total_units"],
        reverse=True,
    )

    storefront_location = next(
        (
            location
            for location in locations
            if (location.location_name or "").strip().casefold()
            == "brookshouse storefront"
        ),
        None,
    )

    if storefront_location is None:
        storefront_location = next(
            (
                location
                for location in locations
                if (location.location_type or "").strip().casefold()
                == "store"
            ),
            None,
        )

    storefront_records = []

    if storefront_location is not None:
        storefront_records = [
            record
            for record in inventory_records
            if record.location_id == storefront_location.location_id
        ]

    storefront_records_by_product = {}

    for record in storefront_records:
        storefront_records_by_product.setdefault(
            record.product_id,
            [],
        ).append(record)

    low_stock_products = []
    out_of_stock_products = []

    for product in products:
        product_storefront_records = storefront_records_by_product.get(
            product.product_id
        )

        # A product must already have a Storefront inventory record to
        # appear in either alert. Catalog-only items are ignored.
        if not product_storefront_records:
            continue

        storefront_quantity = sum(
            record.quantity_on_hand or 0
            for record in product_storefront_records
        )

        primary_barcode = None

        if product.barcodes:
            primary_barcode = product.barcodes[0].barcode

        alert_row = {
            "product_id": product.product_id,
            "product_name": product.product_name,
            "barcode": primary_barcode,
            "total_quantity": storefront_quantity,
        }

        if storefront_quantity <= 0:
            out_of_stock_products.append(alert_row)
        elif storefront_quantity <= low_stock_limit:
            low_stock_products.append(alert_row)

    low_stock_products.sort(
        key=lambda row: (
            row["total_quantity"],
            (row["product_name"] or "").lower(),
        )
    )
    out_of_stock_products.sort(
        key=lambda row: (row["product_name"] or "").lower()
    )

    recent_transactions = database.scalars(
        select(InventoryTransaction)
        .order_by(InventoryTransaction.transaction_id.desc())
        .limit(25)
    ).unique().all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total_products": total_products,
            "total_units": total_units,
            "inventory_value": inventory_value,
            "active_location_count": len(locations),
            "location_totals": location_totals,
            "low_stock_products": low_stock_products,
            "out_of_stock_products": out_of_stock_products,
            "recent_transactions": recent_transactions,
            "low_stock_limit": low_stock_limit,
        },
    )


@app.get(
    "/inventory/search",
    response_class=HTMLResponse,
)
def inventory_search_page(
    request: Request,
    search: str = "",
    barcode: str = "",
    location_id: str = "",
    container_id: str = "",
    stock_status: str = "all",
    marketplace_status: str = "all",
    page: int = 1,
    page_size: int = 25,
    sort_by: str = "product_asc",
    minimum_quantity: str = "",
    maximum_quantity: str = "",
    minimum_value: str = "",
    store_price: str = "",
    run_report: str = "",
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    # Inventory Search opens empty until Run Report is clicked.
    if not run_report:
        filters = {
            "search": search,
            "barcode": barcode,
            "location_id": location_id,
            "container_id": container_id,
            "stock_status": stock_status,
            "marketplace_status": marketplace_status,
            "page_size": page_size,
            "sort_by": sort_by,
            "minimum_quantity": minimum_quantity,
            "maximum_quantity": maximum_quantity,
            "minimum_value": minimum_value,
            "store_price": store_price,
        }

        summary = {
            "product_count": 0,
            "total_units": 0,
            "total_value": 0,
        }

        return templates.TemplateResponse(
            request=request,
            name="inventory_search.html",
            context={
                "locations": locations,
                "rows": [],
                "filters": filters,
                "summary": summary,
                "report_has_run": False,
            },
        )


    try:
        parsed_location_id = (
            int(location_id)
            if location_id.strip()
            and int(location_id) > 0
            else None
        )

        parsed_minimum_quantity = (
            int(minimum_quantity)
            if minimum_quantity.strip()
            else None
        )

        parsed_maximum_quantity = (
            int(maximum_quantity)
            if maximum_quantity.strip()
            else None
        )

        parsed_minimum_value = (
            Decimal(minimum_value)
            if minimum_value.strip()
            else None
        )

        parsed_store_price = (
            Decimal(store_price)
            if store_price.strip()
            else None
        )

    except (ValueError, TypeError):
        parsed_location_id = None
        parsed_minimum_quantity = None
        parsed_maximum_quantity = None
        parsed_minimum_value = None
        parsed_store_price = None

    # --------------------------------------------------
    # Build Inventory Search at the DATABASE level.
    # This prevents records from other totes/locations
    # from ever reaching the report.
    # --------------------------------------------------

    normalized_search = clean_search_term(search)
    normalized_barcode = barcode.strip()

    requested_container = normalize_container_id(container_id)

    # Allow a tote label scanned into the Barcode box.
    barcode_upper = normalized_barcode.upper()

    known_container_scan = False
    if barcode_upper:
        known_container_scan = database.scalar(
            select(Inventory.inventory_id)
            .where(
                func.upper(Inventory.container_id)
                == normalize_container_id(barcode_upper)
            )
            .limit(1)
        ) is not None

    if (
        barcode_upper.startswith("BR-TOTE-")
        or barcode_upper.startswith("TOTE-")
        or barcode_upper.startswith("BR-CONTAINER-")
        or barcode_upper.startswith("CONTAINER-")
        or barcode_upper.startswith("BR-PALLET-")
        or barcode_upper.startswith("PALLET-")
        or known_container_scan
    ):
        requested_container = normalize_container_id(barcode_upper)
        container_id = requested_container

        normalized_barcode = ""
        barcode = ""

    # A pallet/tote/container defines the complete inventory scope.
    # Clear every unrelated filter before the database statement is built;
    # otherwise a stale Location selection can incorrectly hide the tote.
    if requested_container:
        parsed_location_id = None
        location_id = ""
        normalized_search = ""
        normalized_barcode = ""
        barcode = ""
        stock_status = "all"
        marketplace_status = "all"
        parsed_minimum_quantity = None
        parsed_maximum_quantity = None
        parsed_minimum_value = None
        parsed_store_price = None

    inventory_statement = select(Inventory)

    # Hard location filter.
    if parsed_location_id is not None:
        inventory_statement = (
            inventory_statement.where(
                Inventory.location_id
                == parsed_location_id
            )
        )

    # Hard tote/container filter.
    if requested_container:
        inventory_statement = inventory_statement.where(
            Inventory.container_id.ilike(
                sql_wildcard_pattern(requested_container),
                escape="\\",
            )
        )

    # Apply product text search directly in SQL before report enrichment.
    # This keeps name, brand, and long-description matches from being lost
    # during the later Walmart/Amazon report-building steps.
    if normalized_search:
        product_search_pattern = sql_wildcard_pattern(normalized_search)
        inventory_statement = (
            inventory_statement
            .join(Product, Inventory.product_id == Product.product_id)
            .where(
                or_(
                    Product.product_name.ilike(
                        product_search_pattern,
                        escape="\\",
                    ),
                    Product.brand.ilike(
                        product_search_pattern,
                        escape="\\",
                    ),
                    Product.description.ilike(
                        product_search_pattern,
                        escape="\\",
                    ),
                )

            )
        )

    inventory_records = database.scalars(
        inventory_statement
        .order_by(Inventory.inventory_id)
    ).all()

    # Keep this available for the existing code below.
    normalized_container_id = requested_container

    # When a tote/container is selected, the tote itself
    # becomes the authoritative search scope. Do not allow
    # unrelated report filters to remove valid tote contents.
    if normalized_container_id:
        normalized_search = ""
        normalized_barcode = ""
        stock_status = "all"
        marketplace_status = "all"

        parsed_minimum_quantity = None
        parsed_maximum_quantity = None
        parsed_minimum_value = None
        parsed_store_price = None

    walmart_barcode_values = [
        barcode_record.barcode
        for inventory_record in inventory_records
        for barcode_record in inventory_record.product.barcodes
        if barcode_record.barcode
    ]

    walmart_flags = load_walmart_inventory_flags(
        walmart_barcode_values
    )

    from app.services.product_channel_status import get_product_channel_status

    amazon_status_by_product_id = {}

    rows = []

    enrichment_image_by_barcode = {}
    enrichment_connection = sqlite3.connect(DB_PATH, timeout=30)

    try:
        enrichment_rows = enrichment_connection.execute(
            """
            SELECT barcode, image_url
            FROM product_enrichment
            WHERE image_url IS NOT NULL
              AND TRIM(image_url) != ''
            """
        ).fetchall()

        for enrichment_barcode, enrichment_image in enrichment_rows:
            exact_barcode = str(enrichment_barcode or "").strip()
            if not exact_barcode:
                continue

            enrichment_image_by_barcode[exact_barcode] = enrichment_image
            enrichment_image_by_barcode[
                exact_barcode.lstrip("0") or "0"
            ] = enrichment_image
    finally:
        enrichment_connection.close()

    for record in inventory_records:
        product = record.product
        location = record.location

        if parsed_location_id is not None:
            if record.location_id != parsed_location_id:
                continue

        if normalized_container_id:
            record_container_id = normalize_container_id(
                record.container_id
            )

            if not wildcard_match(
                record_container_id,
                normalized_container_id,
            ):
                continue

        primary_barcode = None

        if product.barcodes:
            primary_barcode = (
                product.barcodes[0].barcode
            )

        if normalized_barcode:
            barcode_match = False

            for barcode_record in product.barcodes:
                stored_barcode = (
                    barcode_record.barcode or ""
                )

                if stored_barcode == normalized_barcode:
                    barcode_match = True
                    break

                if (
                    stored_barcode.lstrip("0")
                    == normalized_barcode.lstrip("0")
                ):
                    barcode_match = True
                    break

            if not barcode_match:
                continue

        quantity_on_hand = (
            record.quantity_on_hand or 0
        )

        if (
            parsed_location_id is not None
            and quantity_on_hand <= 0
        ):
            continue

        quantity_reserved = (
            record.quantity_reserved or 0
        )

        quantity_available = max(
            quantity_on_hand - quantity_reserved,
            0,
        )

        selling_price = (
            product.store_price
            or Decimal("0")
        )

        estimated_sales_value = (
            Decimal(quantity_on_hand)
            * selling_price
        )

        if (
            parsed_store_price is not None
            and selling_price != parsed_store_price
        ):
            continue

        estimated_acquisition_value = (
            estimated_sales_value
            * Decimal("0.25")
        )

        estimated_gross_spread = (
            estimated_sales_value
            - estimated_acquisition_value
        )



        if stock_status == "in_stock":
            if quantity_on_hand <= 0:
                continue

        elif stock_status == "out_of_stock":
            if quantity_on_hand != 0:
                continue

        elif stock_status == "low_stock":
            if not 1 <= quantity_on_hand <= 10:
                continue

        elif stock_status == "over_10":
            if quantity_on_hand <= 10:
                continue

        if parsed_minimum_quantity is not None:
            if quantity_on_hand < parsed_minimum_quantity:
                continue

        if parsed_maximum_quantity is not None:
            if quantity_on_hand > parsed_maximum_quantity:
                continue

        if parsed_minimum_value is not None:
            if estimated_sales_value < parsed_minimum_value:
                continue


        primary_image_url = None

        for image_record in product.images:
            for field_name in (
                "image_url",
                "url",
                "source_url",
                "external_url",
            ):
                image_value = getattr(
                    image_record,
                    field_name,
                    None,
                )

                if image_value:
                    primary_image_url = str(
                        image_value
                    )

                    break

            if primary_image_url:
                break

        if not primary_image_url and primary_barcode:
            primary_barcode_text = str(primary_barcode).strip()
            primary_image_url = (
                enrichment_image_by_barcode.get(primary_barcode_text)
                or enrichment_image_by_barcode.get(
                    primary_barcode_text.lstrip("0") or "0"
                )
            )

        walmart = summarize_walmart_inventory_flag(
            product.barcodes,
            walmart_flags,
        )

        if product.product_id not in amazon_status_by_product_id:
            channel_status = get_product_channel_status(
                product.product_id
            )
            amazon_status_by_product_id[
                product.product_id
            ] = channel_status.get("amazon", {})

        amazon = amazon_status_by_product_id[
            product.product_id
        ]

        walmart_related = walmart.get("state") in {
            "seller_published",
            "seller_unpublished",
            "catalog_match",
        }
        amazon_related = bool(amazon.get("linked"))

        if marketplace_status == "walmart" and not walmart_related:
            continue
        elif marketplace_status == "amazon" and not amazon_related:
            continue
        elif marketplace_status == "either" and not (
            walmart_related or amazon_related
        ):
            continue
        elif marketplace_status == "both" and not (
            walmart_related and amazon_related
        ):
            continue
        elif marketplace_status == "neither" and (
            walmart_related or amazon_related
        ):
            continue

        if (
            not primary_image_url
            and walmart.get("image_url")
        ):
            primary_image_url = walmart["image_url"]

        rows.append(
            {
                "product_id": product.product_id,
                "product_name": product.product_name,
                "image_url": primary_image_url,
                "barcode": primary_barcode,
                "location_id": location.location_id,
                "location_name": location.location_name,
                "container_id": record.container_id or "",
                "quantity_on_hand": quantity_on_hand,
                "quantity_reserved": quantity_reserved,
                "quantity_available": quantity_available,
                "selling_price": selling_price,
                "is_price_review": selling_price == Decimal("0.99"),
                "estimated_sales_value": (
                    estimated_sales_value
                ),
                "estimated_acquisition_value": (
                    estimated_acquisition_value
                ),
                "estimated_gross_spread": (
                    estimated_gross_spread
                ),
                "walmart": walmart,
                "amazon": amazon,
            }
        )

    allowed_sort_values = {
        "product_asc",
        "product_desc",
        "quantity_desc",
        "quantity_asc",
        "value_desc",
        "value_asc",
        "location_asc",
        "container_asc",
    }
    if sort_by not in allowed_sort_values:
        sort_by = "product_asc"

    if sort_by == "product_desc":
        rows.sort(
            key=lambda row: (row["product_name"] or "").casefold(),
            reverse=True,
        )
    elif sort_by == "quantity_desc":
        rows.sort(key=lambda row: row["quantity_on_hand"], reverse=True)
    elif sort_by == "quantity_asc":
        rows.sort(key=lambda row: row["quantity_on_hand"])
    elif sort_by == "value_desc":
        rows.sort(key=lambda row: row["estimated_sales_value"], reverse=True)
    elif sort_by == "value_asc":
        rows.sort(key=lambda row: row["estimated_sales_value"])
    elif sort_by == "location_asc":
        rows.sort(
            key=lambda row: (
                (row["location_name"] or "").casefold(),
                (row["product_name"] or "").casefold(),
            )
        )
    elif sort_by == "container_asc":
        rows.sort(
            key=lambda row: (
                not bool(row["container_id"]),
                (row["container_id"] or "").casefold(),
                (row["product_name"] or "").casefold(),
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                (row["product_name"] or "").casefold(),
                (row["location_name"] or "").casefold(),
            )
        )

    total_units = sum(
        row["quantity_on_hand"]
        for row in rows
    )

    total_value = sum(
        (
            row["estimated_sales_value"]
            for row in rows
        ),
        Decimal("0"),
    )

    total_estimated_acquisition = sum(
        (
            row["estimated_acquisition_value"]
            for row in rows
        ),
        Decimal("0"),
    )

    total_estimated_gross_spread = sum(
        (
            row["estimated_gross_spread"]
            for row in rows
        ),
        Decimal("0"),
    )

    unique_product_ids = {
        row["product_id"]
        for row in rows
    }

    allowed_page_sizes = {25, 50, 100, 250}

    if page_size not in allowed_page_sizes:
        page_size = 25

    if page < 1:
        page = 1

    total_matching_rows = len(rows)

    total_pages = max(
        1,
        (total_matching_rows + page_size - 1)
        // page_size,
    )

    if page > total_pages:
        page = total_pages

    page_start = (page - 1) * page_size
    page_end = page_start + page_size

    rows = rows[page_start:page_end]

    pagination = {
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "total_pages": total_pages,
        "total_rows": total_matching_rows,
        "start_row": (
            page_start + 1
            if total_matching_rows
            else 0
        ),
        "end_row": min(
            page_end,
            total_matching_rows,
        ),
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "previous_page": max(1, page - 1),
        "next_page": min(total_pages, page + 1),
    }

    filters = {
        "search": search,
        "barcode": barcode,
        "location_id": parsed_location_id,
        "container_id": container_id,
        "stock_status": stock_status,
        "marketplace_status": marketplace_status,
        "minimum_quantity": minimum_quantity,
        "maximum_quantity": maximum_quantity,
        "minimum_value": minimum_value,
        "store_price": store_price,
    }

    summary = {
        "product_count": len(
            unique_product_ids
        ),
        "total_units": total_units,
        "total_value": total_value,
        "estimated_acquisition_value": (
            total_estimated_acquisition
        ),
        "estimated_gross_spread": (
            total_estimated_gross_spread
        ),
        "estimated_acquisition_percent": 25,
    }

    return templates.TemplateResponse(
        request=request,
        name="inventory_search.html",
        context={
            "locations": locations,
            "rows": rows,
            "filters": filters,
            "summary": summary,
                "report_has_run": True,
            "pagination": pagination,
        },
    )



@app.get("/inventory/search/export")
def export_inventory_search_csv(
    search: str = "",
    barcode: str = "",
    location_id: str = "",
    container_id: str = "",
    stock_status: str = "all",
    sort_by: str = "product_asc",
    minimum_quantity: str = "",
    maximum_quantity: str = "",
    minimum_value: str = "",
    store_price: str = "",
    database: Session = Depends(get_database),
):
    try:
        parsed_location_id = (
            int(location_id)
            if location_id.strip()
            and int(location_id) > 0
            else None
        )

        parsed_minimum_quantity = (
            int(minimum_quantity)
            if minimum_quantity.strip()
            else None
        )

        parsed_maximum_quantity = (
            int(maximum_quantity)
            if maximum_quantity.strip()
            else None
        )

        parsed_minimum_value = (
            Decimal(minimum_value)
            if minimum_value.strip()
            else None
        )

        parsed_store_price = (
            Decimal(store_price)
            if store_price.strip()
            else None
        )

    except (ValueError, TypeError):
        parsed_location_id = None
        parsed_minimum_quantity = None
        parsed_maximum_quantity = None
        parsed_minimum_value = None
        parsed_store_price = None

    normalized_search = clean_search_term(search)
    normalized_barcode = barcode.strip()
    normalized_container_id = normalize_container_id(container_id)

    if normalized_container_id:
        parsed_location_id = None
        normalized_search = ""
        normalized_barcode = ""
        stock_status = "all"
        parsed_minimum_quantity = None
        parsed_maximum_quantity = None
        parsed_minimum_value = None
        parsed_store_price = None

    inventory_records = database.scalars(
        select(Inventory)
    ).all()

    export_rows = []

    for record in inventory_records:
        product = record.product
        location = record.location

        if parsed_location_id is not None:
            if record.location_id != parsed_location_id:
                continue

        if normalized_container_id:
            if not wildcard_match(
                normalize_container_id(record.container_id),
                normalized_container_id,
            ):
                continue

        primary_barcode = None

        if product.barcodes:
            primary_barcode = (
                product.barcodes[0].barcode
            )

        if normalized_search:
            if not wildcard_matches_any(
                (
                    product.product_name,
                    product.brand,
                    product.description,
                ),
                normalized_search,
            ):
                continue

        if normalized_barcode:
            barcode_match = False

            for barcode_record in product.barcodes:
                stored_barcode = (
                    barcode_record.barcode or ""
                )

                if stored_barcode == normalized_barcode:
                    barcode_match = True
                    break

                if (
                    stored_barcode.lstrip("0")
                    == normalized_barcode.lstrip("0")
                ):
                    barcode_match = True
                    break

            if not barcode_match:
                continue

        quantity_on_hand = (
            record.quantity_on_hand or 0
        )

        quantity_reserved = (
            record.quantity_reserved or 0
        )

        quantity_available = max(
            quantity_on_hand - quantity_reserved,
            0,
        )

        average_cost = (
            product.average_cost
            or Decimal("0")
        )

        inventory_value = (
            Decimal(quantity_on_hand)
            * average_cost
        )

        selling_price = (
            product.store_price
            or Decimal("0")
        )

        estimated_sales_value = (
            Decimal(quantity_on_hand)
            * selling_price
        )

        if (
            parsed_store_price is not None
            and selling_price != parsed_store_price
        ):
            continue

        if stock_status == "in_stock":
            if quantity_on_hand <= 0:
                continue

        elif stock_status == "out_of_stock":
            if quantity_on_hand != 0:
                continue

        elif stock_status == "low_stock":
            if not 1 <= quantity_on_hand <= 10:
                continue

        elif stock_status == "over_10":
            if quantity_on_hand <= 10:
                continue

        if parsed_minimum_quantity is not None:
            if quantity_on_hand < parsed_minimum_quantity:
                continue

        if parsed_maximum_quantity is not None:
            if quantity_on_hand > parsed_maximum_quantity:
                continue

        if parsed_minimum_value is not None:
            if estimated_sales_value < parsed_minimum_value:
                continue

        export_rows.append(
            {
                "product_name": product.product_name,
                "barcode": primary_barcode or "",
                "location_name": location.location_name,
                "container_id": record.container_id or "",
                "quantity_on_hand": quantity_on_hand,
                "quantity_reserved": quantity_reserved,
                "quantity_available": quantity_available,
                "average_cost": average_cost,
                "inventory_value": inventory_value,
                "selling_price": selling_price,
                "estimated_sales_value": estimated_sales_value,
            }
        )

    if sort_by == "product_desc":
        export_rows.sort(key=lambda row: (row["product_name"] or "").casefold(), reverse=True)
    elif sort_by == "quantity_desc":
        export_rows.sort(key=lambda row: row["quantity_on_hand"], reverse=True)
    elif sort_by == "quantity_asc":
        export_rows.sort(key=lambda row: row["quantity_on_hand"])
    elif sort_by == "value_desc":
        export_rows.sort(key=lambda row: row["estimated_sales_value"], reverse=True)
    elif sort_by == "value_asc":
        export_rows.sort(key=lambda row: row["estimated_sales_value"])
    elif sort_by == "location_asc":
        export_rows.sort(key=lambda row: ((row["location_name"] or "").casefold(), (row["product_name"] or "").casefold()))
    elif sort_by == "container_asc":
        export_rows.sort(key=lambda row: (not bool(row["container_id"]), (row["container_id"] or "").casefold(), (row["product_name"] or "").casefold()))
    else:
        sort_by = "product_asc"
        export_rows.sort(key=lambda row: ((row["product_name"] or "").casefold(), (row["location_name"] or "").casefold()))

    output = StringIO()

    writer = csv.writer(output)

    writer.writerow(
        [
            "Product",
            "Barcode",
            "Location",
            "Pallet / Container ID",
            "Quantity On Hand",
            "Quantity Reserved",
            "Quantity Available",
            "Average Cost",
            "Estimated Inventory Value",
            "Store Price",
            "Estimated Sales Value",
            "Price Review Needed",
        ]
    )

    for row in export_rows:
        writer.writerow(
            [
                row["product_name"],
                row["barcode"],
                row["location_name"],
                row["container_id"],
                row["quantity_on_hand"],
                row["quantity_reserved"],
                row["quantity_available"],
                f"{row['average_cost']:.2f}",
                f"{row['inventory_value']:.2f}",
                f"{row['selling_price']:.2f}",
                f"{row['estimated_sales_value']:.2f}",
                "YES" if row["selling_price"] == Decimal("0.99") else "",
            ]
        )

    output.seek(0)

    filename = "brookshouse_inventory_report.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        },
    )



@app.get(
    "/channels/shopify/reconcile",
    response_class=HTMLResponse,
)
def shopify_inventory_reconciliation(
    request: Request,
    search: str = "",
    reconciliation_status: str = "all",
    database: Session = Depends(get_database),
):
    channel = database.scalar(
        select(SalesChannel).where(
            SalesChannel.channel_name == "Shopify"
        )
    )

    status_labels = {
        "match": "Match",
        "local_higher": "Local Higher",
        "shopify_higher": "Shopify Higher",
        "not_in_shopify": "Not in Shopify",
        "not_in_brookshouse": "Not in BrooksHouse",
        "duplicate_shopify_barcode": (
            "Duplicate Shopify Barcode"
        ),
        "missing_barcode": "Missing Barcode",
    }

    status_options = list(
        status_labels.items()
    )

    filters = {
        "search": search,
        "reconciliation_status": reconciliation_status,
    }

    if channel is None:
        return templates.TemplateResponse(
            request=request,
            name="shopify_reconciliation.html",
            context={
                "rows": [],
                "summary": {
                    "compared": 0,
                    "matches": 0,
                    "mismatches": 0,
                    "unmatched": 0,
                },
                "filters": filters,
                "status_options": status_options,
                "error": (
                    "The Shopify sales channel was not found. "
                    "Run the Shopify import first."
                ),
            },
        )

    products = database.scalars(
        select(Product).order_by(
            Product.product_name
        )
    ).unique().all()

    shopify_inventory_location_ids = (
        load_shopify_inventory_location_ids()
    )

    shopify_listings = database.scalars(
        select(ChannelListing)
        .where(
            ChannelListing.channel_id
            == channel.channel_id
        )
        .order_by(
            ChannelListing.listing_title,
            ChannelListing.listing_id,
        )
    ).all()

    shopify_by_exact = {}
    shopify_by_lookup = {}

    for listing in shopify_listings:
        exact = (
            listing.barcode_exact or ""
        ).strip()

        lookup = (
            listing.barcode_lookup or ""
        ).strip()

        if exact:
            shopify_by_exact.setdefault(
                exact,
                [],
            ).append(listing)

        if lookup:
            shopify_by_lookup.setdefault(
                lookup,
                [],
            ).append(listing)

    matched_shopify_ids = set()
    rows = []

    for product in products:
        local_quantity = sum(
            record.quantity_on_hand or 0
            for record in product.inventory_records
            if record.location_id
            in shopify_inventory_location_ids
        )

        product_barcodes = [
            barcode_record.barcode.strip()
            for barcode_record in product.barcodes
            if barcode_record.barcode
            and barcode_record.barcode.strip()
        ]

        if not product_barcodes:
            rows.append(
                {
                    "status": "missing_barcode",
                    "status_label": (
                        status_labels["missing_barcode"]
                    ),
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "barcode": None,
                    "local_quantity": local_quantity,
                    "shopify_quantity": None,
                    "difference": None,
                    "shopify_status": None,
                    "shopify_price": None,
                    "details": (
                        "The BrooksHouse product does not have "
                        "a barcode available for matching."
                    ),
                }
            )

            continue

        matched_listings = []
        matched_barcode = product_barcodes[0]

        for product_barcode in product_barcodes:
            exact_matches = shopify_by_exact.get(
                product_barcode,
                [],
            )

            if exact_matches:
                matched_listings = exact_matches
                matched_barcode = product_barcode
                break

            lookup_barcode = (
                product_barcode.lstrip("0")
                or "0"
            )

            lookup_matches = shopify_by_lookup.get(
                lookup_barcode,
                [],
            )

            if lookup_matches:
                matched_listings = lookup_matches
                matched_barcode = product_barcode
                break

            if len(product_barcode) > 1:
                barcode_without_check_digit = (
                    product_barcode[:-1]
                )

                check_digit_matches = (
                    shopify_by_exact.get(
                        barcode_without_check_digit,
                        [],
                    )
                )

                if check_digit_matches:
                    matched_listings = (
                        check_digit_matches
                    )

                    matched_barcode = (
                        product_barcode
                    )

                    break

                lookup_without_check_digit = (
                    barcode_without_check_digit
                    .lstrip("0")
                    or "0"
                )

                check_digit_lookup_matches = (
                    shopify_by_lookup.get(
                        lookup_without_check_digit,
                        [],
                    )
                )

                if check_digit_lookup_matches:
                    matched_listings = (
                        check_digit_lookup_matches
                    )

                    matched_barcode = (
                        product_barcode
                    )

                    break

        if not matched_listings:
            rows.append(
                {
                    "status": "not_in_shopify",
                    "status_label": (
                        status_labels["not_in_shopify"]
                    ),
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "barcode": matched_barcode,
                    "local_quantity": local_quantity,
                    "shopify_quantity": None,
                    "difference": None,
                    "shopify_status": None,
                    "shopify_price": None,
                    "details": (
                        "No Shopify listing matched this "
                        "BrooksHouse barcode."
                    ),
                }
            )

            continue

        if len(matched_listings) > 1:
            for listing in matched_listings:
                matched_shopify_ids.add(
                    listing.listing_id
                )

            combined_quantity = sum(
                listing.quantity_available or 0
                for listing in matched_listings
            )

            rows.append(
                {
                    "status": (
                        "duplicate_shopify_barcode"
                    ),
                    "status_label": status_labels[
                        "duplicate_shopify_barcode"
                    ],
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "barcode": matched_barcode,
                    "local_quantity": local_quantity,
                    "shopify_quantity": (
                        combined_quantity
                    ),
                    "difference": (
                        local_quantity
                        - combined_quantity
                    ),
                    "shopify_status": "MULTIPLE",
                    "shopify_price": None,
                    "details": (
                        f"{len(matched_listings)} Shopify "
                        "variants share this barcode."
                    ),
                }
            )

            continue

        listing = matched_listings[0]

        matched_shopify_ids.add(
            listing.listing_id
        )

        shopify_quantity = (
            listing.quantity_available or 0
        )

        difference = (
            local_quantity - shopify_quantity
        )

        if difference == 0:
            row_status = "match"

        elif difference > 0:
            row_status = "local_higher"

        else:
            row_status = "shopify_higher"

        rows.append(
            {
                "status": row_status,
                "status_label": (
                    status_labels[row_status]
                ),
                "product_id": product.product_id,
                "product_name": product.product_name,
                "barcode": matched_barcode,
                "local_quantity": local_quantity,
                "shopify_quantity": shopify_quantity,
                "difference": difference,
                "shopify_status": (
                    listing.listing_status
                ),
                "shopify_price": (
                    listing.listed_price
                ),
                "details": None,
            }
        )

    for listing in shopify_listings:
        if listing.listing_id in matched_shopify_ids:
            continue

        listing_barcode = (
            listing.barcode_exact
            or listing.barcode_raw
            or ""
        ).strip()

        if not listing_barcode:
            row_status = "missing_barcode"

            details = (
                "This Shopify listing does not have "
                "a usable barcode."
            )

        else:
            row_status = "not_in_brookshouse"

            details = (
                "This Shopify listing was not matched "
                "to a BrooksHouse product."
            )

        rows.append(
            {
                "status": row_status,
                "status_label": (
                    status_labels[row_status]
                ),
                "product_id": None,
                "product_name": (
                    listing.listing_title
                    or "Untitled Shopify listing"
                ),
                "barcode": (
                    listing_barcode or None
                ),
                "local_quantity": None,
                "shopify_quantity": (
                    listing.quantity_available or 0
                ),
                "difference": None,
                "shopify_status": (
                    listing.listing_status
                ),
                "shopify_price": (
                    listing.listed_price
                ),
                "details": details,
            }
        )

    normalized_search = clean_search_term(search)

    if normalized_search:
        rows = [
            row
            for row in rows
            if wildcard_matches_any(
                (row["product_name"], row["barcode"]),
                normalized_search,
            )
        ]

    if (
        reconciliation_status
        and reconciliation_status != "all"
    ):
        rows = [
            row
            for row in rows
            if row["status"]
            == reconciliation_status
        ]

    status_priority = {
        "duplicate_shopify_barcode": 1,
        "missing_barcode": 2,
        "local_higher": 3,
        "shopify_higher": 4,
        "not_in_shopify": 5,
        "not_in_brookshouse": 6,
        "match": 7,
    }

    rows.sort(
        key=lambda row: (
            status_priority.get(
                row["status"],
                99,
            ),
            row["product_name"].lower(),
        )
    )

    compared_rows = [
        row
        for row in rows
        if row["status"] in {
            "match",
            "local_higher",
            "shopify_higher",
        }
    ]

    summary = {
        "compared": len(compared_rows),
        "matches": sum(
            1
            for row in rows
            if row["status"] == "match"
        ),
        "mismatches": sum(
            1
            for row in rows
            if row["status"] in {
                "local_higher",
                "shopify_higher",
                "duplicate_shopify_barcode",
            }
        ),
        "unmatched": sum(
            1
            for row in rows
            if row["status"] in {
                "not_in_shopify",
                "not_in_brookshouse",
                "missing_barcode",
            }
        ),
    }

    return templates.TemplateResponse(
        request=request,
        name="shopify_reconciliation.html",
        context={
            "rows": rows,
            "summary": summary,
            "filters": filters,
            "status_options": status_options,
            "error": None,
        },
    )



SHOPIFY_INVENTORY_SETTINGS_PATH = (
    Path(__file__).resolve().parent
    / "config"
    / "shopify_inventory_locations.json"
)


def load_shopify_inventory_location_ids() -> set[int]:
    if not SHOPIFY_INVENTORY_SETTINGS_PATH.exists():
        return set()

    try:
        data = json.loads(
            SHOPIFY_INVENTORY_SETTINGS_PATH.read_text(
                encoding="utf-8"
            )
        )

        return {
            int(location_id)
            for location_id in data.get(
                "location_ids",
                [],
            )
        }

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return set()


def save_shopify_inventory_location_ids(
    location_ids: list[int],
) -> None:
    SHOPIFY_INVENTORY_SETTINGS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    cleaned_ids = sorted(
        {
            int(location_id)
            for location_id in location_ids
        }
    )

    data = {
        "location_ids": cleaned_ids,
    }

    SHOPIFY_INVENTORY_SETTINGS_PATH.write_text(
        json.dumps(
            data,
            indent=2,
        ),
        encoding="utf-8",
    )


@app.get(
    "/channels/shopify/settings",
    response_class=HTMLResponse,
)
def shopify_inventory_settings_page(
    request: Request,
    saved: str = "",
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    selected_location_ids = (
        load_shopify_inventory_location_ids()
    )

    return templates.TemplateResponse(
        request=request,
        name="shopify_inventory_settings.html",
        context={
            "locations": locations,
            "selected_location_ids": selected_location_ids,
            "message": (
                "Shopify inventory locations saved successfully."
                if saved == "1"
                else None
            ),
            "error": None,
        },
    )


@app.post(
    "/channels/shopify/settings",
)
async def save_shopify_inventory_settings(
    request: Request,
    database: Session = Depends(get_database),
):
    form_data = await request.form()

    raw_location_ids = form_data.getlist(
        "location_ids"
    )

    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    valid_location_ids = {
        location.location_id
        for location in locations
    }

    try:
        selected_location_ids = [
            int(location_id)
            for location_id in raw_location_ids
            if int(location_id)
            in valid_location_ids
        ]

        if not selected_location_ids:
            raise ValueError(
                "Select at least one location for Shopify inventory."
            )

        save_shopify_inventory_location_ids(
            selected_location_ids
        )

        return RedirectResponse(
            url=(
                "/channels/shopify/settings"
                "?saved=1"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    except ValueError as error:
        return templates.TemplateResponse(
            request=request,
            name="shopify_inventory_settings.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            context={
                "locations": locations,
                "selected_location_ids": set(),
                "message": None,
                "error": str(error),
            },
        )



@app.get(
    "/channels/shopify/push-preview",
    response_class=HTMLResponse,
)
def shopify_push_preview_page(
    request: Request,
    saved: str = "",
    pushed: str = "",
    database: Session = Depends(get_database),
):
    rows = build_shopify_push_preview(
        database
    )

    push_settings = (
        load_shopify_push_settings()
    )

    ready_count = sum(
        1
        for row in rows
        if row["status"] == "ready"
    )

    stale_count = sum(
        1
        for row in rows
        if row["status"] == "stale"
    )

    blocked_count = (
        len(rows)
        - ready_count
        - stale_count
    )

    summary = {
        "total": len(rows),
        "ready": ready_count,
        "stale": stale_count,
        "blocked": blocked_count,
    }

    return templates.TemplateResponse(
        request=request,
        name="shopify_push_preview.html",
        context={
            "rows": rows,
            "summary": summary,
            "push_settings": push_settings,
            "message": (
                "Shopify destination saved."
                if saved == "1"
                else (
                    f"{pushed} approved inventory "
                    "record(s) were pushed to Shopify."
                    if pushed.isdigit()
                    and int(pushed) > 0
                    else None
                )
            ),
            "error": None,
        },
    )


@app.post(
    "/channels/shopify/push-preview/settings",
)
async def save_shopify_push_destination(
    request: Request,
    database: Session = Depends(get_database),
):
    form_data = await request.form()

    shopify_location_id = str(
        form_data.get(
            "shopify_location_id",
            "",
        )
    ).strip()

    shopify_location_name = str(
        form_data.get(
            "shopify_location_name",
            "",
        )
    ).strip()

    rows = build_shopify_push_preview(
        database
    )

    ready_count = sum(
        1
        for row in rows
        if row["status"] == "ready"
    )

    stale_count = sum(
        1
        for row in rows
        if row["status"] == "stale"
    )

    summary = {
        "total": len(rows),
        "ready": ready_count,
        "stale": stale_count,
        "blocked": (
            len(rows)
            - ready_count
            - stale_count
        ),
    }

    if not shopify_location_name:
        return templates.TemplateResponse(
            request=request,
            name="shopify_push_preview.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            context={
                "rows": rows,
                "summary": summary,
                "push_settings": {
                    "shopify_location_id": (
                        shopify_location_id
                    ),
                    "shopify_location_name": "",
                },
                "message": None,
                "error": (
                    "Enter a name for the Shopify "
                    "inventory location."
                ),
            },
        )

    if not shopify_location_id.startswith(
        "gid://shopify/Location/"
    ):
        return templates.TemplateResponse(
            request=request,
            name="shopify_push_preview.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            context={
                "rows": rows,
                "summary": summary,
                "push_settings": {
                    "shopify_location_id": (
                        shopify_location_id
                    ),
                    "shopify_location_name": (
                        shopify_location_name
                    ),
                },
                "message": None,
                "error": (
                    "Enter a valid Shopify GraphQL "
                    "location ID beginning with "
                    "gid://shopify/Location/."
                ),
            },
        )

    save_shopify_push_settings(
        shopify_location_id=(
            shopify_location_id
        ),
        shopify_location_name=(
            shopify_location_name
        ),
    )

    return RedirectResponse(
        url=(
            "/channels/shopify/push-preview"
            "?saved=1"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )



@app.post(
    "/channels/shopify/push-preview/execute",
)
def execute_shopify_inventory_push(
    request: Request,
    database: Session = Depends(get_database),
):
    try:
        result = push_approved_shopify_inventory(
            database
        )

    except Exception as error:
        database.rollback()

        rows = build_shopify_push_preview(
            database
        )

        ready_count = sum(
            1
            for row in rows
            if row["status"] == "ready"
        )

        stale_count = sum(
            1
            for row in rows
            if row["status"] == "stale"
        )

        summary = {
            "total": len(rows),
            "ready": ready_count,
            "stale": stale_count,
            "blocked": (
                len(rows)
                - ready_count
                - stale_count
            ),
        }

        return templates.TemplateResponse(
            request=request,
            name="shopify_push_preview.html",
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            context={
                "rows": rows,
                "summary": summary,
                "push_settings": (
                    load_shopify_push_settings()
                ),
                "message": None,
                "error": str(error),
            },
        )

    return RedirectResponse(
        url=(
            "/channels/shopify/push-preview"
            f"?pushed={result['pushed_count']}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/channels/shopify/approve",
    response_class=HTMLResponse,
)
def shopify_approval_queue(
    request: Request,
    saved: str = "",
    cleared: str = "",
    database: Session = Depends(get_database),
):
    candidates = build_approval_candidates(
        database
    )

    approved_count = sum(
        1
        for row in candidates
        if row["approved"]
        and not row["stale"]
    )

    stale_count = sum(
        1
        for row in candidates
        if row["stale"]
    )

    summary = {
        "total": len(candidates),
        "approved": approved_count,
        "pending": (
            len(candidates)
            - approved_count
            - stale_count
        ),
        "stale": stale_count,
    }

    message = None

    if saved:
        message = (
            f"{saved} Shopify inventory "
            "approval(s) saved."
        )

    elif cleared == "1":
        message = (
            "All saved Shopify approvals "
            "were cleared."
        )

    return templates.TemplateResponse(
        request=request,
        name="shopify_approval_queue.html",
        context={
            "candidates": candidates,
            "summary": summary,
            "message": message,
            "error": None,
        },
    )


@app.post(
    "/channels/shopify/approve",
)
async def save_shopify_approvals(
    request: Request,
    database: Session = Depends(get_database),
):
    form_data = await request.form()

    selected_keys = {
        str(key)
        for key in form_data.getlist(
            "approval_keys"
        )
    }

    candidates = build_approval_candidates(
        database
    )

    saved_count = save_selected_approvals(
        candidates=candidates,
        selected_keys=selected_keys,
    )

    return RedirectResponse(
        url=(
            "/channels/shopify/approve"
            f"?saved={saved_count}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/channels/shopify/approve/clear",
)
def clear_shopify_approvals():
    clear_approvals()

    return RedirectResponse(
        url=(
            "/channels/shopify/approve"
            "?cleared=1"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )



@app.get(
    "/channels/shopify/storefront-import",
    response_class=HTMLResponse,
)
def shopify_storefront_import_preview(
    request: Request,
    database: Session = Depends(get_database),
):
    preview = build_storefront_import_preview(
        database
    )

    error = None

    if not preview["channel_found"]:
        error = (
            "The Shopify channel was not found. "
            "Run the Shopify listing import first."
        )

    return templates.TemplateResponse(
        request=request,
        name="shopify_storefront_import.html",
        context={
            "summary": preview["summary"],
            "safe_rows": preview["safe_rows"],
            "review_rows": preview["review_rows"],
            "error": error,
        },
    )



@app.get(
    "/channels/amazon/mapping",
    response_class=HTMLResponse,
)
def amazon_mapping_page(
    request: Request,
    q: str = "",
    status: str = "unmatched",
    message: str | None = None,
    error: str | None = None,
):
    data = get_mapping_page_data(
        search_term=q,
        mapping_filter=status,
    )

    return templates.TemplateResponse(
        request=request,
        name="amazon_mapping.html",
        context={
            **data,
            "message": message,
            "error": error,
        },
    )


@app.post("/channels/amazon/link")
def amazon_mapping_link(
    amazon_listing_id: int = Form(...),
    product_id: int = Form(...),
):
    try:
        link_amazon_listing(
            amazon_listing_id=amazon_listing_id,
            product_id=product_id,
        )

        return RedirectResponse(
            url=(
                "/channels/amazon/mapping"
                "?status=unmatched"
                "&message=Amazon listing linked successfully."
            ),
            status_code=303,
        )

    except Exception as error:
        return RedirectResponse(
            url=(
                "/channels/amazon/mapping"
                "?status=unmatched"
                "&error="
                + str(error).replace(" ", "%20")
            ),
            status_code=303,
        )


@app.post("/channels/amazon/unlink")
def amazon_mapping_unlink(
    amazon_listing_id: int = Form(...),
):
    try:
        unlink_amazon_listing(
            amazon_listing_id
        )

        return RedirectResponse(
            url=(
                "/channels/amazon/mapping"
                "?status=linked"
                "&message=Amazon listing unlinked."
            ),
            status_code=303,
        )

    except Exception as error:
        return RedirectResponse(
            url=(
                "/channels/amazon/mapping"
                "?status=linked"
                "&error="
                + str(error).replace(" ", "%20")
            ),
            status_code=303,
        )




@app.get("/api/dashboard/location-values")
def dashboard_location_values():
    return build_location_financial_summary()


@app.get("/api/dashboard/financial-summary")
def dashboard_financial_summary():
    return build_financial_summary()



@app.get("/api/products/{product_id}/channel-status")
def product_channel_status_api(
    product_id: int,
):
    return get_product_channel_status(
        product_id
    )


@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "database": "connected",
    }


@app.get("/api/locations")
def list_locations(
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation).order_by(
            InventoryLocation.location_name
        )
    ).all()

    return [
        {
            "location_id": location.location_id,
            "location_name": location.location_name,
            "location_type": location.location_type,
            "description": location.description,
            "active": location.active,
        }
        for location in locations
    ]


@app.get("/api/products")
def list_products(
    database: Session = Depends(get_database),
):
    products = database.scalars(
        select(Product).order_by(Product.product_name)
    ).all()

    return [
        {
            "product_id": product.product_id,
            "product_name": product.product_name,
            "brand": product.brand,
            "category": product.category,
            "store_price": (
                float(product.store_price)
                if product.store_price is not None
                else None
            ),
            "active": product.active,
        }
        for product in products
    ]


@app.get("/api/products/{product_id}")
def get_product(
    product_id: int,
    database: Session = Depends(get_database),
):
    product = database.get(Product, product_id)

    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )

    return product_to_dictionary(product)


@app.post(
    "/api/products",
    status_code=status.HTTP_201_CREATED,
)
def create_product(
    product_data: ProductCreate,
    database: Session = Depends(get_database),
):
    try:
        product = save_product_record(
            database=database,
            product_data=product_data,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    except IntegrityError as error:
        database.rollback()

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The product could not be saved.",
        ) from error

    return {
        "message": "Product created successfully.",
        "product": product_to_dictionary(product),
    }
@app.get("/inventory")
def inventory_home():
    return RedirectResponse(
        url="/inventory/search",
        status_code=status.HTTP_302_FOUND,
    )

@app.get("/api/barcodes/{barcode}")
def find_product_by_barcode(
    barcode: str,
    database: Session = Depends(get_database),
):
    cleaned_barcode = barcode.strip().replace(" ", "")

    barcode_record = database.scalar(
        select(ProductBarcode).where(
            ProductBarcode.barcode == cleaned_barcode
        )
    )

    if barcode_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "Barcode not found.",
                "barcode": cleaned_barcode,
                "action": "add_new_product",
            },
        )

    return {
        "found": True,
        "barcode": barcode_record.barcode,
        "barcode_type": barcode_record.barcode_type,
        "quantity_per_scan": barcode_record.quantity_per_scan,
        "product": product_to_dictionary(barcode_record.product),
    }









# ============================================================
# STORAGE LOAD MAP + PHOTO / VIDEO GALLERY


def storage_gallery_ensure_settings_table():
    connection = sqlite3.connect(DB_PATH, timeout=30)

    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS storage_gallery_settings (
                location_id INTEGER PRIMARY KEY,
                display_name TEXT,
                slot_prefix TEXT,
                cover_image_path TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def storage_gallery_default_prefix(location):
    import re

    name = str(location.location_name or "").strip()
    folded = name.casefold()
    trailer_match = re.search(r"trailer\s*(\d+)", folded)

    if trailer_match:
        return f"T{trailer_match.group(1)}"

    if "storage container" in folded:
        return "SC"

    if "container" in folded:
        container_match = re.search(r"container\s*(\d+)", folded)
        if container_match:
            return f"C{container_match.group(1)}"
        return "SC"

    return f"L{location.location_id}"


def storage_gallery_get_settings(location):
    storage_gallery_ensure_settings_table()

    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row

    try:
        row = connection.execute(
            """
            SELECT location_id, display_name, slot_prefix, cover_image_path
            FROM storage_gallery_settings
            WHERE location_id = ?
            """,
            (location.location_id,),
        ).fetchone()

        if row is None:
            default_prefix = storage_gallery_default_prefix(location)
            connection.execute(
                """
                INSERT INTO storage_gallery_settings (
                    location_id,
                    display_name,
                    slot_prefix,
                    cover_image_path
                )
                VALUES (?, ?, ?, NULL)
                """,
                (
                    location.location_id,
                    location.location_name,
                    default_prefix,
                ),
            )
            connection.commit()

            return {
                "location_id": location.location_id,
                "display_name": location.location_name,
                "slot_prefix": default_prefix,
                "cover_image_path": None,
            }

        return {
            "location_id": row["location_id"],
            "display_name": row["display_name"] or location.location_name,
            "slot_prefix": row["slot_prefix"] or storage_gallery_default_prefix(location),
            "cover_image_path": row["cover_image_path"],
        }
    finally:
        connection.close()


def storage_gallery_update_settings(
    location,
    *,
    display_name=None,
    cover_image_path=None,
    update_cover=False,
):
    settings = storage_gallery_get_settings(location)

    new_display_name = settings["display_name"] if display_name is None else display_name
    new_cover_image_path = (
        settings["cover_image_path"] if not update_cover else cover_image_path
    )

    connection = sqlite3.connect(DB_PATH, timeout=30)

    try:
        connection.execute(
            """
            UPDATE storage_gallery_settings
            SET display_name = ?,
                cover_image_path = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE location_id = ?
            """,
            (
                new_display_name,
                new_cover_image_path,
                location.location_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

# ============================================================

def storage_gallery_slot_prefix(location):
    settings = storage_gallery_get_settings(location)
    return settings["slot_prefix"]


def storage_gallery_media_kind(path_value):
    suffix = Path(str(path_value or "")).suffix.casefold()

    if suffix in {
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
    }:
        return "video"

    return "photo"


def storage_gallery_pallet_products(
    database,
    location,
    slot_code,
):
    inventory_records = database.scalars(
        select(Inventory).where(
            Inventory.location_id
            == location.location_id
        )
    ).all()

    rows = []

    wanted_container = normalize_container_id(
        slot_code
    )

    for record in inventory_records:
        quantity = int(
            record.quantity_on_hand or 0
        )

        if quantity <= 0:
            continue

        record_container = normalize_container_id(
            record.container_id
        )

        if record_container != wanted_container:
            continue

        product = record.product

        if product is None:
            continue

        primary_barcode = None

        if product.barcodes:
            primary_barcode = str(
                product.barcodes[0].barcode or ""
            ).strip() or None

        rows.append(
            {
                "inventory_id": record.inventory_id,
                "product_id": product.product_id,
                "product_name": product.product_name,
                "brand": product.brand,
                "barcode": primary_barcode,
                "quantity_on_hand": quantity,
                "average_cost": (
                    float(product.average_cost)
                    if product.average_cost is not None
                    else None
                ),
                "store_price": (
                    float(product.store_price)
                    if product.store_price is not None
                    else None
                ),
            }
        )

    rows.sort(
        key=lambda row: (
            str(
                row["product_name"] or ""
            ).casefold(),
            row["product_id"],
        )
    )

    return rows


def storage_gallery_add_inventory_summary(
    database,
    location,
    slots,
):
    inventory_records = database.scalars(
        select(Inventory).where(
            Inventory.location_id
            == location.location_id
        )
    ).all()

    summary_by_container = {}

    for record in inventory_records:
        quantity = int(
            record.quantity_on_hand or 0
        )

        if quantity <= 0:
            continue

        container_id = normalize_container_id(
            record.container_id
        )

        if not container_id:
            continue

        key = container_id.casefold()

        summary = summary_by_container.setdefault(
            key,
            {
                "total_units": 0,
                "product_ids": set(),
            },
        )

        summary["total_units"] += quantity

        if record.product_id is not None:
            summary["product_ids"].add(
                record.product_id
            )

    for slot in slots:
        summary = summary_by_container.get(
            slot["slot_code"].casefold(),
            None,
        )

        if summary is None:
            slot["inventory_units"] = 0
            slot["inventory_product_count"] = 0
            slot["has_inventory"] = False
            continue

        slot["inventory_units"] = (
            summary["total_units"]
        )

        slot["inventory_product_count"] = len(
            summary["product_ids"]
        )

        slot["has_inventory"] = (
            summary["total_units"] > 0
        )

    return slots


def storage_gallery_build_slots(location, media_records):
    prefix = storage_gallery_slot_prefix(location)

    media_by_container = {}

    for media in media_records:
        container = str(media.container_id or "").strip()

        if not container:
            continue

        media_by_container.setdefault(
            container.casefold(),
            [],
        ).append(media)

    slots = []

    for position in range(1, 33):
        slot_code = f"{prefix}-P{position:02d}"
        slot_media = media_by_container.get(
            slot_code.casefold(),
            [],
        )

        photos = [
            media
            for media in slot_media
            if storage_gallery_media_kind(
                media.image_path
            )
            == "photo"
        ]

        videos = [
            media
            for media in slot_media
            if storage_gallery_media_kind(
                media.image_path
            )
            == "video"
        ]

        cover_photo = photos[0] if photos else None

        slots.append(
            {
                "slot_code": slot_code,
                "position": position,
                "row_number": ((position - 1) // 2) + 1,
                "column_number": (
                    "Left"
                    if position % 2 == 1
                    else "Right"
                ),
                "media": slot_media,
                "photos": photos,
                "videos": videos,
                "photo_count": len(photos),
                "video_count": len(videos),
                "media_count": len(slot_media),
                "cover_photo": cover_photo,
            }
        )

    return slots


@app.get(
    "/storage-gallery",
    response_class=HTMLResponse,
)
def storage_gallery_page(
    request: Request,
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto

    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    storage_locations = [
        location
        for location in locations
        if (
            "trailer" in location.location_name.casefold()
            or "container" in location.location_name.casefold()
        )
    ]

    albums = []

    for location in storage_locations:
        media_records = database.scalars(
            select(StoragePhoto)
            .where(
                StoragePhoto.location_id
                == location.location_id
            )
            .order_by(StoragePhoto.created_at.desc())
        ).all()

        photo_count = sum(
            1
            for media in media_records
            if storage_gallery_media_kind(
                media.image_path
            )
            == "photo"
        )

        video_count = sum(
            1
            for media in media_records
            if storage_gallery_media_kind(
                media.image_path
            )
            == "video"
        )

        cover_photo = next(
            (
                media
                for media in media_records
                if storage_gallery_media_kind(
                    media.image_path
                )
                == "photo"
            ),
            None,
        )

        albums.append(
            {
                "location": location,
                "display_name": storage_gallery_get_settings(location)["display_name"],
                "cover_image_path": storage_gallery_get_settings(location)["cover_image_path"],
                "photo_count": photo_count,
                "video_count": video_count,
                "media_count": len(media_records),
                "cover_photo": cover_photo,
                "slot_prefix": storage_gallery_slot_prefix(
                    location
                ),
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="storage_gallery.html",
        context={
            "gallery_mode": "albums",
            "albums": albums,
            "location": None,
            "slots": [],
            "slot": None,
            "media_records": [],
        },
    )


@app.get(
    "/storage-gallery/{location_id}",
    response_class=HTMLResponse,
)
def storage_gallery_location_page(
    request: Request,
    location_id: int,
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto

    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    media_records = database.scalars(
        select(StoragePhoto)
        .where(
            StoragePhoto.location_id == location_id
        )
        .order_by(StoragePhoto.created_at.desc())
    ).all()

    general_media_records = [
        media
        for media in media_records
        if normalize_container_id(
            media.container_id
        )
        == "TRAILER-GENERAL"
    ]

    general_photos = [
        media
        for media in general_media_records
        if storage_gallery_media_kind(media.image_path) == "photo"
    ]
    general_videos = [
        media
        for media in general_media_records
        if storage_gallery_media_kind(media.image_path) == "video"
    ]

    general_folder = {
        "media_count": len(general_media_records),
        "photo_count": len(general_photos),
        "video_count": len(general_videos),
        "cover_photo": general_photos[0] if general_photos else None,
    }

    slots = storage_gallery_build_slots(
        location,
        media_records,
    )

    slots = storage_gallery_add_inventory_summary(
        database,
        location,
        slots,
    )

    return templates.TemplateResponse(
        request=request,
        name="storage_gallery.html",
        context={
            "gallery_mode": "load_map",
            "albums": [],
            "location": location,
            "gallery_settings": storage_gallery_get_settings(location),
            "slots": slots,
            "slot": None,
            "media_records": media_records,
            "general_media_records": general_media_records,
            "general_folder": general_folder,
        },
    )


@app.get(
    "/storage-gallery/{location_id}/general",
    response_class=HTMLResponse,
)
def storage_gallery_general_page(
    request: Request,
    location_id: int,
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto

    location = database.get(InventoryLocation, location_id)

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    media_records = database.scalars(
        select(StoragePhoto)
        .where(
            StoragePhoto.location_id == location_id,
            StoragePhoto.container_id == "TRAILER-GENERAL",
        )
        .order_by(StoragePhoto.created_at.desc())
    ).all()

    slots = storage_gallery_build_slots(location, [])

    return templates.TemplateResponse(
        request=request,
        name="storage_gallery.html",
        context={
            "gallery_mode": "general",
            "albums": [],
            "location": location,
            "gallery_settings": storage_gallery_get_settings(location),
            "slots": slots,
            "slot": None,
            "media_records": media_records,
            "general_media_records": media_records,
            "pallet_products": [],
        },
    )


@app.get(
    "/storage-gallery/{location_id}/slot/{slot_code}",
    response_class=HTMLResponse,
)
def storage_gallery_slot_page(
    request: Request,
    location_id: int,
    slot_code: str,
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto

    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    allowed_slots = {
        slot["slot_code"]: slot
        for slot in storage_gallery_build_slots(
            location,
            [],
        )
    }

    if slot_code not in allowed_slots:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pallet slot not found.",
        )

    media_records = database.scalars(
        select(StoragePhoto)
        .where(
            StoragePhoto.location_id == location_id,
            StoragePhoto.container_id == slot_code,
        )
        .order_by(StoragePhoto.created_at.desc())
    ).all()

    slot = allowed_slots[slot_code]

    storage_gallery_add_inventory_summary(
        database,
        location,
        [slot],
    )
    slot["media"] = media_records
    slot["photos"] = [
        media
        for media in media_records
        if storage_gallery_media_kind(
            media.image_path
        )
        == "photo"
    ]
    slot["videos"] = [
        media
        for media in media_records
        if storage_gallery_media_kind(
            media.image_path
        )
        == "video"
    ]
    slot["photo_count"] = len(slot["photos"])
    slot["video_count"] = len(slot["videos"])
    slot["media_count"] = len(media_records)

    pallet_products = storage_gallery_pallet_products(
        database,
        location,
        slot_code,
    )

    return templates.TemplateResponse(
        request=request,
        name="storage_gallery.html",
        context={
            "gallery_mode": "slot",
            "albums": [],
            "location": location,
            "gallery_settings": storage_gallery_get_settings(location),
            "slots": [],
            "slot": slot,
            "media_records": media_records,
            "pallet_products": pallet_products,
        },
    )




@app.post(
    "/storage-gallery/{location_id}/general/upload"
)
async def storage_gallery_general_upload(
    location_id: int,
    storage_media: UploadFile = File(...),
    caption: str = Form(""),
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto
    from uuid import uuid4

    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    media_types = {
        "image/jpeg": ("photo", ".jpg"),
        "image/jpg": ("photo", ".jpg"),
        "image/png": ("photo", ".png"),
        "image/webp": ("photo", ".webp"),
        "image/heic": ("photo", ".heic"),
        "image/heif": ("photo", ".heif"),
        "video/mp4": ("video", ".mp4"),
        "video/quicktime": ("video", ".mov"),
        "video/x-m4v": ("video", ".m4v"),
        "video/webm": ("video", ".webm"),
    }

    content_type = (
        storage_media.content_type
        or ""
    ).casefold()

    if content_type not in media_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "General trailer media must be a supported "
                "photo or video."
            ),
        )

    media_kind, extension = media_types[
        content_type
    ]

    media_bytes = await storage_media.read()

    max_bytes = (
        250 * 1024 * 1024
        if media_kind == "video"
        else 25 * 1024 * 1024
    )

    if len(media_bytes) > max_bytes:
        size_label = (
            "250 MB"
            if media_kind == "video"
            else "25 MB"
        )

        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"{media_kind.title()} is larger than "
                f"{size_label}."
            ),
        )

    location_folder = (
        str(
            location.location_name
            or f"location-{location_id}"
        )
        .strip()
        .casefold()
        .replace(" ", "_")
        .replace("/", "_")
        .replace(chr(92), "_")
    )

    media_folder = (
        "videos"
        if media_kind == "video"
        else "photos"
    )

    storage_directory = (
        STATIC_DIRECTORY
        / "storage_gallery"
        / location_folder
        / "general"
        / media_folder
    )

    storage_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = f"{uuid4().hex}{extension}"
    file_path = storage_directory / filename
    file_path.write_bytes(
        media_bytes
    )

    public_path = (
        "/static/storage_gallery/"
        f"{location_folder}/"
        "general/"
        f"{media_folder}/"
        f"{filename}"
    )

    media_record = StoragePhoto(
        location_id=location_id,
        container_id="TRAILER-GENERAL",
        image_path=public_path,
        caption=caption.strip() or None,
    )

    database.add(media_record)
    database.commit()

    return RedirectResponse(
        url=f"/storage-gallery/{location_id}/general",
        status_code=status.HTTP_303_SEE_OTHER,
    )



# ============================================================
# STORAGE GALLERY MULTI-UPLOAD
# ============================================================

async def storage_gallery_save_media_batch(
    *,
    database: Session,
    location,
    container_id: str,
    folder_name: str,
    uploads: list[UploadFile],
    caption: str = "",
):
    from app.database.models import StoragePhoto
    from uuid import uuid4

    media_types = {
        "image/jpeg": ("photos", ".jpg", 25),
        "image/jpg": ("photos", ".jpg", 25),
        "image/png": ("photos", ".png", 25),
        "image/webp": ("photos", ".webp", 25),
        "image/heic": ("photos", ".heic", 25),
        "image/heif": ("photos", ".heif", 25),
        "video/mp4": ("videos", ".mp4", 250),
        "video/quicktime": ("videos", ".mov", 250),
        "video/x-m4v": ("videos", ".m4v", 250),
        "video/webm": ("videos", ".webm", 250),
    }

    clean_uploads = [
        upload
        for upload in uploads
        if upload is not None
        and str(upload.filename or "").strip()
    ]

    if not clean_uploads:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose at least one photo or video.",
        )

    if len(clean_uploads) > 30:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload 30 files or fewer at one time.",
        )

    location_id = location.location_id

    location_folder = (
        str(location.location_name or f"location-{location_id}")
        .strip()
        .casefold()
        .replace(" ", "_")
        .replace("/", "_")
        .replace(chr(92), "_")
    )

    prepared = []

    for upload in clean_uploads:
        content_type = (
            upload.content_type or ""
        ).casefold()

        if content_type not in media_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{upload.filename or 'A selected file'} "
                    "is not a supported photo/video."
                ),
            )

        prepared.append(
            (
                upload,
                *media_types[content_type],
            )
        )

    created_paths = []

    try:
        for upload, media_folder, extension, max_mb in prepared:
            media_bytes = await upload.read()

            if len(media_bytes) > (max_mb * 1024 * 1024):
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        f"{upload.filename or 'Selected file'} "
                        f"is larger than {max_mb} MB."
                    ),
                )

            storage_directory = (
                STATIC_DIRECTORY
                / "storage_gallery"
                / location_folder
                / folder_name
                / media_folder
            )

            storage_directory.mkdir(
                parents=True,
                exist_ok=True,
            )

            filename = f"{uuid4().hex}{extension}"
            file_path = storage_directory / filename

            file_path.write_bytes(media_bytes)
            created_paths.append(file_path)

            public_path = (
                "/static/storage_gallery/"
                f"{location_folder}/"
                f"{folder_name}/"
                f"{media_folder}/"
                f"{filename}"
            )

            database.add(
                StoragePhoto(
                    location_id=location_id,
                    container_id=container_id,
                    image_path=public_path,
                    caption=caption.strip() or None,
                )
            )

        database.commit()

    except Exception:
        database.rollback()

        for file_path in created_paths:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception:
                pass

        raise


@app.post(
    "/storage-gallery/{location_id}/slot/{slot_code}/upload-multiple"
)
async def storage_gallery_slot_upload_multiple(
    location_id: int,
    slot_code: str,
    storage_media: list[UploadFile] = File(...),
    caption: str = Form(""),
    database: Session = Depends(get_database),
):
    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    allowed_slots = {
        item["slot_code"]
        for item in storage_gallery_build_slots(
            location,
            [],
        )
    }

    if slot_code not in allowed_slots:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pallet slot not found.",
        )

    await storage_gallery_save_media_batch(
        database=database,
        location=location,
        container_id=slot_code,
        folder_name=slot_code,
        uploads=storage_media,
        caption=caption,
    )

    return RedirectResponse(
        url=(
            f"/storage-gallery/{location_id}"
            f"/slot/{slot_code}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/storage-gallery/{location_id}/general/upload-multiple"
)
async def storage_gallery_general_upload_multiple(
    location_id: int,
    storage_media: list[UploadFile] = File(...),
    caption: str = Form(""),
    database: Session = Depends(get_database),
):
    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    await storage_gallery_save_media_batch(
        database=database,
        location=location,
        container_id="TRAILER-GENERAL",
        folder_name="general",
        uploads=storage_media,
        caption=caption,
    )

    return RedirectResponse(
        url=f"/storage-gallery/{location_id}/general",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/storage-gallery/media/{media_id}/assign-slot"
)
def storage_gallery_assign_media_to_slot(
    media_id: int,
    slot_code: str = Form(...),
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto

    media_record = database.get(StoragePhoto, media_id)

    if media_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found.",
        )

    location = database.get(
        InventoryLocation,
        media_record.location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    normalized_slot = str(slot_code or "").strip().upper()
    allowed_slots = {
        slot["slot_code"]
        for slot in storage_gallery_build_slots(location, [])
    }

    if normalized_slot not in allowed_slots:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a valid pallet position.",
        )

    media_record.container_id = normalized_slot
    database.commit()

    return RedirectResponse(
        url=f"/storage-gallery/{location.location_id}/general",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/storage-gallery/{location_id}/slot/{slot_code}/upload"
)
async def storage_gallery_slot_upload(
    location_id: int,
    slot_code: str,
    storage_media: UploadFile = File(...),
    caption: str = Form(""),
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto
    from uuid import uuid4

    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    allowed_slots = {
        slot["slot_code"]
        for slot in storage_gallery_build_slots(
            location,
            [],
        )
    }

    if slot_code not in allowed_slots:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pallet slot not found.",
        )

    image_types = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }

    video_types = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-m4v": ".m4v",
        "video/webm": ".webm",
    }

    content_type = (
        storage_media.content_type
        or ""
    ).casefold()

    if content_type in image_types:
        media_kind = "photos"
        extension = image_types[content_type]

    elif content_type in video_types:
        media_kind = "videos"
        extension = video_types[content_type]

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Media must be a supported photo or video "
                "(JPG, PNG, WEBP, HEIC, MP4, MOV, M4V, WEBM)."
            ),
        )

    location_folder = (
        str(location.location_name or f"location-{location_id}")
        .strip()
        .casefold()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
    )

    storage_directory = (
        STATIC_DIRECTORY
        / "storage_gallery"
        / location_folder
        / slot_code
        / media_kind
    )

    storage_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = (
        f"{uuid4().hex}"
        f"{extension}"
    )

    file_path = storage_directory / filename

    media_bytes = await storage_media.read()

    max_media_bytes = 250 * 1024 * 1024

    if len(media_bytes) > max_media_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Media file is larger than 250 MB.",
        )

    file_path.write_bytes(media_bytes)

    public_path = (
        "/static/storage_gallery/"
        f"{location_folder}/"
        f"{slot_code}/"
        f"{media_kind}/"
        f"{filename}"
    )

    media_record = StoragePhoto(
        location_id=location_id,
        container_id=slot_code,
        image_path=public_path,
        caption=caption.strip() or None,
    )

    database.add(media_record)
    database.commit()

    return RedirectResponse(
        url=(
            f"/storage-gallery/{location_id}"
            f"/slot/{slot_code}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )




@app.post("/storage-gallery/{location_id}/settings")
def storage_gallery_save_settings(
    location_id: int,
    display_name: str = Form(...),
    database: Session = Depends(get_database),
):
    location = database.get(InventoryLocation, location_id)

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    cleaned_name = str(display_name or "").strip()

    if not cleaned_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Storage display name cannot be blank.",
        )

    if len(cleaned_name) > 80:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Storage display name must be 80 characters or fewer.",
        )

    storage_gallery_update_settings(
        location,
        display_name=cleaned_name,
    )

    return RedirectResponse(
        url=f"/storage-gallery/{location_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/storage-gallery/{location_id}/cover")
async def storage_gallery_upload_cover(
    location_id: int,
    cover_image: UploadFile = File(...),
    database: Session = Depends(get_database),
):
    from uuid import uuid4

    location = database.get(InventoryLocation, location_id)

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    image_types = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }

    content_type = (cover_image.content_type or "").casefold()

    if content_type not in image_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cover must be JPG, PNG, WEBP, or HEIC.",
        )

    image_bytes = await cover_image.read()
    max_cover_bytes = 25 * 1024 * 1024

    if len(image_bytes) > max_cover_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Cover image is larger than 25 MB.",
        )

    settings = storage_gallery_get_settings(location)
    old_cover = settings.get("cover_image_path")

    cover_directory = (
        STATIC_DIRECTORY
        / "storage_gallery"
        / "covers"
        / str(location_id)
    )
    cover_directory.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid4().hex}{image_types[content_type]}"
    file_path = cover_directory / filename
    file_path.write_bytes(image_bytes)

    public_path = (
        "/static/storage_gallery/"
        f"covers/{location_id}/{filename}"
    )

    storage_gallery_update_settings(
        location,
        cover_image_path=public_path,
        update_cover=True,
    )

    if old_cover:
        try:
            relative_old = str(old_cover).replace("/static/", "", 1)
            old_path = STATIC_DIRECTORY / relative_old
            if old_path.exists():
                old_path.unlink()
        except Exception:
            pass

    return RedirectResponse(
        url=f"/storage-gallery/{location_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/storage-gallery/{location_id}/cover/delete")
def storage_gallery_delete_cover(
    location_id: int,
    database: Session = Depends(get_database),
):
    location = database.get(InventoryLocation, location_id)

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage location not found.",
        )

    settings = storage_gallery_get_settings(location)
    old_cover = settings.get("cover_image_path")

    if old_cover:
        try:
            relative_old = str(old_cover).replace("/static/", "", 1)
            old_path = STATIC_DIRECTORY / relative_old
            if old_path.exists():
                old_path.unlink()
        except Exception:
            pass

    storage_gallery_update_settings(
        location,
        cover_image_path=None,
        update_cover=True,
    )

    return RedirectResponse(
        url=f"/storage-gallery/{location_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

@app.post(
    "/storage-gallery/media/{media_id}/delete"
)
def storage_gallery_delete_media(
    media_id: int,
    database: Session = Depends(get_database),
):
    from app.database.models import StoragePhoto

    media_record = database.get(
        StoragePhoto,
        media_id,
    )

    if media_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found.",
        )

    location_id = media_record.location_id
    slot_code = str(
        media_record.container_id or ""
    ).strip()

    try:
        relative_path = media_record.image_path.replace(
            "/static/",
            "",
            1,
        )

        local_path = (
            STATIC_DIRECTORY
            / relative_path
        )

        if local_path.exists():
            local_path.unlink()

    except Exception:
        pass

    database.delete(media_record)
    database.commit()

    if slot_code == "TRAILER-GENERAL":
        redirect_url = (
            f"/storage-gallery/{location_id}/general"
        )
    elif slot_code:
        redirect_url = (
            f"/storage-gallery/{location_id}"
            f"/slot/{slot_code}"
        )
    else:
        redirect_url = (
            f"/storage-gallery/{location_id}"
        )

    return RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ============================================================
# TOTE MANAGER
# ============================================================

def tote_manager_redirect(
    container_id: str = "",
    location_id: int | None = None,
    *,
    message: str = "",
    error: str = "",
):
    parameters = []
    if container_id:
        parameters.append("container_id=" + quote_plus(container_id))
    if location_id:
        parameters.append("location_id=" + str(location_id))
    if message:
        parameters.append("message=" + quote_plus(message))
    if error:
        parameters.append("error=" + quote_plus(error))
    suffix = "?" + "&".join(parameters) if parameters else ""
    return RedirectResponse(
        url="/inventory/tote-manager" + suffix,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/admin/system-check", response_class=HTMLResponse)
def admin_system_check_page(request: Request):
    auth_user = getattr(request.state, "auth_user", None)
    if auth_user is not None and getattr(auth_user, "role", "") != "owner_admin":
        raise HTTPException(status_code=403, detail="Owner/admin access is required.")
    return templates.TemplateResponse(
        request=request,
        name="admin_system_check.html",
        context={"report": build_system_check()},
    )


def receive_scan_details(
    database: Session,
    barcode: str,
    product: Product,
) -> dict:
    """Describe how many retail units one scanned barcode represents.

    Older unit barcodes and databases without packaging metadata safely fall
    back to one retail unit.  A barcode linked to another product is rejected
    so receiving can never add stock to the wrong item.
    """
    details = {
        "barcode_record": None,
        "barcode_type": "UNIT",
        "quantity_per_scan": 1,
        "is_package": False,
    }
    if not str(barcode or "").strip() or product is None:
        return details

    barcode_record = find_exact_product_barcode(database, barcode)
    if barcode_record is None:
        # The normal product lookup also accepts zero-padded/check-digit
        # variants.  Those variants are unit scans unless explicitly mapped.
        return details
    if int(barcode_record.product_id) != int(product.product_id):
        raise ValueError(
            "The scanned barcode is mapped to a different BrooksHouse product. "
            "Nothing was received."
        )

    barcode_type = str(barcode_record.barcode_type or "UNIT").strip().upper()
    try:
        quantity_per_scan = max(1, int(barcode_record.quantity_per_scan or 1))
    except (TypeError, ValueError):
        quantity_per_scan = 1
    unit_types = {"UNIT", "UPC", "UPC-A", "UPC-E", "EAN", "EAN-13", "GTIN", "BROOKSHOUSE_INTERNAL"}
    is_package = quantity_per_scan > 1 or barcode_type not in unit_types
    details.update(
        barcode_record=barcode_record,
        barcode_type=barcode_type,
        quantity_per_scan=quantity_per_scan,
        is_package=is_package,
    )
    return details


def tote_manager_primary_barcode(product: Product) -> str:
    if not product.barcodes:
        return ""
    primary = next(
        (
            item.barcode
            for item in product.barcodes
            if getattr(item, "is_primary", False)
        ),
        None,
    )
    return primary or product.barcodes[0].barcode or ""


@app.get(
    "/inventory/tote-manager",
    response_class=HTMLResponse,
)
def tote_manager_page(
    request: Request,
    container_id: str = "",
    location_id: int | None = None,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()
    clean_container_id = normalize_container_id(container_id)
    matching_records = []
    matching_location_ids = set()

    if clean_container_id:
        candidates = database.scalars(
            select(Inventory).order_by(
                Inventory.location_id,
                Inventory.inventory_id,
            )
        ).all()
        matching_records = [
            record
            for record in candidates
            if normalize_container_id(record.container_id)
            == clean_container_id
        ]
        matching_location_ids = {
            record.location_id for record in matching_records
        }

    selected_location_id = int(location_id or 0)
    scoped_records = (
        [
            record
            for record in matching_records
            if record.location_id == selected_location_id
        ]
        if selected_location_id
        else matching_records
    )
    rows = []
    total_units = 0
    total_reserved = 0
    for record in scoped_records:
        quantity = int(record.quantity_on_hand or 0)
        reserved = int(record.quantity_reserved or 0)
        total_units += quantity
        total_reserved += reserved
        rows.append(
            {
                "inventory_id": record.inventory_id,
                "product_id": record.product_id,
                "product_name": record.product.product_name,
                "brand": record.product.brand or "",
                "barcode": tote_manager_primary_barcode(record.product),
                "location_id": record.location_id,
                "location_name": record.location.location_name,
                "quantity": quantity,
                "reserved": reserved,
                "available": max(quantity - reserved, 0),
                "updated_at": record.updated_at,
            }
        )

    tote_locations = [
        location
        for location in locations
        if location.location_id in matching_location_ids
    ]
    history = []
    if clean_container_id:
        transactions = database.scalars(
            select(InventoryTransaction)
            .where(
                func.upper(InventoryTransaction.container_id)
                == clean_container_id.upper()
            )
            .order_by(InventoryTransaction.transaction_id.desc())
            .limit(75)
        ).all()
        history = transactions

    return templates.TemplateResponse(
        request=request,
        name="tote_manager.html",
        context={
            "locations": locations,
            "container_id": clean_container_id,
            "selected_location_id": selected_location_id,
            "tote_locations": tote_locations,
            "rows": rows,
            "total_units": total_units,
            "total_reserved": total_reserved,
            "history": history,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/inventory/tote-manager/rename")
def tote_manager_rename(
    source_container_id: str = Form(...),
    source_location_id: int = Form(...),
    destination_container_id: str = Form(...),
    reason: str = Form("Tote label correction"),
    database: Session = Depends(get_database),
):
    source = normalize_container_id(source_container_id)
    destination = normalize_container_id(destination_container_id)
    reason = (reason or "Tote label correction").strip()
    if not source or not destination:
        return tote_manager_redirect(source, source_location_id, error="Both tote labels are required.")
    if source == destination:
        return tote_manager_redirect(source, source_location_id, error="The corrected tote label is unchanged.")
    location = database.get(InventoryLocation, source_location_id)
    records = database.scalars(
        select(Inventory).where(Inventory.location_id == source_location_id)
    ).all()
    records = [r for r in records if normalize_container_id(r.container_id) == source]
    if location is None or not records:
        return tote_manager_redirect(source, source_location_id, error="That tote was not found at the selected location.")
    reference = "TOTE-RENAME-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        for record in records:
            quantity = int(record.quantity_on_hand or 0)
            reserved = int(record.quantity_reserved or 0)
            target = database.scalar(
                select(Inventory).where(
                    Inventory.product_id == record.product_id,
                    Inventory.location_id == source_location_id,
                    func.upper(Inventory.container_id)
                    == destination.upper(),
                    Inventory.inventory_id != record.inventory_id,
                )
            )
            if target is None:
                record.container_id = destination
            else:
                target.quantity_on_hand = int(target.quantity_on_hand or 0) + quantity
                target.quantity_reserved = int(target.quantity_reserved or 0) + reserved
                database.delete(record)
            for tote, change, note in (
                (source, -quantity, f"Removed during tote rename to {destination}."),
                (destination, quantity, f"Added during tote rename from {source}."),
            ):
                database.add(InventoryTransaction(
                    product_id=record.product_id,
                    location_id=source_location_id,
                    container_id=tote,
                    transaction_type="tote_manager_rename",
                    quantity_change=change,
                    unit_cost=record.product.average_cost,
                    reference_number=reference,
                    notes=f"{note} Reason: {reason}",
                ))
        database.commit()
    except Exception:
        database.rollback()
        raise
    return tote_manager_redirect(destination, source_location_id, message=f"Renamed {source} to {destination} and preserved its history.")


@app.post("/inventory/tote-manager/move")
def tote_manager_move(
    source_container_id: str = Form(...),
    source_location_id: int = Form(...),
    destination_location_id: int = Form(...),
    destination_container_id: str = Form(...),
    reason: str = Form("Tote moved"),
    database: Session = Depends(get_database),
):
    source = normalize_container_id(source_container_id)
    destination = normalize_container_id(destination_container_id)
    reason = (reason or "Tote moved").strip()
    source_location = database.get(InventoryLocation, source_location_id)
    destination_location = database.get(InventoryLocation, destination_location_id)
    if not source or not destination:
        return tote_manager_redirect(source, source_location_id, error="Source and destination tote labels are required.")
    if source_location is None or destination_location is None:
        return tote_manager_redirect(source, source_location_id, error="Choose valid source and destination locations.")
    if source_location_id == destination_location_id and source == destination:
        return tote_manager_redirect(source, source_location_id, error="The destination is the same as the source.")
    records = database.scalars(
        select(Inventory).where(Inventory.location_id == source_location_id)
    ).all()
    records = [r for r in records if normalize_container_id(r.container_id) == source]
    if not records:
        return tote_manager_redirect(source, source_location_id, error="That tote was not found at the selected source location.")
    reference = "TOTE-MOVE-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        for record in records:
            quantity = int(record.quantity_on_hand or 0)
            reserved = int(record.quantity_reserved or 0)
            target = database.scalar(select(Inventory).where(
                Inventory.product_id == record.product_id,
                Inventory.location_id == destination_location_id,
                func.upper(Inventory.container_id)
                == destination.upper(),
            ))
            product_id = record.product_id
            unit_cost = record.product.average_cost
            if target is None:
                record.location_id = destination_location_id
                record.container_id = destination
            else:
                target.quantity_on_hand = int(target.quantity_on_hand or 0) + quantity
                target.quantity_reserved = int(target.quantity_reserved or 0) + reserved
                database.delete(record)
            database.add(InventoryTransaction(
                product_id=product_id, location_id=source_location_id,
                container_id=source, transaction_type="tote_manager_move_out",
                quantity_change=-quantity, unit_cost=unit_cost,
                reference_number=reference,
                notes=f"Whole tote moved to {destination_location.location_name} / {destination}. Reason: {reason}",
            ))
            database.add(InventoryTransaction(
                product_id=product_id, location_id=destination_location_id,
                container_id=destination, transaction_type="tote_manager_move_in",
                quantity_change=quantity, unit_cost=unit_cost,
                reference_number=reference,
                notes=f"Whole tote moved from {source_location.location_name} / {source}. Reason: {reason}",
            ))
        database.commit()
    except Exception:
        database.rollback()
        raise
    return tote_manager_redirect(destination, destination_location_id, message=f"Moved {source} to {destination_location.location_name} / {destination}.")


@app.post("/inventory/tote-manager/line")
def tote_manager_update_line(
    inventory_id: int = Form(...),
    action: str = Form(...),
    quantity: int = Form(...),
    destination_location_id: int = Form(0),
    destination_container_id: str = Form(""),
    reason: str = Form(...),
    database: Session = Depends(get_database),
):
    record = database.get(Inventory, inventory_id)
    if record is None:
        return tote_manager_redirect(error="That inventory row no longer exists.")
    source = normalize_container_id(record.container_id)
    source_location_id = record.location_id
    reason = (reason or "").strip()
    if not reason:
        return tote_manager_redirect(source, source_location_id, error="A reason is required for every correction.")
    before = int(record.quantity_on_hand or 0)
    reserved = int(record.quantity_reserved or 0)
    reference = "TOTE-LINE-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    if action == "correct":
        if quantity < reserved:
            return tote_manager_redirect(source, source_location_id, error=f"Quantity cannot be below the {reserved} reserved units.")
        difference = quantity - before
        if difference == 0:
            return tote_manager_redirect(source, source_location_id, error="The corrected quantity is unchanged.")
        record.quantity_on_hand = quantity
        database.add(InventoryTransaction(
            product_id=record.product_id, location_id=source_location_id,
            container_id=source, transaction_type="tote_manager_adjust",
            quantity_change=difference, unit_cost=record.product.average_cost,
            reference_number=reference,
            notes=f"Tote Manager quantity correction: {before} to {quantity}. Reason: {reason}",
        ))
        database.commit()
        return tote_manager_redirect(source, source_location_id, message=f"Corrected {record.product.product_name} from {before} to {quantity} units.")
    if action != "move":
        return tote_manager_redirect(source, source_location_id, error="Unknown Tote Manager action.")
    available = max(before - reserved, 0)
    if quantity <= 0 or quantity > available:
        return tote_manager_redirect(source, source_location_id, error=f"Move between 1 and {available} available units.")
    destination_location = database.get(InventoryLocation, destination_location_id)
    destination = normalize_container_id(destination_container_id)
    if destination_location is None:
        return tote_manager_redirect(source, source_location_id, error="Choose a destination location.")
    if destination_location_id == source_location_id and destination == source:
        return tote_manager_redirect(source, source_location_id, error="The destination is the same as the source.")
    target = database.scalar(select(Inventory).where(
        Inventory.product_id == record.product_id,
        Inventory.location_id == destination_location_id,
        func.upper(Inventory.container_id)
        == destination.upper(),
    ))
    if target is None:
        target = Inventory(
            product_id=record.product_id,
            location_id=destination_location_id,
            container_id=destination,
            quantity_on_hand=0,
            quantity_reserved=0,
            reorder_level=record.reorder_level,
        )
        database.add(target)
    record.quantity_on_hand = before - quantity
    target.quantity_on_hand = int(target.quantity_on_hand or 0) + quantity
    for location, tote, change, note in (
        (source_location_id, source, -quantity, f"Moved to {destination_location.location_name} / {destination or 'NO CONTAINER'}."),
        (destination_location_id, destination, quantity, f"Moved from inventory row {inventory_id} in {source or 'NO CONTAINER'}."),
    ):
        database.add(InventoryTransaction(
            product_id=record.product_id, location_id=location,
            container_id=tote, transaction_type="tote_manager_line_move",
            quantity_change=change, unit_cost=record.product.average_cost,
            reference_number=reference, notes=f"{note} Reason: {reason}",
        ))
    database.commit()
    return tote_manager_redirect(source, source_location_id, message=f"Moved {quantity} units of {record.product.product_name}.")


# ============================================================
# TOTE REPAIR PAGE
# ============================================================

@app.get(
    "/inventory/tote-repair",
    response_class=HTMLResponse,
)
def tote_repair_page(
    request: Request,
    database: Session = Depends(get_database),
):
    records = database.scalars(
        select(Inventory)
        .where(
            Inventory.quantity_on_hand > 0
        )
        .order_by(Inventory.inventory_id)
    ).all()

    unassigned = [
        record
        for record in records
        if not normalize_container_id(
            record.container_id
        )
    ]

    groups = []
    current_group = []

    for record in unassigned:

        if not current_group:
            current_group = [record]
            continue

        previous = current_group[-1]
        previous_id = previous.inventory_id

        # Groups are review aids only. Never visually combine
        # records from different locations, and split whenever
        # an inventory-id gap suggests a separate scan run.
        if (
            record.inventory_id != previous_id + 1
            or record.location_id != previous.location_id
        ):
            groups.append(current_group)
            current_group = [record]
        else:
            current_group.append(record)

    if current_group:
        groups.append(current_group)

    display_groups = []

    for number, group in enumerate(
        groups,
        start=1,
    ):
        rows = []

        total_units = 0

        for record in group:
            product = record.product

            barcode = ""

            if product.barcodes:
                primary = next(
                    (
                        item.barcode
                        for item in product.barcodes
                        if getattr(
                            item,
                            "is_primary",
                            False,
                        )
                    ),
                    None,
                )

                barcode = (
                    primary
                    or product.barcodes[0].barcode
                    or ""
                )

            quantity = int(
                record.quantity_on_hand or 0
            )

            total_units += quantity

            rows.append(
                {
                    "inventory_id": (
                        record.inventory_id
                    ),
                    "product_id": (
                        record.product_id
                    ),
                    "product_name": (
                        product.product_name
                    ),
                    "brand": product.brand,
                    "barcode": barcode,
                    "quantity": quantity,
                    "location_name": (
                        record.location.location_name
                    ),
                    "scanned_at": record.updated_at,
                }
            )

        display_groups.append(
            {
                "group_number": number,
                "first_id": (
                    group[0].inventory_id
                ),
                "last_id": (
                    group[-1].inventory_id
                ),
                "row_count": len(group),
                "total_units": total_units,
                "first_scan_at": (
                    group[0].updated_at
                ),
                "last_scan_at": (
                    group[-1].updated_at
                ),
                "rows": rows,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="tote_repair.html",
        context={
            "groups": display_groups,
            "message": request.query_params.get(
                "message"
            ),
            "error": request.query_params.get(
                "error"
            ),
        },
    )


@app.post(
    "/inventory/tote-repair/assign"
)
def assign_tote_repair_group(
    inventory_ids: list[int] = Form(...),
    container_id: str = Form(...),
    database: Session = Depends(get_database),
):
    clean_container_id = normalize_container_id(
        container_id
    )

    if not clean_container_id:
        return RedirectResponse(
            url=(
                "/inventory/tote-repair"
                "?error=Tote+label+is+required."
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    selected_ids = sorted(set(inventory_ids))

    records = database.scalars(
        select(Inventory)
        .where(
            Inventory.inventory_id.in_(selected_ids),
            Inventory.quantity_on_hand > 0,
        )
        .order_by(Inventory.inventory_id)
    ).all()

    records = [
        record
        for record in records
        if not normalize_container_id(
            record.container_id
        )
    ]

    if not records:
        return RedirectResponse(
            url=(
                "/inventory/tote-repair"
                "?error=No+unassigned+inventory+"
                "was+found+in+that+group."
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    found_ids = {
        record.inventory_id for record in records
    }

    if found_ids != set(selected_ids):
        return RedirectResponse(
            url=(
                "/inventory/tote-repair"
                "?error=One+or+more+selected+rows+changed."
                "+Review+the+group+and+try+again."
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    location_ids = {
        record.location_id for record in records
    }

    if len(location_ids) != 1:
        return RedirectResponse(
            url=(
                "/inventory/tote-repair"
                "?error=Selected+rows+must+belong+to+one+location."
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    updated = 0
    merged = 0
    total_units = 0

    try:
        for record in records:

            quantity = int(
                record.quantity_on_hand or 0
            )

            total_units += quantity

            existing = database.scalar(
                select(Inventory)
                .where(
                    Inventory.product_id
                    == record.product_id,
                    Inventory.location_id
                    == record.location_id,
                    Inventory.container_id
                    == clean_container_id,
                    Inventory.inventory_id
                    != record.inventory_id,
                )
            )

            if existing is not None:

                existing.quantity_on_hand = (
                    int(
                        existing.quantity_on_hand
                        or 0
                    )
                    + quantity
                )

                # Preserve reserved quantity if any
                existing.quantity_reserved = (
                    int(
                        existing.quantity_reserved
                        or 0
                    )
                    + int(
                        record.quantity_reserved
                        or 0
                    )
                )

                database.delete(record)
                merged += 1

            else:
                record.container_id = (
                    clean_container_id
                )
                updated += 1

        database.commit()

    except Exception:
        database.rollback()
        raise

    message = (
        f"{clean_container_id} repaired: "
        f"{updated} assigned, "
        f"{merged} merged, "
        f"{total_units} units."
    )

    from urllib.parse import quote_plus

    return RedirectResponse(
        url=(
            "/inventory/tote-repair"
            "?message="
            + quote_plus(message)
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )



# ============================================================
# TOTE AUDIT
# ============================================================

@app.get(
    "/inventory/tote-audit",
    response_class=HTMLResponse,
)
def tote_audit_page(
    request: Request,
    database: Session = Depends(get_database),
):
    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="tote_audit.html",
        context={
            "locations": locations,
        },
    )


@app.get("/inventory/tote-audit/check-tote")
def tote_audit_check_tote(
    location_id: int,
    container_id: str,
    database: Session = Depends(get_database),
):
    clean_container_id = normalize_container_id(
        container_id
    )

    if not clean_container_id:
        return {
            "found": False,
            "error": "Scan or enter a tote label.",
        }

    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        return {
            "found": False,
            "error": "The selected location was not found.",
        }

    inventory_records = database.scalars(
        select(Inventory)
        .where(
            Inventory.location_id == location_id
        )
        .order_by(Inventory.inventory_id)
    ).all()

    tote_records = [
        record
        for record in inventory_records
        if normalize_container_id(
            record.container_id
        ) == clean_container_id
    ]

    if not tote_records:
        return {
            "found": False,
            "error": (
                f"{clean_container_id} was not found in "
                f"{location.location_name}."
            ),
        }

    system_items = []

    for record in tote_records:
        product = record.product

        barcode = ""

        if product.barcodes:
            primary = next(
                (
                    barcode_record.barcode
                    for barcode_record
                    in product.barcodes
                    if getattr(
                        barcode_record,
                        "is_primary",
                        False,
                    )
                ),
                None,
            )

            barcode = (
                primary
                or product.barcodes[0].barcode
                or ""
            )

        system_items.append(
            {
                "inventory_id": record.inventory_id,
                "product_id": record.product_id,
                "product_name": product.product_name,
                "barcode": barcode,
                "quantity": int(
                    record.quantity_on_hand or 0
                ),
            }
        )

    return {
        "found": True,
        "location_id": location.location_id,
        "location_name": location.location_name,
        "container_id": clean_container_id,
        "system_items": system_items,
        "system_products": len(system_items),
        "system_units": sum(
            item["quantity"]
            for item in system_items
        ),
    }


@app.post("/inventory/tote-audit/save")
async def tote_audit_save(
    request: Request,
    database: Session = Depends(get_database),
):
    import json
    from datetime import datetime

    payload = await request.json()

    location_id = int(
        payload.get("location_id") or 0
    )

    clean_container_id = normalize_container_id(
        payload.get("container_id")
    )

    counted_items = payload.get(
        "counted_items"
    ) or []

    location = database.get(
        InventoryLocation,
        location_id,
    )

    if location is None:
        return {
            "success": False,
            "error": "Location not found.",
        }

    if not clean_container_id:
        return {
            "success": False,
            "error": "Tote label is required.",
        }

    all_location_records = database.scalars(
        select(Inventory)
        .where(
            Inventory.location_id == location_id
        )
    ).all()

    system_records = [
        record
        for record in all_location_records
        if normalize_container_id(
            record.container_id
        ) == clean_container_id
    ]

    if not system_records:
        return {
            "success": False,
            "error": (
                f"{clean_container_id} no longer exists "
                f"in {location.location_name}."
            ),
        }

    counted_by_product = {}

    for item in counted_items:
        product_id = int(
            item.get("product_id") or 0
        )

        quantity = int(
            item.get("quantity") or 0
        )

        if product_id <= 0:
            continue

        if quantity < 0:
            quantity = 0

        counted_by_product[product_id] = (
            counted_by_product.get(
                product_id,
                0,
            )
            + quantity
        )

    existing_by_product = {
        record.product_id: record
        for record in system_records
    }

    changed_lines = []
    audit_time = datetime.now()

    try:
        # First reconcile everything already
        # systematically assigned to this tote.
        for product_id, record in existing_by_product.items():

            before = int(
                record.quantity_on_hand or 0
            )

            counted = int(
                counted_by_product.pop(
                    product_id,
                    0,
                )
            )

            difference = counted - before

            if difference == 0:
                continue

            record.quantity_on_hand = counted

            changed_lines.append(
                {
                    "product_id": product_id,
                    "product_name": (
                        record.product.product_name
                    ),
                    "before": before,
                    "counted": counted,
                    "difference": difference,
                }
            )

            database.add(
                InventoryTransaction(
                    product=record.product,
                    location=location,
                    container_id=clean_container_id,
                    transaction_type="audit",
                    quantity_change=difference,
                    unit_cost=record.product.average_cost,
                    reference_number=(
                        "TOTE-AUDIT-"
                        + audit_time.strftime(
                            "%Y%m%d-%H%M%S"
                        )
                    ),
                    notes=(
                        "Tote audit. "
                        "Reason: Found Inventory. "
                        f"System quantity {before}; "
                        f"physical count {counted}."
                    ),
                )
            )

        # Anything still in counted_by_product
        # was physically found in the tote but was
        # not systematically assigned to that tote.
        for product_id, counted in counted_by_product.items():

            if counted <= 0:
                continue

            product = database.get(
                Product,
                product_id,
            )

            if product is None:
                continue

            existing = database.scalar(
                select(Inventory)
                .where(
                    Inventory.product_id
                    == product_id,
                    Inventory.location_id
                    == location_id,
                    Inventory.container_id
                    == clean_container_id,
                )
            )

            if existing is None:
                existing = Inventory(
                    product=product,
                    location=location,
                    container_id=clean_container_id,
                    quantity_on_hand=counted,
                    quantity_reserved=0,
                    reorder_level=0,
                )

                database.add(existing)

            else:
                existing.quantity_on_hand = counted

            changed_lines.append(
                {
                    "product_id": product_id,
                    "product_name": (
                        product.product_name
                    ),
                    "before": 0,
                    "counted": counted,
                    "difference": counted,
                }
            )

            database.add(
                InventoryTransaction(
                    product=product,
                    location=location,
                    container_id=clean_container_id,
                    transaction_type="audit",
                    quantity_change=counted,
                    unit_cost=product.average_cost,
                    reference_number=(
                        "TOTE-AUDIT-"
                        + audit_time.strftime(
                            "%Y%m%d-%H%M%S"
                        )
                    ),
                    notes=(
                        "Tote audit. "
                        "Reason: Found Inventory. "
                        "Item physically found in tote "
                        "but not systematically assigned."
                    ),
                )
            )

        database.commit()

    except Exception as error:
        database.rollback()

        return {
            "success": False,
            "error": (
                "Audit could not be saved. "
                f"Technical details: {error}"
            ),
        }

    return {
        "success": True,
        "container_id": clean_container_id,
        "location_name": location.location_name,
        "changed_lines": changed_lines,
        "changed_count": len(changed_lines),
        "audit_time": audit_time.strftime(
            "%m/%d/%Y %I:%M:%S %p"
        ),
    }


# ============================================================
# WALMART ORDER DESK
# ============================================================

from app.walmart_order_service import (
    DB_PATH as WALMART_ORDER_DB_PATH,
    ensure_order_tables,
    load_order_desk,
    remove_walmart_product_mapping,
    save_walmart_product_mapping,
    search_mapping_products,
    shipment_payload,
    sync_orders,
    sync_today_orders,
    walmart_request,
)


@app.get("/channels/walmart/orders", response_class=HTMLResponse)
def walmart_order_desk_page(
    request: Request,
    history_days: str = "30",
    order_status: str = "all",
    inventory_status: str = "all",
    physical_site: str = "all",
    search: str = "",
    mapping_search: str = "",
):
    from datetime import timedelta

    orders = load_order_desk()
    normalized_status = order_status.strip().casefold()
    normalized_inventory = inventory_status.strip().casefold()
    normalized_site = physical_site.strip().casefold()
    normalized_search = clean_search_term(search)

    if history_days.strip().casefold() != "all":
        try:
            safe_days = max(1, min(365, int(history_days)))
        except (TypeError, ValueError):
            safe_days = 30
        cutoff = datetime.now().astimezone() - timedelta(days=safe_days)
        orders = [
            order
            for order in orders
            if order.get("local_status") not in {"shipped", "cancelled"}
            or order.get("order_datetime") is None
            or order["order_datetime"] >= cutoff
        ]
        history_days = str(safe_days)
    else:
        history_days = "all"

    status_groups = {
        "open": {"new", "acknowledged", "pulling", "pulled", "picked", "packed", "staged", "shipment_submitted"},
        "picking": {"pulling", "pulled", "picked"},
        "completed": {"shipped", "cancelled"},
    }
    if normalized_status != "all":
        wanted_statuses = status_groups.get(
            normalized_status,
            {normalized_status},
        )
        orders = [
            order
            for order in orders
            if str(order.get("local_status") or "").casefold()
            in wanted_statuses
        ]

    if normalized_inventory != "all":
        orders = [
            order
            for order in orders
            if str(order.get("inventory_state") or "").casefold()
            == normalized_inventory
        ]

    if normalized_site != "all":
        orders = [
            order
            for order in orders
            if normalized_site
            in {
                str(site).casefold()
                for site in order.get("site_names") or []
            }
        ]

    if normalized_search:
        filtered_orders = []
        for order in orders:
            searchable = [
                order.get("purchase_order_id"),
                order.get("customer_order_id"),
                *(order.get("site_names") or []),
            ]
            for line in order.get("lines") or []:
                searchable.extend(
                    [
                        line.get("item_name"),
                        line.get("product_barcode"),
                        line.get("sku"),
                        line.get("upc"),
                    ]
                )
            if wildcard_matches_any(searchable, normalized_search):
                filtered_orders.append(order)
        orders = filtered_orders

    return templates.TemplateResponse(
        request=request,
        name="walmart_orders.html",
        context={
            "orders": orders,
            "summary": {
                "total_orders": len(orders),
                "total_units": sum(order["unit_count"] for order in orders),
                "ready_orders": sum(order["inventory_state"] == "ready" for order in orders),
                "split_orders": sum(bool(order["split_location"]) for order in orders),
                "missing_orders": sum(order["inventory_state"] != "ready" for order in orders),
                "completed_orders": sum(order.get("local_status") in {"shipped", "cancelled"} for order in orders),
                "gross_sales": sum(float(order.get("order_total") or 0) for order in orders),
                "estimated_cost": sum(float(order.get("estimated_cost") or 0) for order in orders),
                "estimated_profit": sum(float(order.get("estimated_profit") or 0) for order in orders),
            },
            "filters": {
                "history_days": history_days,
                "order_status": normalized_status,
                "inventory_status": normalized_inventory,
                "physical_site": normalized_site,
                "search": search,
            },
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "mapping_search": mapping_search,
            "mapping_products": search_mapping_products(mapping_search) if mapping_search.strip() else [],
        },
    )


@app.post("/channels/walmart/orders/lines/{order_line_id}/mapping")
def walmart_save_order_line_mapping(order_line_id: int, product_id: int = Form(...)):
    from urllib.parse import quote_plus
    try:
        sku, product_name = save_walmart_product_mapping(order_line_id, product_id)
        url = "/channels/walmart/orders?message=" + quote_plus(f"Walmart SKU {sku} is now linked to {product_name}.")
    except Exception as error:
        url = "/channels/walmart/orders?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=303)


@app.post("/channels/walmart/orders/lines/{order_line_id}/mapping/remove")
def walmart_remove_order_line_mapping(order_line_id: int):
    from urllib.parse import quote_plus
    try:
        sku = remove_walmart_product_mapping(order_line_id)
        url = "/channels/walmart/orders?message=" + quote_plus(f"Walmart SKU {sku} was unlinked. No product or inventory was deleted.")
    except Exception as error:
        url = "/channels/walmart/orders?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=303)


@app.get(
    "/channels/walmart/orders/pull-list",
    response_class=HTMLResponse,
)
def walmart_master_pull_list(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="walmart_pull_guide.html",
        context={
            "orders": load_order_desk(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.get(
    "/channels/walmart/orders/{purchase_order_id}",
    response_class=HTMLResponse,
)
def walmart_order_pull_page(purchase_order_id: str, request: Request):
    order = next(
        (
            item
            for item in load_order_desk()
            if item["purchase_order_id"] == purchase_order_id
        ),
        None,
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Walmart order was not found.")
    return templates.TemplateResponse(
        request=request,
        name="walmart_order_pull.html",
        context={
            "orders": [order],
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/channels/walmart/orders/sync")
def walmart_sync_orders(days_back: int = Form(3)):
    from urllib.parse import quote_plus
    try:
        safe_days = max(1, min(30, int(days_back)))
        count = sync_orders(safe_days)
        url = "/channels/walmart/orders?message=" + quote_plus(
            f"Walmart sync complete: {count} order(s) returned from the last {safe_days} day(s)."
        )
    except Exception as error:
        url = "/channels/walmart/orders?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


# ============================================================
# SHARED MARKETPLACE ORDER HUB
# ============================================================

from app.marketplace_order_service import (
    load_marketplace_orders,
    marketplace_summary,
)


@app.get("/channels/orders", response_class=HTMLResponse)
def marketplace_order_hub(request: Request):
    orders = load_marketplace_orders()
    return templates.TemplateResponse(
        request=request,
        name="marketplace_orders.html",
        context={
            "orders": orders,
            "summary": marketplace_summary(orders),
            "channel_connections": {
                "walmart": "connected",
                "amazon": "awaiting_api",
                "shopify": "planned",
            },
        },
    )


@app.get(
    "/channels/orders/pull-list",
    response_class=HTMLResponse,
)
def marketplace_master_pull_list(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="marketplace_pull_guide.html",
        context={
            "orders": load_marketplace_orders(),
        },
    )


@app.get("/channels/amazon/orders", response_class=HTMLResponse)
def amazon_order_setup_page(request: Request):
    return RedirectResponse(
        url="/channels/orders",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/channels/amazon/orders/pull-list",
    response_class=HTMLResponse,
)
def amazon_pull_list_setup_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="amazon_orders_pending.html",
        context={"page_type": "pull_list"},
    )


@app.post("/channels/walmart/orders/{purchase_order_id}/acknowledge")
def walmart_acknowledge_order(purchase_order_id: str):
    from urllib.parse import quote_plus
    import sqlite3 as _sqlite3
    try:
        walmart_request("POST", f"/v3/orders/{purchase_order_id}/acknowledge")
        connection = _sqlite3.connect(WALMART_ORDER_DB_PATH, timeout=30)
        connection.execute(
            "UPDATE walmart_orders SET local_status='acknowledged', acknowledged_at=? WHERE purchase_order_id=?",
            (datetime.now().astimezone().isoformat(), purchase_order_id),
        )
        connection.commit()
        connection.close()
        url = "/channels/walmart/orders?message=" + quote_plus(
            f"Order {purchase_order_id} acknowledged."
        )
    except Exception as error:
        url = "/channels/walmart/orders?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/channels/walmart/orders/{purchase_order_id}/stage/{stage_name}")
def walmart_update_fulfillment_stage(
    purchase_order_id: str,
    stage_name: str,
):
    from urllib.parse import quote_plus
    import sqlite3 as _sqlite3

    stage = stage_name.strip().casefold()
    connection = _sqlite3.connect(WALMART_ORDER_DB_PATH, timeout=30)
    connection.row_factory = _sqlite3.Row
    try:
        order = connection.execute(
            "SELECT * FROM walmart_orders WHERE purchase_order_id=?",
            (purchase_order_id,),
        ).fetchone()
        if order is None:
            raise ValueError("Walmart order was not found.")
        remaining = connection.execute(
            """
            SELECT COUNT(*) FROM walmart_order_lines
            WHERE purchase_order_id=? AND pulled_quantity < quantity
            """,
            (purchase_order_id,),
        ).fetchone()[0]
        now = datetime.now().astimezone().isoformat()

        if stage == "picked":
            if remaining:
                raise ValueError("Every product must be scanned before checking Picked.")
            connection.execute(
                """
                UPDATE walmart_orders SET local_status='picked', picked_at=?
                WHERE purchase_order_id=?
                """,
                (now, purchase_order_id),
            )
        elif stage == "packed":
            if remaining:
                raise ValueError("Every product must be picked before checking Packed.")
            connection.execute(
                """
                UPDATE walmart_orders SET local_status='packed', packed_at=?
                WHERE purchase_order_id=?
                """,
                (now, purchase_order_id),
            )
        elif stage == "staged":
            if order["local_status"] not in {"packed", "staged"}:
                raise ValueError("The order must be packed before it can be staged.")
            connection.execute(
                """
                UPDATE walmart_orders SET local_status='staged', staged_at=?
                WHERE purchase_order_id=?
                """,
                (now, purchase_order_id),
            )
        else:
            raise ValueError("Use the shipment screen to confirm carrier and tracking.")

        connection.commit()
        url = "/channels/walmart/orders/pull-list?message=" + quote_plus(
            f"Order {purchase_order_id} marked {stage}."
        )
    except Exception as error:
        connection.rollback()
        url = "/channels/walmart/orders/pull-list?error=" + quote_plus(str(error))
    finally:
        connection.close()
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/channels/walmart/orders/lines/{order_line_id}/pull")
def walmart_pull_order_line(
    order_line_id: int,
    inventory_id: int = Form(...),
    scanned_barcode: str = Form(...),
):
    from urllib.parse import quote_plus
    import sqlite3 as _sqlite3
    clean_scan = normalize_scan_barcode(scanned_barcode)["lookup"]
    connection = _sqlite3.connect(WALMART_ORDER_DB_PATH, timeout=30)
    connection.row_factory = _sqlite3.Row
    try:
        line = connection.execute(
            "SELECT * FROM walmart_order_lines WHERE order_line_id=?", (order_line_id,)
        ).fetchone()
        inventory = connection.execute(
            "SELECT * FROM inventory WHERE inventory_id=?", (inventory_id,)
        ).fetchone()
        if line is None or inventory is None:
            raise ValueError("The order line or selected inventory record was not found.")
        product_barcodes = connection.execute(
            "SELECT barcode FROM product_barcodes WHERE product_id=?",
            (inventory["product_id"],),
        ).fetchall()
        valid_scans = {
            normalize_scan_barcode(row[0])["lookup"] for row in product_barcodes if row[0]
        }
        if clean_scan not in valid_scans:
            raise ValueError("Scanned barcode does not match the selected BrooksHouse product.")
        needed = int(line["quantity"] or 0)
        pulled = int(line["pulled_quantity"] or 0)
        available = int(inventory["quantity_on_hand"] or 0) - int(inventory["quantity_reserved"] or 0)
        if available < needed:
            raise ValueError("The selected location/tote does not have enough available inventory.")
        if pulled < needed:
            pulled += 1
            connection.execute(
                """
                INSERT INTO walmart_order_allocations (
                    order_line_id, inventory_id, planned_quantity, pulled_quantity
                ) VALUES (?, ?, 0, 1)
                ON CONFLICT(order_line_id, inventory_id) DO UPDATE SET
                    pulled_quantity=pulled_quantity + 1
                """,
                (order_line_id, inventory_id),
            )
        connection.execute(
            """
            UPDATE walmart_order_lines
            SET pulled_quantity=?, inventory_id=?, product_id=?
            WHERE order_line_id=?
            """,
            (pulled, inventory_id, inventory["product_id"], order_line_id),
        )
        remaining = connection.execute(
            "SELECT COUNT(*) FROM walmart_order_lines WHERE purchase_order_id=? AND pulled_quantity < quantity",
            (line["purchase_order_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE walmart_orders SET local_status=? WHERE purchase_order_id=?",
            ("pulled" if remaining == 0 else "pulling", line["purchase_order_id"]),
        )
        connection.commit()
        url = f"/channels/walmart/orders/{line['purchase_order_id']}?message=" + quote_plus(
            f"Pull confirmed ({pulled} of {needed})."
        )
    except Exception as error:
        connection.rollback()
        order_id = line["purchase_order_id"] if 'line' in locals() and line is not None else ""
        url = f"/channels/walmart/orders/{order_id}?error=" + quote_plus(str(error))
    finally:
        connection.close()
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/channels/walmart/orders/{purchase_order_id}/pack")
def walmart_pack_order(purchase_order_id: str):
    from urllib.parse import quote_plus
    import sqlite3 as _sqlite3
    connection = _sqlite3.connect(WALMART_ORDER_DB_PATH, timeout=30)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM walmart_order_lines WHERE purchase_order_id=? AND pulled_quantity < quantity",
            (purchase_order_id,),
        ).fetchone()[0]
        if remaining:
            raise ValueError("Every order line must be pulled before packing.")
        connection.execute(
            "UPDATE walmart_orders SET local_status='packed', packed_at=? WHERE purchase_order_id=?",
            (datetime.now().astimezone().isoformat(), purchase_order_id),
        )
        connection.commit()
        url = f"/channels/walmart/orders/{purchase_order_id}?message=" + quote_plus(f"Order {purchase_order_id} packed.")
    except Exception as error:
        connection.rollback()
        url = f"/channels/walmart/orders/{purchase_order_id}?error=" + quote_plus(str(error))
    finally:
        connection.close()
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/channels/walmart/orders/{purchase_order_id}/ship")
def walmart_ship_order(
    purchase_order_id: str,
    carrier: str = Form(...),
    tracking_number: str = Form(...),
    database: Session = Depends(get_database),
):
    from urllib.parse import quote_plus
    import sqlite3 as _sqlite3
    clean_carrier = carrier.strip()
    clean_tracking = tracking_number.strip()
    if not clean_carrier or not clean_tracking:
        return RedirectResponse(
            url=f"/channels/walmart/orders/{purchase_order_id}?error=Carrier+and+tracking+number+are+required.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    ensure_order_tables()
    connection = _sqlite3.connect(WALMART_ORDER_DB_PATH, timeout=30)
    connection.row_factory = _sqlite3.Row
    try:
        order = connection.execute(
            "SELECT * FROM walmart_orders WHERE purchase_order_id=?", (purchase_order_id,)
        ).fetchone()
        lines = connection.execute(
            "SELECT * FROM walmart_order_lines WHERE purchase_order_id=? ORDER BY order_line_id",
            (purchase_order_id,),
        ).fetchall()
        if order is None or not lines:
            raise ValueError("Walmart order was not found locally.")
        if order["local_status"] not in {"packed", "staged"}:
            raise ValueError("Order must be packed before shipment confirmation.")
        allocations_by_line = {}
        for line in lines:
            allocations = connection.execute(
                """
                SELECT * FROM walmart_order_allocations
                WHERE order_line_id=? AND pulled_quantity > 0
                ORDER BY allocation_id
                """,
                (line["order_line_id"],),
            ).fetchall()
            allocations_by_line[line["order_line_id"]] = allocations
            if int(line["pulled_quantity"] or 0) < int(line["quantity"] or 0):
                raise ValueError("Every item must be scanned and tied to inventory before shipping.")
            if sum(int(item["pulled_quantity"] or 0) for item in allocations) != int(line["quantity"] or 0):
                raise ValueError(f"Source allocation is incomplete for line {line['line_number']}.")
            for allocation in allocations:
                inventory = database.get(Inventory, int(allocation["inventory_id"]))
                if inventory is None or int(inventory.quantity_on_hand or 0) < int(allocation["pulled_quantity"] or 0):
                    raise ValueError(f"Inventory changed for line {line['line_number']}; review before shipping.")

        walmart_request(
            "POST",
            f"/v3/orders/{purchase_order_id}/shipping",
            payload=shipment_payload(lines, clean_carrier, clean_tracking),
        )

        # Walmart has accepted the shipment. Record that state before
        # touching local inventory so a later local error cannot cause
        # the shipment API call to be submitted a second time.
        connection.execute(
            """
            UPDATE walmart_orders SET local_status='shipment_submitted',
                shipped_at=?, carrier=?, tracking_number=?
            WHERE purchase_order_id=?
            """,
            (
                datetime.now().astimezone().isoformat(),
                clean_carrier,
                clean_tracking,
                purchase_order_id,
            ),
        )
        connection.commit()

        for line in lines:
            for allocation in allocations_by_line[line["order_line_id"]]:
                inventory = database.get(Inventory, int(allocation["inventory_id"]))
                quantity = int(allocation["pulled_quantity"] or 0)
                inventory.quantity_on_hand = int(inventory.quantity_on_hand or 0) - quantity
                database.add(
                    InventoryTransaction(
                        product=inventory.product,
                        location=inventory.location,
                        container_id=inventory.container_id,
                        transaction_type="walmart_order",
                        quantity_change=-quantity,
                        unit_cost=inventory.product.average_cost,
                        reference_number=purchase_order_id,
                        notes=f"Walmart shipment. Carrier: {clean_carrier}. Tracking: {clean_tracking}.",
                    )
                )
        database.commit()
        connection.execute(
            """
            UPDATE walmart_orders SET local_status='shipped', shipped_at=?,
                carrier=?, tracking_number=? WHERE purchase_order_id=?
            """,
            (datetime.now().astimezone().isoformat(), clean_carrier, clean_tracking, purchase_order_id),
        )
        connection.commit()
        url = f"/channels/walmart/orders/{purchase_order_id}?message=" + quote_plus(
            f"Order {purchase_order_id} shipped and BrooksHouse inventory deducted."
        )
    except Exception as error:
        database.rollback()
        connection.rollback()
        url = f"/channels/walmart/orders/{purchase_order_id}?error=" + quote_plus(str(error))
    finally:
        connection.close()
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)

# ============================================================
# BROOKSHOUSE WEB PUSH NOTIFICATIONS
# ============================================================
from threading import Thread
from time import sleep

from app.services.web_push_notifications import (
    ensure_push_tables,
    ensure_vapid_keys,
    public_vapid_key,
    remove_subscription,
    save_subscription,
    send_notification,
    subscription_summary,
)


class WebPushSubscriptionRequest(BaseModel):
    subscription: dict
    device_name: str | None = None


class WebPushUnsubscribeRequest(BaseModel):
    endpoint: str


@app.get("/tools/notifications", response_class=HTMLResponse)
def web_push_notifications_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="web_push_notifications.html",
        context={
            "summary": subscription_summary(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/api/notifications/public-key")
def web_push_public_key():
    return {"public_key": public_vapid_key()}


@app.post("/api/notifications/subscribe")
def web_push_subscribe(payload: WebPushSubscriptionRequest):
    save_subscription(payload.subscription, payload.device_name or "")
    return {"success": True}


@app.post("/api/notifications/unsubscribe")
def web_push_unsubscribe(payload: WebPushUnsubscribeRequest):
    remove_subscription(payload.endpoint)
    return {"success": True}


@app.post("/tools/notifications/test")
def web_push_test():
    from urllib.parse import quote_plus
    try:
        result = send_notification(
            "BrooksHouse test alert Ã°Å¸â€â€",
            "Web push is connected and ready for marketplace order notifications.",
            "/tools/notifications",
            "test",
        )
        message = f"Test sent to {result['delivered']} device(s); {result['failed']} failed."
        url = "/tools/notifications?message=" + quote_plus(message)
    except Exception as error:
        url = "/tools/notifications?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/notifications/service-worker.js")
def web_push_service_worker():
    from fastapi.responses import FileResponse
    response = FileResponse(
        APP_DIRECTORY.parent / "service-worker.js",
        media_type="application/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


def _walmart_order_ids() -> set[str]:
    ensure_order_tables()
    connection = sqlite3.connect(WALMART_ORDER_DB_PATH, timeout=30)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT purchase_order_id FROM walmart_orders"
            ).fetchall()
        }
    finally:
        connection.close()


def _automatic_order_notification_loop():
    # Wait for FastAPI to finish startup before the first Walmart check.
    sleep(20)
    while True:
        try:
            before = _walmart_order_ids()
            sync_orders(3)
            new_orders = sorted(_walmart_order_ids() - before)
            if new_orders:
                if len(new_orders) == 1:
                    body = f"New Walmart order {new_orders[0]} is ready for review and picking."
                else:
                    body = f"{len(new_orders)} new Walmart orders are ready for review and picking."
                send_notification(
                    "New Walmart order" if len(new_orders) == 1 else "New Walmart orders",
                    body,
                    "/channels/walmart/orders",
                    "walmart_new_order",
                )
        except Exception as error:
            print(f"BrooksHouse notification check skipped: {error}")
        sleep(300)


@app.on_event("startup")
def start_web_push_notifications():
    if not should_run_background_jobs():
        return
    ensure_push_tables()
    ensure_vapid_keys()
    if not getattr(app.state, "web_push_monitor_started", False):
        app.state.web_push_monitor_started = True
        Thread(
            target=_automatic_order_notification_loop,
            name="brookshouse-web-push",
            daemon=True,
        ).start()


# ============================================================
# BROOKSHOUSE DAILY RECAP NOTIFICATIONS
# ============================================================
from app.services.web_push_notifications import (
    build_daily_recap,
    maybe_send_daily_recaps,
    save_device_preferences,
    save_push_settings,
)


@app.post("/tools/notifications/settings")
def web_push_save_recap_settings(
    morning_time: str = Form(...),
    evening_time: str = Form(...),
    morning_enabled: bool = Form(False),
    evening_enabled: bool = Form(False),
):
    from urllib.parse import quote_plus
    try:
        save_push_settings(
            morning_enabled,
            morning_time,
            evening_enabled,
            evening_time,
        )
        url = "/tools/notifications?message=" + quote_plus(
            "Daily recap schedule saved."
        )
    except Exception as error:
        url = "/tools/notifications?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/tools/notifications/devices/{subscription_id}")
def web_push_save_device_settings(
    subscription_id: int,
    notify_new_orders: bool = Form(False),
    notify_morning: bool = Form(False),
    notify_evening: bool = Form(False),
):
    from urllib.parse import quote_plus
    try:
        save_device_preferences(
            subscription_id,
            notify_new_orders,
            notify_morning,
            notify_evening,
        )
        url = "/tools/notifications?message=" + quote_plus(
            "Device notification choices saved."
        )
    except Exception as error:
        url = "/tools/notifications?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/tools/notifications/recap/{period}")
def web_push_send_recap_now(period: str):
    from urllib.parse import quote_plus
    if period not in {"morning", "evening"}:
        raise HTTPException(status_code=404, detail="Unknown recap type.")
    try:
        title, body = build_daily_recap(period)
        result = send_notification(
            title,
            body,
            "/channels/orders",
            f"{period}_recap",
        )
        url = "/tools/notifications?message=" + quote_plus(
            f"{title} sent to {result['delivered']} device(s)."
        )
    except Exception as error:
        url = "/tools/notifications?error=" + quote_plus(str(error))
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _daily_recap_notification_loop():
    sleep(30)
    while True:
        try:
            maybe_send_daily_recaps()
        except Exception as error:
            print(f"BrooksHouse daily recap check skipped: {error}")
        sleep(60)


@app.on_event("startup")
def start_daily_recap_notifications():
    if not should_run_background_jobs():
        return
    if not getattr(app.state, "daily_recap_monitor_started", False):
        app.state.daily_recap_monitor_started = True
        Thread(
            target=_daily_recap_notification_loop,
            name="brookshouse-daily-recaps",
            daemon=True,
        ).start()


# ============================================================
# BROOKSHOUSE INSTALLABLE PWA
# ============================================================
@app.get("/install", response_class=HTMLResponse)
def install_brookshouse_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="install_app.html",
        context={},
    )



# BROOKSHOUSE KIDS HELPER INSTALL
from app.kids_helper import install_kids_helper
install_kids_helper(app)



# BROOKSHOUSE ROLE ACCESS CONTROL
from app.access_control import install_access_control
install_access_control(app)


# BROOKSHOUSE SHARED STORE + INVENTORY LOCATION MAP
from app.store_map import install_store_map
install_store_map(app)



# BROOKSHOUSE SHOPIFY SALES + OPERATIONS WORK QUEUE
from app.shopify_operations import install_shopify_operations
install_shopify_operations(app)



# BROOKSHOUSE CHANNEL PERFORMANCE DASHBOARD
from app.channel_performance import install_channel_performance
install_channel_performance(app)

# BROOKSHOUSE SALES DASHBOARD INSTALL
from app.services.sales_dashboard import install_sales_dashboard
install_sales_dashboard(app)

# BROOKSHOUSE OFFLINE INVENTORY + QUEUED SYNC
from app.services.offline_mode import install_offline_mode
install_offline_mode(app, templates)



# BROOKSHOUSE INVENTORY ACTIVITY / TRANSACTION HISTORY
from app.services.inventory_activity import install_inventory_activity
install_inventory_activity(app, templates)

# === BROOKSHOUSE LOCATION MASTER PHASE 1B START ===

def _location_master_require_manager(request: Request):
    auth_user = getattr(request.state, "auth_user", None)
    if auth_user is not None and getattr(auth_user, "role", "") not in {"owner_admin", "manager"}:
        raise HTTPException(status_code=403, detail="Owner/admin or manager access is required.")
    return auth_user


def _location_master_clean_code(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().upper())
    return cleaned.strip("-")


def _location_master_component(prefix: str, value: str) -> str:
    cleaned = _location_master_clean_code(value)
    if not cleaned:
        return ""
    if cleaned.startswith(prefix):
        return cleaned
    if cleaned.isdigit():
        return f"{prefix}{int(cleaned):02d}"
    return f"{prefix}{cleaned}"


def _location_master_build_code(area_code: str, zone_code: str, section: str = "", level: str = "", position: str = "") -> str:
    parts = [_location_master_clean_code(area_code), _location_master_clean_code(zone_code)]
    for prefix, value in (("S", section), ("L", level), ("P", position)):
        component = _location_master_component(prefix, value)
        if component:
            parts.append(component)
    return "-".join(part for part in parts if part)


@app.get("/admin/location-master", response_class=HTMLResponse)
def location_master_page(request: Request, area_id: str = "", message: str = "", error: str = ""):
    _location_master_require_manager(request)
    parsed_area_id = int(area_id) if str(area_id or "").strip().isdigit() else None

    with engine.connect() as connection:
        site = connection.execute(text("""
            SELECT * FROM inventory_sites WHERE active = 1 ORDER BY site_id LIMIT 1
        """)).mappings().first()

        areas = connection.execute(text("""
            SELECT a.*,
                   COUNT(DISTINCT z.zone_id) AS zone_count,
                   COUNT(DISTINCT lm.exact_location_id) AS location_count
            FROM inventory_areas AS a
            LEFT JOIN inventory_zones AS z ON z.area_id = a.area_id
            LEFT JOIN inventory_location_master AS lm ON lm.zone_id = z.zone_id
            GROUP BY a.area_id
            ORDER BY a.sequence, a.area_name
        """)).mappings().all()

        if parsed_area_id is None and areas:
            parsed_area_id = int(areas[0]["area_id"])

        selected_area = None
        zones = []
        locations = []

        if parsed_area_id is not None:
            selected_area = connection.execute(
                text("SELECT * FROM inventory_areas WHERE area_id=:area_id"),
                {"area_id": parsed_area_id},
            ).mappings().first()

            zones = connection.execute(text("""
                SELECT z.*, COUNT(lm.exact_location_id) AS location_count
                FROM inventory_zones AS z
                LEFT JOIN inventory_location_master AS lm ON lm.zone_id = z.zone_id
                WHERE z.area_id=:area_id
                GROUP BY z.zone_id
                ORDER BY z.sequence, z.zone_name
            """), {"area_id": parsed_area_id}).mappings().all()

            locations = connection.execute(text("""
                SELECT lm.*, z.zone_code, z.zone_name, a.area_id, a.area_code, a.area_name
                FROM inventory_location_master AS lm
                JOIN inventory_zones AS z ON z.zone_id = lm.zone_id
                JOIN inventory_areas AS a ON a.area_id = z.area_id
                WHERE a.area_id=:area_id
                ORDER BY z.sequence, z.zone_name, lm.sequence, lm.location_code
            """), {"area_id": parsed_area_id}).mappings().all()

    return templates.TemplateResponse(
        request=request,
        name="location_master.html",
        context={
            "site": site,
            "areas": areas,
            "selected_area": selected_area,
            "zones": zones,
            "locations": locations,
            "message": message,
            "error": error,
        },
    )


@app.post("/admin/location-master/zones")
def location_master_create_zone(
    request: Request,
    area_id: int = Form(...),
    zone_code: str = Form(...),
    zone_name: str = Form(...),
    zone_type: str = Form(""),
    description: str = Form(""),
):
    _location_master_require_manager(request)
    clean_code = _location_master_clean_code(zone_code)
    clean_name = str(zone_name or "").strip()

    if not clean_code or not clean_name:
        return RedirectResponse(
            url=f"/admin/location-master?area_id={area_id}&error=Zone+code+and+name+are+required",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        with engine.begin() as connection:
            if connection.execute(text("SELECT 1 FROM inventory_areas WHERE area_id=:area_id"), {"area_id": area_id}).first() is None:
                raise ValueError("Area not found.")
            sequence = connection.execute(text("""
                SELECT COALESCE(MAX(sequence),0)+10 FROM inventory_zones WHERE area_id=:area_id
            """), {"area_id": area_id}).scalar_one()
            connection.execute(text("""
                INSERT INTO inventory_zones
                    (area_id,zone_code,zone_name,zone_type,description,sequence,active)
                VALUES
                    (:area_id,:zone_code,:zone_name,:zone_type,:description,:sequence,1)
            """), {
                "area_id": area_id,
                "zone_code": clean_code,
                "zone_name": clean_name,
                "zone_type": _location_master_clean_code(zone_type) or None,
                "description": str(description or "").strip() or None,
                "sequence": sequence,
            })
    except Exception as exc:
        return RedirectResponse(
            url=f"/admin/location-master?area_id={area_id}&error={quote_plus(str(exc))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return RedirectResponse(
        url=f"/admin/location-master?area_id={area_id}&message=Zone+created",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/location-master/zones/{zone_id}/edit")
def location_master_edit_zone(
    zone_id: int,
    request: Request,
    zone_code: str = Form(...),
    zone_name: str = Form(...),
    zone_type: str = Form(""),
    description: str = Form(""),
):
    _location_master_require_manager(request)
    with engine.begin() as connection:
        row = connection.execute(text("SELECT area_id FROM inventory_zones WHERE zone_id=:zone_id"), {"zone_id": zone_id}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Zone not found.")
        area_id = int(row["area_id"])
        try:
            connection.execute(text("""
                UPDATE inventory_zones
                SET zone_code=:zone_code, zone_name=:zone_name, zone_type=:zone_type,
                    description=:description, updated_at=CURRENT_TIMESTAMP
                WHERE zone_id=:zone_id
            """), {
                "zone_id": zone_id,
                "zone_code": _location_master_clean_code(zone_code),
                "zone_name": str(zone_name or "").strip(),
                "zone_type": _location_master_clean_code(zone_type) or None,
                "description": str(description or "").strip() or None,
            })
        except Exception as exc:
            return RedirectResponse(
                url=f"/admin/location-master?area_id={area_id}&error={quote_plus(str(exc))}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
    return RedirectResponse(url=f"/admin/location-master?area_id={area_id}&message=Zone+updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/location-master/zones/{zone_id}/toggle")
def location_master_toggle_zone(zone_id: int, request: Request):
    _location_master_require_manager(request)
    with engine.begin() as connection:
        row = connection.execute(text("SELECT area_id FROM inventory_zones WHERE zone_id=:zone_id"), {"zone_id": zone_id}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Zone not found.")
        area_id = int(row["area_id"])
        connection.execute(text("""
            UPDATE inventory_zones
            SET active=CASE WHEN active=1 THEN 0 ELSE 1 END, updated_at=CURRENT_TIMESTAMP
            WHERE zone_id=:zone_id
        """), {"zone_id": zone_id})
    return RedirectResponse(url=f"/admin/location-master?area_id={area_id}&message=Zone+status+updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/location-master/locations")
def location_master_create_location(
    request: Request,
    zone_id: int = Form(...),
    location_code: str = Form(""),
    location_name: str = Form(""),
    location_type: str = Form("PICK"),
    barcode: str = Form(""),
    aisle: str = Form(""),
    section: str = Form(""),
    level: str = Form(""),
    position: str = Form(""),
    description: str = Form(""),
):
    _location_master_require_manager(request)
    with engine.begin() as connection:
        zone = connection.execute(text("""
            SELECT z.zone_id,z.zone_code,a.area_id,a.area_code
            FROM inventory_zones AS z
            JOIN inventory_areas AS a ON a.area_id=z.area_id
            WHERE z.zone_id=:zone_id
        """), {"zone_id": zone_id}).mappings().first()
        if zone is None:
            raise HTTPException(status_code=404, detail="Zone not found.")

        area_id = int(zone["area_id"])
        generated = _location_master_build_code(zone["area_code"], zone["zone_code"], section, level, position)
        final_code = _location_master_clean_code(location_code) or generated
        final_barcode = _location_master_clean_code(barcode) or f"LOC-{final_code}"
        sequence = connection.execute(text("""
            SELECT COALESCE(MAX(sequence),0)+10 FROM inventory_location_master WHERE zone_id=:zone_id
        """), {"zone_id": zone_id}).scalar_one()
        try:
            connection.execute(text("""
                INSERT INTO inventory_location_master
                    (zone_id,location_code,location_name,location_type,barcode,aisle,section,level,position,description,sequence,active)
                VALUES
                    (:zone_id,:location_code,:location_name,:location_type,:barcode,:aisle,:section,:level,:position,:description,:sequence,1)
            """), {
                "zone_id": zone_id,
                "location_code": final_code,
                "location_name": str(location_name or "").strip() or final_code,
                "location_type": _location_master_clean_code(location_type) or "PICK",
                "barcode": final_barcode,
                "aisle": str(aisle or "").strip() or None,
                "section": str(section or "").strip() or None,
                "level": str(level or "").strip() or None,
                "position": str(position or "").strip() or None,
                "description": str(description or "").strip() or None,
                "sequence": sequence,
            })
        except Exception as exc:
            return RedirectResponse(
                url=f"/admin/location-master?area_id={area_id}&error={quote_plus(str(exc))}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
    return RedirectResponse(url=f"/admin/location-master?area_id={area_id}&message=Exact+location+created", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/location-master/locations/{exact_location_id}/edit")
def location_master_edit_location(
    exact_location_id: int,
    request: Request,
    location_code: str = Form(...),
    location_name: str = Form(""),
    location_type: str = Form("PICK"),
    barcode: str = Form(""),
    aisle: str = Form(""),
    section: str = Form(""),
    level: str = Form(""),
    position: str = Form(""),
    description: str = Form(""),
):
    _location_master_require_manager(request)
    with engine.begin() as connection:
        row = connection.execute(text("""
            SELECT a.area_id
            FROM inventory_location_master AS lm
            JOIN inventory_zones AS z ON z.zone_id=lm.zone_id
            JOIN inventory_areas AS a ON a.area_id=z.area_id
            WHERE lm.exact_location_id=:exact_location_id
        """), {"exact_location_id": exact_location_id}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Exact location not found.")
        area_id = int(row["area_id"])
        final_code = _location_master_clean_code(location_code)
        final_barcode = _location_master_clean_code(barcode) or f"LOC-{final_code}"
        try:
            connection.execute(text("""
                UPDATE inventory_location_master
                SET location_code=:location_code, location_name=:location_name, location_type=:location_type,
                    barcode=:barcode, aisle=:aisle, section=:section, level=:level, position=:position,
                    description=:description, updated_at=CURRENT_TIMESTAMP
                WHERE exact_location_id=:exact_location_id
            """), {
                "exact_location_id": exact_location_id,
                "location_code": final_code,
                "location_name": str(location_name or "").strip() or final_code,
                "location_type": _location_master_clean_code(location_type) or "PICK",
                "barcode": final_barcode,
                "aisle": str(aisle or "").strip() or None,
                "section": str(section or "").strip() or None,
                "level": str(level or "").strip() or None,
                "position": str(position or "").strip() or None,
                "description": str(description or "").strip() or None,
            })
        except Exception as exc:
            return RedirectResponse(
                url=f"/admin/location-master?area_id={area_id}&error={quote_plus(str(exc))}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
    return RedirectResponse(url=f"/admin/location-master?area_id={area_id}&message=Location+updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/location-master/locations/{exact_location_id}/toggle")
def location_master_toggle_location(exact_location_id: int, request: Request):
    _location_master_require_manager(request)
    with engine.begin() as connection:
        row = connection.execute(text("""
            SELECT a.area_id
            FROM inventory_location_master AS lm
            JOIN inventory_zones AS z ON z.zone_id=lm.zone_id
            JOIN inventory_areas AS a ON a.area_id=z.area_id
            WHERE lm.exact_location_id=:exact_location_id
        """), {"exact_location_id": exact_location_id}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Exact location not found.")
        area_id = int(row["area_id"])
        connection.execute(text("""
            UPDATE inventory_location_master
            SET active=CASE WHEN active=1 THEN 0 ELSE 1 END, updated_at=CURRENT_TIMESTAMP
            WHERE exact_location_id=:exact_location_id
        """), {"exact_location_id": exact_location_id})
    return RedirectResponse(url=f"/admin/location-master?area_id={area_id}&message=Location+status+updated", status_code=status.HTTP_303_SEE_OTHER)

# === BROOKSHOUSE LOCATION MASTER PHASE 1B END ===

# === BROOKSHOUSE LOCATION MASTER PHASE 1C START ===

def _lm1c_require_manager(request: Request):
    auth_user = getattr(request.state, "auth_user", None)
    if auth_user is not None and getattr(auth_user, "role", "") not in {"owner_admin", "manager"}:
        raise HTTPException(status_code=403, detail="Owner/admin or manager access is required.")
    return auth_user


def _lm1c_clean_code(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().upper())
    return cleaned.strip("-")


def _lm1c_component(prefix: str, value) -> str:
    cleaned = _lm1c_clean_code(str(value or ""))
    if not cleaned:
        return ""
    if cleaned.startswith(prefix):
        return cleaned
    if cleaned.isdigit():
        return f"{prefix}{int(cleaned):02d}"
    return f"{prefix}{cleaned}"


def _lm1c_build_code(area_code: str, zone_code: str, section, level, position=None) -> str:
    parts = [_lm1c_clean_code(area_code), _lm1c_clean_code(zone_code)]
    for prefix, value in (("S", section), ("L", level), ("P", position)):
        component = _lm1c_component(prefix, value)
        if component:
            parts.append(component)
    return "-".join(part for part in parts if part)


def _lm1c_load_batch(connection, batch_id: int):
    batch = connection.execute(
        text("""
            SELECT
                b.*,
                a.area_name,
                a.area_code,
                z.zone_name,
                z.zone_code
            FROM location_label_batches AS b
            LEFT JOIN inventory_areas AS a ON a.area_id = b.area_id
            LEFT JOIN inventory_zones AS z ON z.zone_id = b.zone_id
            WHERE b.batch_id = :batch_id
        """),
        {"batch_id": batch_id},
    ).mappings().first()

    if batch is None:
        return None, []

    rows = connection.execute(
        text("""
            SELECT
                lm.*,
                z.zone_code,
                z.zone_name,
                a.area_code,
                a.area_name
            FROM location_label_batch_items AS i
            JOIN inventory_location_master AS lm
              ON lm.exact_location_id = i.exact_location_id
            JOIN inventory_zones AS z
              ON z.zone_id = lm.zone_id
            JOIN inventory_areas AS a
              ON a.area_id = z.area_id
            WHERE i.batch_id = :batch_id
            ORDER BY z.sequence, lm.sequence, lm.location_code
        """),
        {"batch_id": batch_id},
    ).mappings().all()

    return batch, rows


def _lm1c_label_from_location(row) -> dict:
    detail = []
    if row.get("section"):
        detail.append(f"Section {row['section']}")
    if row.get("level"):
        detail.append(f"Level {row['level']}")
    if row.get("position"):
        detail.append(f"Position {row['position']}")
    detail.append(str(row.get("location_type") or "LOCATION"))

    barcode_value = str(row.get("barcode") or f"LOC-{row['location_code']}")

    return {
        "type": str(row.get("location_type") or "LOCATION"),
        "code": str(row.get("location_code") or ""),
        "location": f"{row.get('area_name') or ''} Â· {row.get('zone_name') or ''}".strip(" Â·"),
        "detail": " Â· ".join(detail),
        "barcode_value": barcode_value,
        "barcode_svg": build_code39_svg(barcode_value),
    }


@app.get("/admin/location-builder", response_class=HTMLResponse)
def location_builder_page(
    request: Request,
    area_id: str = "",
    batch_id: str = "",
    message: str = "",
    error: str = "",
):
    _lm1c_require_manager(request)

    parsed_area_id = int(area_id) if str(area_id or "").strip().isdigit() else None
    parsed_batch_id = int(batch_id) if str(batch_id or "").strip().isdigit() else None

    with engine.connect() as connection:
        areas = connection.execute(
            text("""
                SELECT
                    a.*,
                    COUNT(DISTINCT z.zone_id) AS zone_count,
                    COUNT(DISTINCT lm.exact_location_id) AS location_count
                FROM inventory_areas AS a
                LEFT JOIN inventory_zones AS z ON z.area_id = a.area_id
                LEFT JOIN inventory_location_master AS lm ON lm.zone_id = z.zone_id
                GROUP BY a.area_id
                ORDER BY a.sequence, a.area_name
            """)
        ).mappings().all()

        if parsed_area_id is None and areas:
            parsed_area_id = int(areas[0]["area_id"])

        selected_area = None
        zones = []
        locations = []
        recent_batches = []

        if parsed_area_id is not None:
            selected_area = connection.execute(
                text("SELECT * FROM inventory_areas WHERE area_id=:area_id"),
                {"area_id": parsed_area_id},
            ).mappings().first()

            zones = connection.execute(
                text("""
                    SELECT
                        z.*,
                        COUNT(lm.exact_location_id) AS location_count
                    FROM inventory_zones AS z
                    LEFT JOIN inventory_location_master AS lm
                      ON lm.zone_id = z.zone_id
                    WHERE z.area_id = :area_id
                    GROUP BY z.zone_id
                    ORDER BY z.sequence, z.zone_name
                """),
                {"area_id": parsed_area_id},
            ).mappings().all()

            locations = connection.execute(
                text("""
                    SELECT
                        lm.*,
                        z.zone_code,
                        z.zone_name,
                        a.area_code,
                        a.area_name
                    FROM inventory_location_master AS lm
                    JOIN inventory_zones AS z ON z.zone_id = lm.zone_id
                    JOIN inventory_areas AS a ON a.area_id = z.area_id
                    WHERE a.area_id = :area_id
                    ORDER BY z.sequence, lm.sequence, lm.location_code
                """),
                {"area_id": parsed_area_id},
            ).mappings().all()

            recent_batches = connection.execute(
                text("""
                    SELECT
                        b.*,
                        COUNT(i.batch_item_id) AS item_count,
                        z.zone_name,
                        z.zone_code
                    FROM location_label_batches AS b
                    LEFT JOIN location_label_batch_items AS i
                      ON i.batch_id = b.batch_id
                    LEFT JOIN inventory_zones AS z
                      ON z.zone_id = b.zone_id
                    WHERE b.area_id = :area_id
                    GROUP BY b.batch_id
                    ORDER BY b.batch_id DESC
                    LIMIT 20
                """),
                {"area_id": parsed_area_id},
            ).mappings().all()

        active_batch = None
        active_items = []
        if parsed_batch_id is not None:
            active_batch, active_items = _lm1c_load_batch(connection, parsed_batch_id)

    return templates.TemplateResponse(
        request=request,
        name="location_bulk_builder.html",
        context={
            "areas": areas,
            "selected_area": selected_area,
            "zones": zones,
            "locations": locations,
            "recent_batches": recent_batches,
            "active_batch": active_batch,
            "active_items": active_items,
            "message": message,
            "error": error,
        },
    )


@app.post("/admin/location-builder/range")
def location_builder_create_range(
    request: Request,
    zone_id: int = Form(...),
    section_start: int = Form(...),
    section_end: int = Form(...),
    level_start: int = Form(1),
    level_end: int = Form(1),
    use_levels: str = Form(""),
    use_positions: str = Form(""),
    position_start: int = Form(1),
    position_end: int = Form(1),
    location_type: str = Form("PICK"),
    batch_name: str = Form(""),
):
    _lm1c_require_manager(request)

    levels_enabled = str(use_levels or "").strip().lower() in {"1", "true", "yes", "on"}

    if section_start < 1:
        raise HTTPException(status_code=400, detail="Section numbers must be 1 or greater.")
    if section_end < section_start:
        raise HTTPException(status_code=400, detail="Section ending value cannot be below starting value.")
    if levels_enabled:
        if level_start < 1:
            raise HTTPException(status_code=400, detail="Level numbers must be 1 or greater.")
        if level_end < level_start:
            raise HTTPException(status_code=400, detail="Level ending value cannot be below starting value.")

    positions_enabled = str(use_positions or "").strip().lower() in {"1", "true", "yes", "on"}

    if positions_enabled:
        if position_start < 1 or position_end < position_start:
            raise HTTPException(status_code=400, detail="Position range is invalid.")
        positions = list(range(position_start, position_end + 1))
    else:
        positions = [None]

    sections = list(range(section_start, section_end + 1))
    levels = list(range(level_start, level_end + 1)) if levels_enabled else [None]
    requested_count = len(sections) * len(levels) * len(positions)

    if requested_count > 500:
        raise HTTPException(status_code=400, detail="Create 500 or fewer locations per range.")

    with engine.begin() as connection:
        zone = connection.execute(
            text("""
                SELECT
                    z.zone_id,
                    z.zone_code,
                    z.zone_name,
                    a.area_id,
                    a.area_code,
                    a.area_name
                FROM inventory_zones AS z
                JOIN inventory_areas AS a ON a.area_id = z.area_id
                WHERE z.zone_id = :zone_id
            """),
            {"zone_id": zone_id},
        ).mappings().first()

        if zone is None:
            raise HTTPException(status_code=404, detail="Zone not found.")

        area_id = int(zone["area_id"])
        location_type = _lm1c_clean_code(location_type) or "PICK"

        next_sequence = connection.execute(
            text("""
                SELECT COALESCE(MAX(sequence),0)
                FROM inventory_location_master
                WHERE zone_id=:zone_id
            """),
            {"zone_id": zone_id},
        ).scalar_one()

        created_ids = []
        skipped = 0

        for section_number in sections:
            for level_number in levels:
                for position_number in positions:
                    code = _lm1c_build_code(
                        zone["area_code"],
                        zone["zone_code"],
                        section_number,
                        level_number,
                        position_number,
                    )

                    existing = connection.execute(
                        text("""
                            SELECT exact_location_id
                            FROM inventory_location_master
                            WHERE location_code=:location_code
                        """),
                        {"location_code": code},
                    ).scalar_one_or_none()

                    if existing is not None:
                        skipped += 1
                        continue

                    next_sequence += 10
                    barcode = f"LOC-{code}"

                    result = connection.execute(
                        text("""
                            INSERT INTO inventory_location_master (
                                zone_id,
                                location_code,
                                location_name,
                                location_type,
                                barcode,
                                aisle,
                                section,
                                level,
                                position,
                                description,
                                sequence,
                                active
                            )
                            VALUES (
                                :zone_id,
                                :location_code,
                                :location_name,
                                :location_type,
                                :barcode,
                                :aisle,
                                :section,
                                :level,
                                :position,
                                :description,
                                :sequence,
                                1
                            )
                        """),
                        {
                            "zone_id": zone_id,
                            "location_code": code,
                            "location_name": code,
                            "location_type": location_type,
                            "barcode": barcode,
                            "aisle": zone["zone_code"],
                            "section": str(section_number),
                            "level": None if level_number is None else str(level_number),
                            "position": None if position_number is None else str(position_number),
                            "description": f"Generated in Location Builder for {zone['zone_name']}.",
                            "sequence": next_sequence,
                        },
                    )

                    created_id = result.lastrowid
                    if created_id is None:
                        created_id = connection.execute(
                            text("""
                                SELECT exact_location_id
                                FROM inventory_location_master
                                WHERE location_code=:location_code
                            """),
                            {"location_code": code},
                        ).scalar_one()

                    created_ids.append(int(created_id))

        final_batch_name = str(batch_name or "").strip()
        if not final_batch_name:
            final_batch_name = (
                f"{zone['area_code']} {zone['zone_code']} "
                f"S{section_start:02d}-S{section_end:02d}"
            )
            if levels_enabled:
                final_batch_name += f" L{level_start:02d}-L{level_end:02d}"
            else:
                final_batch_name += " SECTION-ONLY"
            if positions_enabled:
                final_batch_name += f" P{position_start:02d}-P{position_end:02d}"

        batch_result = connection.execute(
            text("""
                INSERT INTO location_label_batches (
                    batch_name,
                    area_id,
                    zone_id,
                    batch_source,
                    status,
                    created_count,
                    skipped_count,
                    created_at,
                    updated_at
                )
                VALUES (
                    :batch_name,
                    :area_id,
                    :zone_id,
                    'RANGE',
                    'SAVED',
                    :created_count,
                    :skipped_count,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            """),
            {
                "batch_name": final_batch_name,
                "area_id": area_id,
                "zone_id": zone_id,
                "created_count": len(created_ids),
                "skipped_count": skipped,
            },
        )
        batch_id = int(batch_result.lastrowid)

        for exact_location_id in created_ids:
            connection.execute(
                text("""
                    INSERT OR IGNORE INTO location_label_batch_items (
                        batch_id,
                        exact_location_id,
                        created_at
                    )
                    VALUES (
                        :batch_id,
                        :exact_location_id,
                        CURRENT_TIMESTAMP
                    )
                """),
                {
                    "batch_id": batch_id,
                    "exact_location_id": exact_location_id,
                },
            )

    message = f"{len(created_ids)} created Â· {skipped} already existed Â· label batch saved."

    return RedirectResponse(
        url=(
            f"/admin/location-builder?area_id={area_id}"
            f"&batch_id={batch_id}"
            f"&message={quote_plus(message)}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/location-builder/zone-batch")
def location_builder_zone_batch(
    request: Request,
    zone_id: int = Form(...),
):
    _lm1c_require_manager(request)

    with engine.begin() as connection:
        zone = connection.execute(
            text("""
                SELECT
                    z.zone_id,
                    z.zone_code,
                    z.zone_name,
                    a.area_id,
                    a.area_code
                FROM inventory_zones AS z
                JOIN inventory_areas AS a ON a.area_id = z.area_id
                WHERE z.zone_id=:zone_id
            """),
            {"zone_id": zone_id},
        ).mappings().first()

        if zone is None:
            raise HTTPException(status_code=404, detail="Zone not found.")

        exact_ids = connection.execute(
            text("""
                SELECT exact_location_id
                FROM inventory_location_master
                WHERE zone_id=:zone_id
                  AND active=1
                ORDER BY sequence, location_code
            """),
            {"zone_id": zone_id},
        ).scalars().all()

        if not exact_ids:
            raise HTTPException(status_code=400, detail="Zone has no active exact locations.")
        if len(exact_ids) > 500:
            raise HTTPException(status_code=400, detail="A saved/print batch can contain at most 500 labels.")

        batch_result = connection.execute(
            text("""
                INSERT INTO location_label_batches (
                    batch_name,
                    area_id,
                    zone_id,
                    batch_source,
                    status,
                    created_count,
                    skipped_count,
                    created_at,
                    updated_at
                )
                VALUES (
                    :batch_name,
                    :area_id,
                    :zone_id,
                    'ZONE',
                    'SAVED',
                    :created_count,
                    0,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            """),
            {
                "batch_name": f"{zone['area_code']} {zone['zone_code']} Full Zone",
                "area_id": int(zone["area_id"]),
                "zone_id": zone_id,
                "created_count": len(exact_ids),
            },
        )
        batch_id = int(batch_result.lastrowid)

        for exact_location_id in exact_ids:
            connection.execute(
                text("""
                    INSERT OR IGNORE INTO location_label_batch_items (
                        batch_id,
                        exact_location_id,
                        created_at
                    )
                    VALUES (
                        :batch_id,
                        :exact_location_id,
                        CURRENT_TIMESTAMP
                    )
                """),
                {
                    "batch_id": batch_id,
                    "exact_location_id": int(exact_location_id),
                },
            )

        area_id = int(zone["area_id"])

    return RedirectResponse(
        url=f"/admin/location-builder?area_id={area_id}&batch_id={batch_id}&message=Whole+zone+label+batch+saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/location-builder/selection-batch")
async def location_builder_selection_batch(request: Request):
    _lm1c_require_manager(request)

    form = await request.form()
    exact_ids = [
        int(value)
        for value in form.getlist("exact_location_id")
        if str(value).strip().isdigit()
    ]

    if not exact_ids:
        raise HTTPException(status_code=400, detail="Select at least one exact location.")
    if len(exact_ids) > 500:
        raise HTTPException(status_code=400, detail="Select 500 or fewer locations.")

    with engine.begin() as connection:
        rows = connection.execute(
            text("""
                SELECT
                    lm.exact_location_id,
                    a.area_id,
                    z.zone_id,
                    a.area_code,
                    z.zone_code
                FROM inventory_location_master AS lm
                JOIN inventory_zones AS z ON z.zone_id = lm.zone_id
                JOIN inventory_areas AS a ON a.area_id = z.area_id
                WHERE lm.exact_location_id IN (
                    SELECT value
                    FROM json_each(:ids_json)
                )
            """),
            {"ids_json": json.dumps(exact_ids)},
        ).mappings().all()

        if not rows:
            raise HTTPException(status_code=404, detail="Selected locations were not found.")

        area_id = int(rows[0]["area_id"])
        zone_id = int(rows[0]["zone_id"])

        batch_result = connection.execute(
            text("""
                INSERT INTO location_label_batches (
                    batch_name,
                    area_id,
                    zone_id,
                    batch_source,
                    status,
                    created_count,
                    skipped_count,
                    created_at,
                    updated_at
                )
                VALUES (
                    :batch_name,
                    :area_id,
                    :zone_id,
                    'SELECTION',
                    'SAVED',
                    :created_count,
                    0,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            """),
            {
                "batch_name": f"Selected Locations {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "area_id": area_id,
                "zone_id": zone_id,
                "created_count": len(rows),
            },
        )
        batch_id = int(batch_result.lastrowid)

        for exact_location_id in exact_ids:
            connection.execute(
                text("""
                    INSERT OR IGNORE INTO location_label_batch_items (
                        batch_id,
                        exact_location_id,
                        created_at
                    )
                    VALUES (
                        :batch_id,
                        :exact_location_id,
                        CURRENT_TIMESTAMP
                    )
                """),
                {
                    "batch_id": batch_id,
                    "exact_location_id": exact_location_id,
                },
            )

    return RedirectResponse(
        url=f"/admin/location-builder?area_id={area_id}&batch_id={batch_id}&message=Selected+locations+saved+as+a+label+batch",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/admin/location-builder/batches/{batch_id}/print", response_class=HTMLResponse)
def location_builder_print_batch(
    batch_id: int,
    request: Request,
    database: Session = Depends(get_database),
):
    _lm1c_require_manager(request)

    with engine.connect() as connection:
        batch, rows = _lm1c_load_batch(connection, batch_id)

    if batch is None:
        raise HTTPException(status_code=404, detail="Label batch not found.")
    if not rows:
        raise HTTPException(status_code=400, detail="Label batch is empty.")
    if len(rows) > 500:
        raise HTTPException(status_code=400, detail="Print 500 or fewer labels at a time.")

    labels = [_lm1c_label_from_location(row) for row in rows]

    locations = database.scalars(
        select(InventoryLocation)
        .where(InventoryLocation.active.is_(True))
        .order_by(InventoryLocation.location_name)
    ).all()

    with engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE location_label_batches
                SET status='PRINT_READY',
                    updated_at=CURRENT_TIMESTAMP
                WHERE batch_id=:batch_id
            """),
            {"batch_id": batch_id},
        )

    return templates.TemplateResponse(
        request=request,
        name="pallet_labels.html",
        context={
            "locations": locations,
            "selected_location_id": None,
            "selected_location": None,
            "label_type": "LOCATION_MASTER",
            "label_type_name": f"Location Master Â· {batch['batch_name']}",
            "prefix": "",
            "positions": len(labels),
            "start_number": 1,
            "rack_number": 1,
            "shelves": 1,
            "custom_text": "",
            "labels": labels,
            "generated": True,
            "error": None,
        },
    )

# === BROOKSHOUSE LOCATION MASTER PHASE 1C END ===

# === BROOKSHOUSE LOCATION LABEL CONTROL PHASE 1D START ===

def _lm1d_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _lm1d_load_batch_rows(connection, batch_id: int):
    batch = connection.execute(
        text("""
            SELECT
                b.*,
                a.area_name,
                a.area_code,
                z.zone_name,
                z.zone_code
            FROM location_label_batches AS b
            LEFT JOIN inventory_areas AS a ON a.area_id = b.area_id
            LEFT JOIN inventory_zones AS z ON z.zone_id = b.zone_id
            WHERE b.batch_id = :batch_id
        """),
        {"batch_id": batch_id},
    ).mappings().first()

    if batch is None:
        return None, []

    rows = connection.execute(
        text("""
            SELECT
                lm.*,
                z.zone_code,
                z.zone_name,
                a.area_code,
                a.area_name
            FROM location_label_batch_items AS i
            JOIN inventory_location_master AS lm
              ON lm.exact_location_id = i.exact_location_id
            JOIN inventory_zones AS z
              ON z.zone_id = lm.zone_id
            JOIN inventory_areas AS a
              ON a.area_id = z.area_id
            WHERE i.batch_id = :batch_id
            ORDER BY lm.location_code
        """),
        {"batch_id": batch_id},
    ).mappings().all()

    return batch, rows


def _lm1d_sort_rows(rows, sort_by: str):
    sort_by = str(sort_by or "location").strip().lower()

    def n(value):
        try:
            return int(str(value or "").strip())
        except Exception:
            return 999999

    if sort_by == "section_level":
        return sorted(
            rows,
            key=lambda r: (
                n(r.get("section")),
                n(r.get("level")),
                n(r.get("position")),
                str(r.get("location_code") or ""),
            ),
        )
    if sort_by == "level_section":
        return sorted(
            rows,
            key=lambda r: (
                n(r.get("level")),
                n(r.get("section")),
                n(r.get("position")),
                str(r.get("location_code") or ""),
            ),
        )
    if sort_by == "reverse":
        return sorted(rows, key=lambda r: str(r.get("location_code") or ""), reverse=True)
    return sorted(rows, key=lambda r: str(r.get("location_code") or ""))


@app.get("/admin/location-builder/batches/{batch_id}/edit", response_class=HTMLResponse)
def location_label_batch_editor(
    batch_id: int,
    request: Request,
):
    _lm1c_require_manager(request)

    with engine.connect() as connection:
        batch, rows = _lm1d_load_batch_rows(connection, batch_id)

    if batch is None:
        raise HTTPException(status_code=404, detail="Label batch not found.")

    return templates.TemplateResponse(
        request=request,
        name="location_label_batch_editor.html",
        context={
            "batch": batch,
            "rows": rows,
        },
    )


@app.post("/admin/location-builder/batches/{batch_id}/preview", response_class=HTMLResponse)
async def location_label_batch_preview(
    batch_id: int,
    request: Request,
):
    _lm1c_require_manager(request)

    form = await request.form()

    selected_ids = {
        int(value)
        for value in form.getlist("exact_location_id")
        if str(value).strip().isdigit()
    }

    copies = max(1, min(int(form.get("copies") or 1), 20))
    start_position = max(1, int(form.get("start_position") or 1))
    sort_by = str(form.get("sort_by") or "section_level")
    label_size = str(form.get("label_size") or "small")

    show_area = _lm1d_bool(form.get("show_area"), True)
    show_zone = _lm1d_bool(form.get("show_zone"), True)
    show_purpose = _lm1d_bool(form.get("show_purpose"), True)
    show_detail = _lm1d_bool(form.get("show_detail"), True)
    show_barcode_text = _lm1d_bool(form.get("show_barcode_text"), True)

    code_scale = max(70, min(int(form.get("code_scale") or 100), 160))
    barcode_scale = max(60, min(int(form.get("barcode_scale") or 100), 160))

    with engine.connect() as connection:
        batch, rows = _lm1d_load_batch_rows(connection, batch_id)

    if batch is None:
        raise HTTPException(status_code=404, detail="Label batch not found.")

    if selected_ids:
        rows = [r for r in rows if int(r["exact_location_id"]) in selected_ids]

    if not rows:
        raise HTTPException(status_code=400, detail="Select at least one label.")

    rows = _lm1d_sort_rows(rows, sort_by)

    labels = []
    for row in rows:
        area_text = str(row.get("area_name") or "") if show_area else ""
        zone_text = str(row.get("zone_name") or "") if show_zone else ""

        location_parts = [p for p in [area_text, zone_text] if p]
        location_text = " Â· ".join(location_parts)

        detail_parts = []
        if show_detail:
            if row.get("section"):
                detail_parts.append(f"Section {row['section']}")
            if row.get("level"):
                detail_parts.append(f"Level {row['level']}")
            if row.get("position"):
                detail_parts.append(f"Position {row['position']}")

        if show_purpose:
            detail_parts.append(str(row.get("location_type") or "LOCATION"))

        barcode_value = str(row.get("barcode") or f"LOC-{row['location_code']}")

        label = {
            "type": str(row.get("location_type") or "LOCATION") if show_purpose else "",
            "code": str(row.get("location_code") or ""),
            "location": location_text,
            "detail": " Â· ".join(detail_parts),
            "barcode_value": barcode_value,
            "barcode_svg": build_code39_svg(barcode_value),
        }

        for _ in range(copies):
            labels.append(dict(label))

    if len(labels) > 500:
        raise HTTPException(status_code=400, detail="This preview would exceed 500 printed labels. Reduce copies or selection.")

    preset_map = {
        "full": {
            "columns": 2, "rows": 4,
            "label_width": 3.75, "label_height": 2.25,
            "margin_top": .3, "margin_right": .3,
            "margin_bottom": .3, "margin_left": .3,
            "column_gap": .12, "row_gap": .12,
        },
        "compact": {
            "columns": 2, "rows": 7,
            "label_width": 3.75, "label_height": 1.35,
            "margin_top": .3, "margin_right": .3,
            "margin_bottom": .3, "margin_left": .3,
            "column_gap": .12, "row_gap": .12,
        },
        "small": {
            "columns": 3, "rows": 10,
            "label_width": 2.625, "label_height": 1,
            "margin_top": .5, "margin_right": .1875,
            "margin_bottom": .5, "margin_left": .1875,
            "column_gap": .125, "row_gap": 0,
        },
    }

    preset = preset_map.get(label_size, preset_map["small"])

    return templates.TemplateResponse(
        request=request,
        name="location_label_controlled_print.html",
        context={
            "batch": batch,
            "labels": labels,
            "start_position": start_position,
            "label_size": label_size,
            "layout": preset,
            "show_barcode_text": show_barcode_text,
            "code_scale": code_scale,
            "barcode_scale": barcode_scale,
            "selected_count": len(rows),
            "copies": copies,
        },
    )


@app.post("/admin/location-builder/batches/{batch_id}/preset/save")
async def location_label_save_preset(
    batch_id: int,
    request: Request,
):
    _lm1c_require_manager(request)

    form = await request.form()

    name = str(form.get("preset_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required.")

    settings_json = json.dumps({
        "copies": max(1, min(int(form.get("copies") or 1), 20)),
        "start_position": max(1, int(form.get("start_position") or 1)),
        "sort_by": str(form.get("sort_by") or "section_level"),
        "label_size": str(form.get("label_size") or "small"),
        "show_area": _lm1d_bool(form.get("show_area"), True),
        "show_zone": _lm1d_bool(form.get("show_zone"), True),
        "show_purpose": _lm1d_bool(form.get("show_purpose"), True),
        "show_detail": _lm1d_bool(form.get("show_detail"), True),
        "show_barcode_text": _lm1d_bool(form.get("show_barcode_text"), True),
        "code_scale": max(70, min(int(form.get("code_scale") or 100), 160)),
        "barcode_scale": max(60, min(int(form.get("barcode_scale") or 100), 160)),
    })

    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO location_label_presets (
                    preset_name,
                    settings_json,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (
                    :preset_name,
                    :settings_json,
                    1,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(preset_name) DO UPDATE SET
                    settings_json=excluded.settings_json,
                    active=1,
                    updated_at=CURRENT_TIMESTAMP
            """),
            {
                "preset_name": name,
                "settings_json": settings_json,
            },
        )

    return RedirectResponse(
        url=f"/admin/location-builder/batches/{batch_id}/edit?preset_saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/api/location-label-presets")
def location_label_presets_api(
    request: Request,
):
    _lm1c_require_manager(request)

    with engine.connect() as connection:
        rows = connection.execute(
            text("""
                SELECT preset_id,preset_name,settings_json
                FROM location_label_presets
                WHERE active=1
                ORDER BY preset_name
            """)
        ).mappings().all()

    return [
        {
            "preset_id": row["preset_id"],
            "preset_name": row["preset_name"],
            "settings": json.loads(row["settings_json"]),
        }
        for row in rows
    ]

# === BROOKSHOUSE LOCATION LABEL CONTROL PHASE 1D END ===

# === BROOKSHOUSE CONTAINER TO SHELF START ===

def _ctos_storefront_location(connection):
    row = connection.execute(text("""
        SELECT location_id, location_name
        FROM inventory_locations
        WHERE active = 1
          AND (
                LOWER(location_name) LIKE '%storefront%'
             OR LOWER(location_type) = 'store'
          )
        ORDER BY
            CASE WHEN LOWER(location_name) LIKE '%storefront%' THEN 0 ELSE 1 END,
            location_id
        LIMIT 1
    """)).mappings().first()
    if row is None:
        raise ValueError("BrooksHouse Storefront inventory location was not found.")
    return row


def _ctos_exact_location(connection, raw_value: str):
    cleaned = _location_master_clean_code(str(raw_value or "").strip())
    if cleaned.startswith("LOC-"):
        cleaned = cleaned[4:]

    row = connection.execute(text("""
        SELECT
            lm.exact_location_id,
            lm.location_code,
            lm.location_name,
            lm.barcode,
            lm.section,
            lm.level,
            lm.position,
            z.zone_code,
            z.zone_name,
            a.area_code,
            a.area_name
        FROM inventory_location_master AS lm
        JOIN inventory_zones AS z ON z.zone_id = lm.zone_id
        JOIN inventory_areas AS a ON a.area_id = z.area_id
        WHERE lm.active = 1
          AND (
                UPPER(lm.location_code) = :code
             OR UPPER(COALESCE(lm.barcode, '')) = :barcode
          )
        LIMIT 1
    """), {
        "code": cleaned.upper(),
        "barcode": f"LOC-{cleaned}".upper(),
    }).mappings().first()

    if row is None:
        raise ValueError(f"Exact shelf/location '{raw_value}' was not found.")
    return row


def _ctos_primary_barcode(database, product_id: int) -> str:
    records = database.scalars(
        select(ProductBarcode).where(ProductBarcode.product_id == product_id)
    ).all()
    if not records:
        return ""
    primary = next(
        (r for r in records if bool(getattr(r, "is_primary", False))),
        records[0],
    )
    return str(primary.barcode or "")


def _ctos_source_rows(database, container_id: str, product_id: int | None = None):
    clean_container = normalize_container_id(container_id)
    query = select(Inventory)
    if product_id is not None:
        query = query.where(Inventory.product_id == product_id)

    rows = database.scalars(query).all()
    return [
        row for row in rows
        if normalize_container_id(getattr(row, "container_id", "")) == clean_container
        and int(getattr(row, "quantity_on_hand", 0) or 0) > 0
    ]


def _ctos_source_payload(database, container_id: str):
    clean_container = normalize_container_id(container_id)
    if not clean_container:
        raise ValueError("Scan or enter the old Container ID.")

    rows = _ctos_source_rows(database, clean_container)
    if not rows:
        raise ValueError(f"No positive inventory was found in {clean_container}.")

    grouped = {}
    for row in rows:
        product = database.get(Product, row.product_id)
        if product is None:
            continue
        entry = grouped.setdefault(
            int(product.product_id),
            {
                "product_id": int(product.product_id),
                "product_name": str(product.product_name or f"Product {product.product_id}"),
                "brand": str(product.brand or ""),
                "barcode": _ctos_primary_barcode(database, int(product.product_id)),
                "quantity": 0,
                "reserved": 0,
                "source_locations": set(),
            },
        )
        entry["quantity"] += int(row.quantity_on_hand or 0)
        entry["reserved"] += int(row.quantity_reserved or 0)
        location = getattr(row, "location", None)
        if location is not None:
            entry["source_locations"].add(str(location.location_name or ""))

    items = []
    for entry in grouped.values():
        entry["source_locations"] = sorted(x for x in entry["source_locations"] if x)
        items.append(entry)
    items.sort(key=lambda x: (x["product_name"].casefold(), x["product_id"]))

    return {
        "container_id": clean_container,
        "product_count": len(items),
        "unit_count": sum(int(x["quantity"]) for x in items),
        "items": items,
    }


def _ctos_set_product_exact_location(
    connection,
    product_id: int,
    exact_location_id: int,
    storefront_location_id: int,
):
    cols = connection.execute(text("PRAGMA table_info(product_location_settings)")).mappings().all()
    if not cols:
        raise ValueError("product_location_settings table was not found.")

    col_names = {str(c["name"]) for c in cols}
    if "product_id" not in col_names or "exact_location_id" not in col_names:
        raise ValueError(
            "product_location_settings does not contain product_id and exact_location_id."
        )

    existing = connection.execute(
        text("SELECT rowid AS _rowid, * FROM product_location_settings WHERE product_id=:product_id"),
        {"product_id": product_id},
    ).mappings().all()

    assignments = ["exact_location_id=:exact_location_id"]
    params = {
        "product_id": product_id,
        "exact_location_id": exact_location_id,
        "storefront_location_id": storefront_location_id,
    }

    if "location_id" in col_names:
        assignments.append("location_id=:storefront_location_id")
    if "active" in col_names:
        assignments.append("active=1")
    if "updated_at" in col_names:
        assignments.append("updated_at=CURRENT_TIMESTAMP")

    if existing:
        connection.execute(
            text(
                "UPDATE product_location_settings SET "
                + ", ".join(assignments)
                + " WHERE product_id=:product_id"
            ),
            params,
        )
        return

    insert_cols = ["product_id", "exact_location_id"]
    insert_vals = [":product_id", ":exact_location_id"]

    if "location_id" in col_names:
        insert_cols.append("location_id")
        insert_vals.append(":storefront_location_id")
    if "active" in col_names:
        insert_cols.append("active")
        insert_vals.append("1")
    if "created_at" in col_names:
        insert_cols.append("created_at")
        insert_vals.append("CURRENT_TIMESTAMP")
    if "updated_at" in col_names:
        insert_cols.append("updated_at")
        insert_vals.append("CURRENT_TIMESTAMP")

    supplied = set(insert_cols)
    missing_required = []
    for col in cols:
        name = str(col["name"])
        if (
            int(col["notnull"] or 0) == 1
            and col["dflt_value"] is None
            and int(col["pk"] or 0) == 0
            and name not in supplied
        ):
            missing_required.append(name)

    if missing_required:
        raise ValueError(
            "Cannot create product shelf assignment because these required "
            "product_location_settings fields are unknown: "
            + ", ".join(missing_required)
        )

    connection.execute(
        text(
            "INSERT INTO product_location_settings ("
            + ", ".join(insert_cols)
            + ") VALUES ("
            + ", ".join(insert_vals)
            + ")"
        ),
        params,
    )


@app.get("/inventory/container-to-shelf", response_class=HTMLResponse)
def container_to_shelf_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="container_to_shelf.html",
        context={},
    )


@app.get("/inventory/container-to-shelf/source")
def container_to_shelf_source(container_id: str):
    with SessionLocal() as database:
        try:
            return {"found": True, **_ctos_source_payload(database, container_id)}
        except ValueError as exc:
            return {"found": False, "error": str(exc)}


@app.get("/inventory/container-to-shelf/select")
def container_to_shelf_select(container_id: str, barcode: str):
    with SessionLocal() as database:
        try:
            clean_container = normalize_container_id(container_id)
            if not clean_container:
                raise ValueError("Scan the source Container ID first.")

            product, cleaned_barcode = find_store_product_by_scan(
                database=database,
                barcode=barcode,
            )

            rows = _ctos_source_rows(
                database,
                clean_container,
                int(product.product_id),
            )
            if not rows:
                raise ValueError(
                    f"{product.product_name} is not currently recorded in {clean_container}."
                )

            quantity = sum(int(r.quantity_on_hand or 0) for r in rows)
            reserved = sum(int(r.quantity_reserved or 0) for r in rows)

            return {
                "found": True,
                "product_id": int(product.product_id),
                "product_name": str(product.product_name or ""),
                "brand": str(product.brand or ""),
                "barcode": cleaned_barcode,
                "quantity": quantity,
                "reserved": reserved,
            }
        except ValueError as exc:
            return {"found": False, "error": str(exc)}


@app.get("/inventory/container-to-shelf/destination")
def container_to_shelf_destination(value: str):
    try:
        with engine.connect() as connection:
            row = _ctos_exact_location(connection, value)
            return {
                "found": True,
                "exact_location_id": int(row["exact_location_id"]),
                "location_code": row["location_code"],
                "location_name": row["location_name"],
                "barcode": row["barcode"],
                "zone_name": row["zone_name"],
                "area_name": row["area_name"],
                "section": row["section"],
                "level": row["level"],
                "position": row["position"],
            }
    except ValueError as exc:
        return {"found": False, "error": str(exc)}


@app.post("/inventory/container-to-shelf/commit")
async def container_to_shelf_commit(request: Request):
    payload = await request.json()
    source_container = normalize_container_id(payload.get("container_id"))
    destination_value = str(payload.get("destination") or "").strip()
    product_ids = payload.get("product_ids") or []

    if not source_container:
        return {"ok": False, "error": "Source Container ID is required."}
    if not destination_value:
        return {"ok": False, "error": "Scan the destination shelf/location."}

    try:
        product_ids = sorted({int(x) for x in product_ids})
    except (TypeError, ValueError):
        return {"ok": False, "error": "The selected product list is invalid."}

    if not product_ids:
        return {"ok": False, "error": "Scan at least one representative product barcode."}

    reference = "CONTAINER-SHELF-" + datetime.now().strftime("%Y%m%d-%H%M%S")

    with SessionLocal() as database:
        try:
            with engine.connect() as raw_connection:
                destination = _ctos_exact_location(raw_connection, destination_value)
                storefront = _ctos_storefront_location(raw_connection)

            storefront_location = database.get(
                InventoryLocation,
                int(storefront["location_id"]),
            )
            if storefront_location is None:
                raise ValueError("Storefront inventory location was not found.")

            validated = []
            for product_id in product_ids:
                product = database.get(Product, product_id)
                if product is None:
                    raise ValueError(f"Product ID {product_id} was not found.")

                source_rows = _ctos_source_rows(database, source_container, product_id)
                if not source_rows:
                    raise ValueError(
                        f"{product.product_name} is no longer recorded in {source_container}."
                    )

                reserved = sum(int(r.quantity_reserved or 0) for r in source_rows)
                if reserved:
                    raise ValueError(
                        f"{product.product_name} has {reserved} reserved unit(s). "
                        "That line was not moved so reserved inventory is not disturbed."
                    )

                quantity = sum(int(r.quantity_on_hand or 0) for r in source_rows)
                if quantity < 1:
                    raise ValueError(
                        f"{product.product_name} no longer has positive inventory in {source_container}."
                    )

                destination_rows = database.scalars(
                    select(Inventory).where(
                        Inventory.product_id == product_id,
                        Inventory.location_id == storefront_location.location_id,
                    )
                ).all()
                destination_inventory = next(
                    (
                        r for r in destination_rows
                        if not normalize_container_id(getattr(r, "container_id", ""))
                    ),
                    None,
                )

                validated.append(
                    {
                        "product": product,
                        "source_rows": source_rows,
                        "destination_inventory": destination_inventory,
                        "quantity": quantity,
                    }
                )

            moved = []
            total_units = 0

            for line in validated:
                product = line["product"]
                quantity = int(line["quantity"])
                destination_inventory = line["destination_inventory"]

                if destination_inventory is None:
                    destination_inventory = Inventory(
                        product=product,
                        location=storefront_location,
                        container_id="",
                        quantity_on_hand=0,
                        quantity_reserved=0,
                        reorder_level=0,
                    )
                    database.add(destination_inventory)
                    database.flush()

                destination_before = int(destination_inventory.quantity_on_hand or 0)

                for source_row in line["source_rows"]:
                    source_qty = int(source_row.quantity_on_hand or 0)
                    if source_qty <= 0:
                        continue
                    source_location = source_row.location
                    source_row.quantity_on_hand = 0

                    notes = (
                        f"Container-to-shelf reconciliation. Existing counted inventory "
                        f"moved from Container ID {source_container} to exact shelf "
                        f"{destination['location_code']}. Source inventory location: "
                        f"{source_location.location_name if source_location else source_row.location_id}. "
                        f"Quantity moved: {source_qty}."
                    )

                    database.add(
                        InventoryTransaction(
                            product=product,
                            location=source_location,
                            container_id=source_container,
                            transaction_type="transfer_out",
                            quantity_change=-source_qty,
                            unit_cost=product.average_cost,
                            reference_number=reference,
                            notes=notes,
                        )
                    )

                destination_inventory.quantity_on_hand = destination_before + quantity

                in_notes = (
                    f"Container-to-shelf reconciliation. Existing counted inventory "
                    f"moved from Container ID {source_container} to exact shelf "
                    f"{destination['location_code']}. Quantity moved: {quantity}. "
                    f"Storefront quantity: {destination_before} to "
                    f"{destination_before + quantity}."
                )
                database.add(
                    InventoryTransaction(
                        product=product,
                        location=storefront_location,
                        container_id="",
                        transaction_type="transfer_in",
                        quantity_change=quantity,
                        unit_cost=product.average_cost,
                        reference_number=reference,
                        notes=in_notes,
                    )
                )

                # Flush inventory/transactions, then update the exact shelf assignment
                # through the shared engine connection.
                database.flush()

                with engine.begin() as connection:
                    _ctos_set_product_exact_location(
                        connection,
                        int(product.product_id),
                        int(destination["exact_location_id"]),
                        int(storefront_location.location_id),
                    )

                moved.append(
                    {
                        "product_id": int(product.product_id),
                        "product_name": str(product.product_name or ""),
                        "quantity": quantity,
                    }
                )
                total_units += quantity

            database.commit()

            remaining = _ctos_source_payload(database, source_container) if _ctos_source_rows(database, source_container) else {
                "container_id": source_container,
                "product_count": 0,
                "unit_count": 0,
                "items": [],
            }

            return {
                "ok": True,
                "reference": reference,
                "destination": destination["location_code"],
                "moved_products": len(moved),
                "moved_units": total_units,
                "moved": moved,
                "remaining": remaining,
            }

        except ValueError as exc:
            database.rollback()
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            database.rollback()
            return {
                "ok": False,
                "error": f"The container-to-shelf move failed. Technical details: {exc}",
            }

# === BROOKSHOUSE CONTAINER TO SHELF END ===
