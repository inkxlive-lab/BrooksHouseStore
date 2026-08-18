"""Amazon-to-BrooksHouse product mapping helpers."""

import sqlite3
from pathlib import Path
from typing import Any

from app.services.search_helpers import clean_search_term, sql_wildcard_pattern


DATABASE_PATH = Path(
    r"C:\BrooksHouseStore"
    r"\app\data\brookshouse_store.db"
)


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row

    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    return connection


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = connection.execute(
        """
        SELECT name
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
) -> list[str]:
    if not table_exists(
        connection,
        table_name,
    ):
        return []

    rows = connection.execute(
        f"""
        PRAGMA table_info(
            {quote_identifier(table_name)}
        )
        """
    ).fetchall()

    return [
        row["name"]
        for row in rows
    ]


def first_column(
    columns: list[str],
    candidates: tuple[str, ...],
) -> str | None:
    column_map = {
        column.lower(): column
        for column in columns
    }

    for candidate in candidates:
        if candidate.lower() in column_map:
            return column_map[
                candidate.lower()
            ]

    return None


def product_schema(
    connection: sqlite3.Connection,
) -> dict[str, str | None]:
    columns = table_columns(
        connection,
        "products",
    )

    return {
        "id": first_column(
            columns,
            (
                "product_id",
                "id",
            ),
        ),
        "name": first_column(
            columns,
            (
                "product_name",
                "name",
                "title",
            ),
        ),
        "sku": first_column(
            columns,
            (
                "sku",
                "product_sku",
                "internal_sku",
            ),
        ),
        "price": first_column(
            columns,
            (
                "store_price",
                "selling_price",
                "retail_price",
                "price",
            ),
        ),
    }


def barcode_schema(
    connection: sqlite3.Connection,
) -> dict[str, str | None]:
    columns = table_columns(
        connection,
        "product_barcodes",
    )

    return {
        "product_id": first_column(
            columns,
            (
                "product_id",
                "local_product_id",
            ),
        ),
        "barcode": first_column(
            columns,
            (
                "barcode",
                "barcode_value",
                "upc",
            ),
        ),
    }


def image_schema(
    connection: sqlite3.Connection,
) -> dict[str, str | None]:
    columns = table_columns(
        connection,
        "product_images",
    )

    return {
        "product_id": first_column(
            columns,
            (
                "product_id",
                "local_product_id",
            ),
        ),
        "url": first_column(
            columns,
            (
                "image_url",
                "url",
                "source_url",
                "external_url",
                "file_path",
            ),
        ),
        "primary": first_column(
            columns,
            (
                "is_primary",
                "primary_image",
            ),
        ),
    }


def build_product_rows(
    connection: sqlite3.Connection,
    search_term: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    products = product_schema(
        connection
    )

    product_id_column = products["id"]
    product_name_column = products["name"]
    product_sku_column = products["sku"]
    product_price_column = products["price"]

    if (
        not product_id_column
        or not product_name_column
    ):
        return []

    barcode = barcode_schema(
        connection
    )

    image = image_schema(
        connection
    )

    selected_columns = [
        (
            f"p.{quote_identifier(product_id_column)} "
            "AS product_id"
        ),
        (
            f"p.{quote_identifier(product_name_column)} "
            "AS product_name"
        ),
    ]

    if product_sku_column:
        selected_columns.append(
            f"p.{quote_identifier(product_sku_column)} "
            "AS product_sku"
        )
    else:
        selected_columns.append(
            "NULL AS product_sku"
        )

    if product_price_column:
        selected_columns.append(
            f"p.{quote_identifier(product_price_column)} "
            "AS store_price"
        )
    else:
        selected_columns.append(
            "NULL AS store_price"
        )

    joins = []
    search_conditions = [
        (
            f"lower(p.{quote_identifier(product_name_column)}) "
            "LIKE lower(?) ESCAPE '\\'"
        )
    ]

    parameters: list[Any] = []

    search_value = sql_wildcard_pattern(search_term)

    parameters.append(search_value)

    if product_sku_column:
        search_conditions.append(
            (
                f"lower(coalesce("
                f"p.{quote_identifier(product_sku_column)}, "
                "'')) LIKE lower(?) ESCAPE '\\'"
            )
        )

        parameters.append(search_value)

    if (
        barcode["product_id"]
        and barcode["barcode"]
    ):
        joins.append(
            f"""
            LEFT JOIN product_barcodes pb
              ON pb.{quote_identifier(barcode["product_id"])}
               = p.{quote_identifier(product_id_column)}
            """
        )

        selected_columns.append(
            (
                "group_concat("
                f"DISTINCT pb.{quote_identifier(barcode['barcode'])}"
                ") AS barcodes"
            )
        )

        search_conditions.append(
            (
                f"pb.{quote_identifier(barcode['barcode'])} "
                "LIKE ? ESCAPE '\\'"
            )
        )

        parameters.append(search_value)
    else:
        selected_columns.append(
            "NULL AS barcodes"
        )

    if (
        table_exists(
            connection,
            "product_images",
        )
        and image["product_id"]
        and image["url"]
    ):
        primary_order = ""

        if image["primary"]:
            primary_order = (
                f"ORDER BY "
                f"pi.{quote_identifier(image['primary'])} "
                "DESC"
            )

        selected_columns.append(
            f"""
            (
                SELECT
                    pi.{quote_identifier(image["url"])}
                FROM product_images pi
                WHERE
                    pi.{quote_identifier(image["product_id"])}
                    = p.{quote_identifier(product_id_column)}
                  AND
                    pi.{quote_identifier(image["url"])}
                    IS NOT NULL
                {primary_order}
                LIMIT 1
            ) AS image_url
            """
        )
    else:
        selected_columns.append(
            "NULL AS image_url"
        )

    where_clause = ""

    if clean_search_term(search_term):
        where_clause = (
            "WHERE "
            + " OR ".join(
                f"({condition})"
                for condition in search_conditions
            )
        )
    else:
        parameters = []

    query = f"""
        SELECT
            {", ".join(selected_columns)}
        FROM products p
        {" ".join(joins)}
        {where_clause}
        GROUP BY
            p.{quote_identifier(product_id_column)}
        ORDER BY
            p.{quote_identifier(product_name_column)}
        LIMIT ?
    """

    parameters.append(limit)

    rows = connection.execute(
        query,
        parameters,
    ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def get_mapping_page_data(
    search_term: str = "",
    mapping_filter: str = "unmatched",
) -> dict[str, Any]:
    connection = connect_database()

    try:
        mapping_filter = clean_text(
            mapping_filter
        ).lower()

        if mapping_filter not in {
            "unmatched",
            "linked",
            "all",
        }:
            mapping_filter = "unmatched"

        status_clause = ""

        if mapping_filter == "unmatched":
            status_clause = (
                "WHERE apl.match_status = 'unmatched'"
            )

        elif mapping_filter == "linked":
            status_clause = (
                "WHERE apl.match_status = 'linked'"
            )

        amazon_rows = connection.execute(
            f"""
            SELECT
                al.amazon_listing_id,
                al.seller_sku,
                al.asin,
                al.amazon_price,
                al.amazon_quantity,
                al.approval_status,
                al.inventory_status,
                apl.product_id,
                apl.match_status,
                apl.match_method,
                apl.match_value,
                apl.linked_at
            FROM amazon_listings al
            JOIN amazon_product_links apl
              ON apl.amazon_listing_id
               = al.amazon_listing_id
            {status_clause}
            ORDER BY
                CASE
                    WHEN apl.match_status = 'unmatched'
                    THEN 0
                    ELSE 1
                END,
                al.seller_sku
            """
        ).fetchall()

        summary = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN apl.match_status = 'linked'
                        THEN 1
                        ELSE 0
                    END
                ) AS linked,
                SUM(
                    CASE
                        WHEN apl.match_status = 'unmatched'
                        THEN 1
                        ELSE 0
                    END
                ) AS unmatched,
                SUM(
                    CASE
                        WHEN al.inventory_status = 'in_stock'
                        THEN 1
                        ELSE 0
                    END
                ) AS in_stock,
                SUM(
                    CASE
                        WHEN al.inventory_status = 'out_of_stock'
                        THEN 1
                        ELSE 0
                    END
                ) AS out_of_stock,
                SUM(
                    CASE
                        WHEN al.inventory_status = 'quantity_unknown'
                        THEN 1
                        ELSE 0
                    END
                ) AS quantity_unknown
            FROM amazon_listings al
            JOIN amazon_product_links apl
              ON apl.amazon_listing_id
               = al.amazon_listing_id
            """
        ).fetchone()

        products = build_product_rows(
            connection,
            search_term=search_term,
            limit=100,
        )

        linked_products: dict[int, dict[str, Any]] = {}

        product_ids = [
            row["product_id"]
            for row in amazon_rows
            if row["product_id"] is not None
        ]

        for product_id in product_ids:
            result = build_product_rows(
                connection,
                search_term="",
                limit=5000,
            )

            for product in result:
                if (
                    product["product_id"]
                    == product_id
                ):
                    linked_products[
                        product_id
                    ] = product
                    break

        return {
            "amazon_rows": [
                dict(row)
                for row in amazon_rows
            ],
            "summary": dict(summary),
            "products": products,
            "linked_products": linked_products,
            "search_term": search_term,
            "mapping_filter": mapping_filter,
        }

    finally:
        connection.close()


