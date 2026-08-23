"""Amazon and Walmart status helpers for BrooksHouse products."""

import sqlite3
from typing import Any

from app.database_resolution import configured_sqlite_path

DATABASE_PATH = configured_sqlite_path()


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND lower(name) = lower(?)
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    """Return actual column names without assuming an import version."""
    if not table_exists(connection, table_name):
        return set()

    return {
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()
    }


def first_existing_column(
    columns: set[str],
    candidates: tuple[str, ...],
) -> str | None:
    """Choose a known-safe column name from supported schema variants."""
    return next(
        (candidate for candidate in candidates if candidate in columns),
        None,
    )


def selected_column(
    table_alias: str,
    columns: set[str],
    candidates: tuple[str, ...],
    result_alias: str,
) -> str:
    """Build a SELECT expression that always exposes result_alias."""
    column = first_existing_column(columns, candidates)

    if column is None:
        return f"NULL AS {result_alias}"

    return f'{table_alias}."{column}" AS {result_alias}'


def empty_amazon_status() -> dict[str, Any]:
    return {
        "linked": False,
        "approved": False,
        "status": "not_linked",
        "label": "Amazon Not Linked",
        "asin": None,
        "seller_sku": None,
        "price": None,
        "quantity": None,
        "inventory_status": None,
    }


def empty_walmart_status() -> dict[str, Any]:
    return {
        "linked": False,
        "approved": False,
        "status": "not_linked",
        "label": "Walmart Not Linked",
        "item_id": None,
        "seller_sku": None,
        "price": None,
        "quantity": None,
        "inventory_status": None,
    }


def get_amazon_status(
    connection: sqlite3.Connection,
    product_id: int,
) -> dict[str, Any]:
    if not table_exists(
        connection,
        "amazon_listings",
    ):
        return empty_amazon_status()

    if not table_exists(
        connection,
        "amazon_product_links",
    ):
        return empty_amazon_status()

    rows = connection.execute(
        """
        SELECT
            al.amazon_listing_id,
            al.seller_sku,
            al.asin,
            al.amazon_price,
            al.amazon_quantity,
            al.approval_status,
            al.inventory_status,
            apl.match_status
        FROM amazon_product_links apl
        JOIN amazon_listings al
          ON al.amazon_listing_id
           = apl.amazon_listing_id
        WHERE apl.product_id = ?
          AND apl.match_status = 'linked'
        ORDER BY
            CASE
                WHEN al.inventory_status = 'in_stock'
                THEN 0

                WHEN al.inventory_status = 'out_of_stock'
                THEN 1

                ELSE 2
            END,
            al.amazon_listing_id
        """,
        (product_id,),
    ).fetchall()

    if not rows:
        return empty_amazon_status()

    primary = rows[0]

    inventory_status = (
        primary["inventory_status"]
        or "quantity_unknown"
    )

    if inventory_status == "in_stock":
        label = "Amazon Approved — In Stock"
        status = "in_stock"

    elif inventory_status == "out_of_stock":
        label = "Amazon Approved — Out of Stock"
        status = "out_of_stock"

    else:
        label = "Amazon Approved — Quantity Unknown"
        status = "quantity_unknown"

    return {
        "linked": True,
        "approved": True,
        "status": status,
        "label": label,
        "asin": primary["asin"],
        "seller_sku": primary["seller_sku"],
        "price": primary["amazon_price"],
        "quantity": primary["amazon_quantity"],
        "inventory_status": inventory_status,
        "listing_count": len(rows),
        "listings": [
            {
                "asin": row["asin"],
                "seller_sku": row["seller_sku"],
                "price": row["amazon_price"],
                "quantity": row["amazon_quantity"],
                "inventory_status": row["inventory_status"],
            }
            for row in rows
        ],
    }