def link_amazon_listing(
    amazon_listing_id: int,
    product_id: int,
) -> None:
    connection = connect_database()

    try:
        product = product_schema(
            connection
        )

        if not product["id"]:
            raise RuntimeError(
                "Could not identify the products primary key."
            )

        existing_product = connection.execute(
            f"""
            SELECT
                {quote_identifier(product["id"])}
            FROM products
            WHERE
                {quote_identifier(product["id"])}
                = ?
            LIMIT 1
            """,
            (product_id,),
        ).fetchone()

        if existing_product is None:
            raise ValueError(
                "The selected BrooksHouse product was not found."
            )

        listing = connection.execute(
            """
            SELECT
                amazon_listing_id,
                seller_sku,
                asin
            FROM amazon_listings
            WHERE amazon_listing_id = ?
            LIMIT 1
            """,
            (amazon_listing_id,),
        ).fetchone()

        if listing is None:
            raise ValueError(
                "The selected Amazon listing was not found."
            )

        connection.execute(
            """
            UPDATE amazon_product_links
            SET
                product_id = ?,
                match_status = 'linked',
                match_method = 'manual_mapping',
                match_value = ?,
                linked_at = datetime('now')
            WHERE amazon_listing_id = ?
            """,
            (
                product_id,
                listing["seller_sku"],
                amazon_listing_id,
            ),
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def unlink_amazon_listing(
    amazon_listing_id: int,
) -> None:
    connection = connect_database()

    try:
        connection.execute(
            """
            UPDATE amazon_product_links
            SET
                product_id = NULL,
                match_status = 'unmatched',
                match_method = NULL,
                match_value = NULL,
                linked_at = NULL
            WHERE amazon_listing_id = ?
            """,
            (amazon_listing_id,),
        )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()