def get_walmart_status(
    connection: sqlite3.Connection,
    product_id: int,
) -> dict[str, Any]:
    """
    Return Walmart status when Walmart tables are added.

    Until a Walmart report is imported, products remain
    explicitly marked as Walmart Not Linked.
    """

    if not table_exists(
        connection,
        "walmart_listings",
    ):
        return empty_walmart_status()

    if not table_exists(
        connection,
        "walmart_product_links",
    ):
        return empty_walmart_status()

    listing_columns = table_columns(
        connection,
        "walmart_listings",
    )
    link_columns = table_columns(
        connection,
        "walmart_product_links",
    )

    listing_key = first_existing_column(
        listing_columns,
        (
            "walmart_listing_id",
            "listing_id",
            "id",
        ),
    )
    link_listing_key = first_existing_column(
        link_columns,
        (
            "walmart_listing_id",
            "listing_id",
        ),
    )

    if (
        listing_key is None
        or link_listing_key is None
        or "product_id" not in link_columns
    ):
        return empty_walmart_status()

    item_id_sql = selected_column(
        "wl",
        listing_columns,
        (
            "walmart_item_id",
            "external_product_id",
            "item_id",
            "wpid",
            "gtin",
            "upc",
        ),
        "walmart_item_id",
    )
    seller_sku_sql = selected_column(
        "wl",
        listing_columns,
        ("seller_sku", "sku"),
        "seller_sku",
    )
    price_sql = selected_column(
        "wl",
        listing_columns,
        ("walmart_price", "listed_price", "price"),
        "walmart_price",
    )
    quantity_sql = selected_column(
        "wl",
        listing_columns,
        (
            "walmart_quantity",
            "quantity_available",
            "quantity",
        ),
        "walmart_quantity",
    )
    approval_sql = selected_column(
        "wl",
        listing_columns,
        ("approval_status", "listing_status", "status"),
        "approval_status",
    )
    inventory_sql = selected_column(
        "wl",
        listing_columns,
        ("inventory_status",),
        "inventory_status",
    )

    match_filter = ""
    query_parameters: tuple[Any, ...] = (product_id,)

    if "match_status" in link_columns:
        match_filter = (
            "AND lower(COALESCE(wpl.match_status, '')) "
            "= 'linked'"
        )

    query = f"""
        SELECT
            wl."{listing_key}" AS walmart_listing_id,
            {seller_sku_sql},
            {item_id_sql},
            {price_sql},
            {quantity_sql},
            {approval_sql},
            {inventory_sql}
        FROM walmart_product_links wpl
        JOIN walmart_listings wl
          ON wl."{listing_key}"
           = wpl."{link_listing_key}"
        WHERE wpl.product_id = ?
          {match_filter}
        ORDER BY wl."{listing_key}"
        LIMIT 1
        """

    try:
        row = connection.execute(
            query,
            query_parameters,
        ).fetchone()

    except sqlite3.Error as error:
        # Marketplace status is supplemental. A schema mismatch must never
        # prevent Inventory Search from returning the inventory itself.
        print(
            "WALMART PRODUCT STATUS READ ERROR:",
            error,
        )
        return empty_walmart_status()

    if row is None:
        return empty_walmart_status()

    inventory_status = str(
        row["inventory_status"] or ""
    ).strip().lower()

    if not inventory_status:
        quantity = row["walmart_quantity"]

        if quantity is None:
            inventory_status = "quantity_unknown"
        else:
            try:
                inventory_status = (
                    "in_stock"
                    if int(quantity) > 0
                    else "out_of_stock"
                )
            except (TypeError, ValueError):
                inventory_status = "quantity_unknown"

    if inventory_status == "in_stock":
        label = "Walmart Approved — In Stock"
        status = "in_stock"

    elif inventory_status == "out_of_stock":
        label = "Walmart Approved — Out of Stock"
        status = "out_of_stock"

    else:
        label = "Walmart Approved — Quantity Unknown"
        status = "quantity_unknown"

    return {
        "linked": True,
        "approved": True,
        "status": status,
        "label": label,
        "item_id": row["walmart_item_id"],
        "seller_sku": row["seller_sku"],
        "price": row["walmart_price"],
        "quantity": row["walmart_quantity"],
        "inventory_status": inventory_status,
    }


def get_product_channel_status(
    product_id: int,
) -> dict[str, Any]:
    try:
        connection = connect_database()

    except sqlite3.Error as error:
        print(
            "PRODUCT CHANNEL STATUS DATABASE ERROR:",
            error,
        )
        return {
            "product_id": product_id,
            "amazon": empty_amazon_status(),
            "walmart": empty_walmart_status(),
        }

    try:
        try:
            amazon_status = get_amazon_status(
                connection,
                product_id,
            )
        except sqlite3.Error as error:
            print(
                "AMAZON PRODUCT STATUS READ ERROR:",
                error,
            )
            amazon_status = empty_amazon_status()

        try:
            walmart_status = get_walmart_status(
                connection,
                product_id,
            )
        except sqlite3.Error as error:
            print(
                "WALMART PRODUCT STATUS READ ERROR:",
                error,
            )
            walmart_status = empty_walmart_status()

        return {
            "product_id": product_id,
            "amazon": amazon_status,
            "walmart": walmart_status,
        }

    finally:
        connection.close()
