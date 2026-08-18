"""Financial calculations for the BrooksHouse dashboard."""

import sqlite3
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(
    r"C:\BrooksHouseStore"
    r"\app\data\brookshouse_store.db"
)

STOREFRONT_NAME = "BrooksHouse Storefront"


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_names(
    connection: sqlite3.Connection,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

    return [
        row["name"]
        for row in rows
    ]


def find_table(
    connection: sqlite3.Connection,
    candidates: tuple[str, ...],
) -> str | None:
    existing = {
        name.lower(): name
        for name in table_names(connection)
    }

    for candidate in candidates:
        if candidate.lower() in existing:
            return existing[candidate.lower()]

    return None


def table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[str]:
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


def find_column(
    columns: list[str],
    candidates: tuple[str, ...],
) -> str | None:
    existing = {
        column.lower(): column
        for column in columns
    }

    for candidate in candidates:
        if candidate.lower() in existing:
            return existing[candidate.lower()]

    return None


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def money(value: Any) -> float:
    return round(
        number(value),
        2,
    )


def empty_summary(
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "error": error,
        "storefront_found": False,
        "storefront_name": STOREFRONT_NAME,
        "product_count": 0,
        "unit_count": 0,
        "priced_product_count": 0,
        "missing_price_count": 0,
        "missing_cost_count": 0,
        "retail_value": 0.0,
        "cost_value": 0.0,
        "estimated_profit": 0.0,
        "estimated_margin": 0.0,
        "average_selling_price": 0.0,
        "amazon_approved_count": 0,
        "amazon_in_stock_count": 0,
        "amazon_inventory_value": 0.0,
        "top_products": [],
    }


def build_financial_summary() -> dict[str, Any]:
    if not DATABASE_PATH.exists():
        return empty_summary(
            "The BrooksHouse database file was not found."
        )

    connection = connect_database()

    try:
        products_table = find_table(
            connection,
            (
                "products",
                "product",
            ),
        )

        inventory_table = find_table(
            connection,
            (
                "inventory",
                "inventories",
            ),
        )

        locations_table = find_table(
            connection,
            (
                "inventory_locations",
                "locations",
                "inventory_location",
            ),
        )

        if not products_table:
            return empty_summary(
                "The products table was not found."
            )

        if not inventory_table:
            return empty_summary(
                "The inventory table was not found."
            )

        if not locations_table:
            return empty_summary(
                "The inventory locations table was not found."
            )

        product_columns = table_columns(
            connection,
            products_table,
        )

        inventory_columns = table_columns(
            connection,
            inventory_table,
        )

        location_columns = table_columns(
            connection,
            locations_table,
        )

        product_id = find_column(
            product_columns,
            (
                "product_id",
                "id",
            ),
        )

        product_name = find_column(
            product_columns,
            (
                "product_name",
                "name",
                "title",
            ),
        )

        selling_price = find_column(
            product_columns,
            (
                "store_price",
                "selling_price",
                "retail_price",
                "price",
                "sale_price",
            ),
        )

        average_cost = find_column(
            product_columns,
            (
                "average_cost",
                "avg_cost",
                "unit_cost",
                "cost",
                "purchase_cost",
            ),
        )

        inventory_product_id = find_column(
            inventory_columns,
            (
                "product_id",
                "local_product_id",
            ),
        )

        inventory_location_id = find_column(
            inventory_columns,
            (
                "location_id",
                "inventory_location_id",
            ),
        )

        quantity_on_hand = find_column(
            inventory_columns,
            (
                "quantity_on_hand",
                "quantity",
                "on_hand",
                "qty_on_hand",
            ),
        )

        location_id = find_column(
            location_columns,
            (
                "location_id",
                "id",
            ),
        )

        location_name = find_column(
            location_columns,
            (
                "location_name",
                "name",
            ),
        )

        required_columns = {
            "product ID": product_id,
            "product name": product_name,
            "inventory product ID": inventory_product_id,
            "inventory location ID": inventory_location_id,
            "quantity": quantity_on_hand,
            "location ID": location_id,
            "location name": location_name,
        }

        missing_required = [
            label
            for label, column
            in required_columns.items()
            if not column
        ]

        if missing_required:
            return empty_summary(
                "Missing database fields: "
                + ", ".join(missing_required)
            )

        selling_expression = (
            f"p.{quote_identifier(selling_price)}"
            if selling_price
            else "NULL"
        )

        cost_expression = (
            f"p.{quote_identifier(average_cost)}"
            if average_cost
            else "NULL"
        )

        summary_row = connection.execute(
            f"""
            SELECT
                COUNT(
                    DISTINCT
                    p.{quote_identifier(product_id)}
                ) AS product_count,

                COALESCE(
                    SUM(
                        COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        )
                    ),
                    0
                ) AS unit_count,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN {selling_expression} IS NOT NULL
                         AND {selling_expression} > 0
                        THEN p.{quote_identifier(product_id)}
                    END
                ) AS priced_product_count,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN {selling_expression} IS NULL
                          OR {selling_expression} <= 0
                        THEN p.{quote_identifier(product_id)}
                    END
                ) AS missing_price_count,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN {cost_expression} IS NULL
                          OR {cost_expression} <= 0
                        THEN p.{quote_identifier(product_id)}
                    END
                ) AS missing_cost_count,

                COALESCE(
                    SUM(
                        COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        )
                        *
                        COALESCE(
                            {selling_expression},
                            0
                        )
                    ),
                    0
                ) AS retail_value,

                COALESCE(
                    SUM(
                        COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        )
                        *
                        COALESCE(
                            {cost_expression},
                            0
                        )
                    ),
                    0
                ) AS cost_value,

                COALESCE(
                    AVG(
                        CASE
                            WHEN {selling_expression} > 0
                            THEN {selling_expression}
                        END
                    ),
                    0
                ) AS average_selling_price

            FROM {quote_identifier(inventory_table)} i

            JOIN {quote_identifier(products_table)} p
              ON p.{quote_identifier(product_id)}
               = i.{quote_identifier(inventory_product_id)}

            JOIN {quote_identifier(locations_table)} l
              ON l.{quote_identifier(location_id)}
               = i.{quote_identifier(inventory_location_id)}

            WHERE lower(
                trim(
                    l.{quote_identifier(location_name)}
                )
            ) = lower(trim(?))
            """,
            (STOREFRONT_NAME,),
        ).fetchone()

        location_exists = connection.execute(
            f"""
            SELECT 1
            FROM {quote_identifier(locations_table)}
            WHERE lower(
                trim(
                    {quote_identifier(location_name)}
                )
            ) = lower(trim(?))
            LIMIT 1
            """,
            (STOREFRONT_NAME,),
        ).fetchone()

        retail_value = number(
            summary_row["retail_value"]
        )

        cost_value = number(
            summary_row["cost_value"]
        )

        estimated_profit = (
            retail_value - cost_value
        )

        estimated_margin = (
            estimated_profit / retail_value * 100
            if retail_value > 0
            else 0
        )

        top_rows = connection.execute(
            f"""
            SELECT
                p.{quote_identifier(product_id)}
                    AS product_id,

                p.{quote_identifier(product_name)}
                    AS product_name,

                COALESCE(
                    i.{quote_identifier(quantity_on_hand)},
                    0
                ) AS quantity,

                {selling_expression}
                    AS selling_price,

                {cost_expression}
                    AS average_cost,

                (
                    COALESCE(
                        i.{quote_identifier(quantity_on_hand)},
                        0
                    )
                    *
                    COALESCE(
                        {selling_expression},
                        0
                    )
                ) AS retail_value,

                (
                    COALESCE(
                        i.{quote_identifier(quantity_on_hand)},
                        0
                    )
                    *
                    (
                        COALESCE(
                            {selling_expression},
                            0
                        )
                        -
                        COALESCE(
                            {cost_expression},
                            0
                        )
                    )
                ) AS estimated_profit

            FROM {quote_identifier(inventory_table)} i

            JOIN {quote_identifier(products_table)} p
              ON p.{quote_identifier(product_id)}
               = i.{quote_identifier(inventory_product_id)}

            JOIN {quote_identifier(locations_table)} l
              ON l.{quote_identifier(location_id)}
               = i.{quote_identifier(inventory_location_id)}

            WHERE lower(
                trim(
                    l.{quote_identifier(location_name)}
                )
            ) = lower(trim(?))

              AND COALESCE(
                    i.{quote_identifier(quantity_on_hand)},
                    0
                  ) > 0

            ORDER BY retail_value DESC,
                     product_name

            LIMIT 10
            """,
            (STOREFRONT_NAME,),
        ).fetchall()

        amazon_approved_count = 0
        amazon_in_stock_count = 0
        amazon_inventory_value = 0.0

        amazon_table = find_table(
            connection,
            (
                "amazon_listings",
            ),
        )

        if amazon_table:
            amazon_columns = table_columns(
                connection,
                amazon_table,
            )

            amazon_price = find_column(
                amazon_columns,
                (
                    "amazon_price",
                    "price",
                ),
            )

            amazon_quantity = find_column(
                amazon_columns,
                (
                    "amazon_quantity",
                    "quantity",
                ),
            )

            amazon_status = find_column(
                amazon_columns,
                (
                    "inventory_status",
                    "status",
                ),
            )

            if amazon_price and amazon_quantity:
                amazon_row = connection.execute(
                    f"""
                    SELECT
                        COUNT(*) AS approved_count,

                        SUM(
                            CASE
                                WHEN COALESCE(
                                    {quote_identifier(amazon_quantity)},
                                    0
                                ) > 0
                                THEN 1
                                ELSE 0
                            END
                        ) AS in_stock_count,

                        COALESCE(
                            SUM(
                                COALESCE(
                                    {quote_identifier(amazon_quantity)},
                                    0
                                )
                                *
                                COALESCE(
                                    {quote_identifier(amazon_price)},
                                    0
                                )
                            ),
                            0
                        ) AS inventory_value

                    FROM {quote_identifier(amazon_table)}
                    """
                ).fetchone()

                amazon_approved_count = int(
                    amazon_row["approved_count"]
                    or 0
                )

                amazon_in_stock_count = int(
                    amazon_row["in_stock_count"]
                    or 0
                )

                amazon_inventory_value = number(
                    amazon_row["inventory_value"]
                )

        return {
            "error": None,
            "storefront_found": bool(
                location_exists
            ),
            "storefront_name": STOREFRONT_NAME,
            "product_count": int(
                summary_row["product_count"]
                or 0
            ),
            "unit_count": int(
                summary_row["unit_count"]
                or 0
            ),
            "priced_product_count": int(
                summary_row["priced_product_count"]
                or 0
            ),
            "missing_price_count": int(
                summary_row["missing_price_count"]
                or 0
            ),
            "missing_cost_count": int(
                summary_row["missing_cost_count"]
                or 0
            ),
            "retail_value": money(
                retail_value
            ),
            "cost_value": money(
                cost_value
            ),
            "estimated_profit": money(
                estimated_profit
            ),
            "estimated_margin": round(
                estimated_margin,
                1,
            ),
            "average_selling_price": money(
                summary_row["average_selling_price"]
            ),
            "amazon_approved_count": (
                amazon_approved_count
            ),
            "amazon_in_stock_count": (
                amazon_in_stock_count
            ),
            "amazon_inventory_value": money(
                amazon_inventory_value
            ),
            "top_products": [
                {
                    "product_id": int(
                        row["product_id"]
                    ),
                    "product_name": (
                        row["product_name"]
                    ),
                    "quantity": int(
                        row["quantity"]
                        or 0
                    ),
                    "selling_price": money(
                        row["selling_price"]
                    ),
                    "average_cost": money(
                        row["average_cost"]
                    ),
                    "retail_value": money(
                        row["retail_value"]
                    ),
                    "estimated_profit": money(
                        row["estimated_profit"]
                    ),
                }
                for row in top_rows
            ],
        }

    except Exception as error:
        return empty_summary(
            str(error)
        )

    finally:
        connection.close()


def build_location_financial_summary() -> dict[str, Any]:
    """Return retail and cost values for every inventory location."""

    if not DATABASE_PATH.exists():
        return {
            "error": "The BrooksHouse database file was not found.",
            "locations": [],
        }

    connection = connect_database()

    try:
        products_table = find_table(
            connection,
            (
                "products",
                "product",
            ),
        )

        inventory_table = find_table(
            connection,
            (
                "inventory",
                "inventories",
            ),
        )

        locations_table = find_table(
            connection,
            (
                "inventory_locations",
                "locations",
                "inventory_location",
            ),
        )

        if not products_table:
            raise RuntimeError(
                "The products table was not found."
            )

        if not inventory_table:
            raise RuntimeError(
                "The inventory table was not found."
            )

        if not locations_table:
            raise RuntimeError(
                "The inventory locations table was not found."
            )

        product_columns = table_columns(
            connection,
            products_table,
        )

        inventory_columns = table_columns(
            connection,
            inventory_table,
        )

        location_columns = table_columns(
            connection,
            locations_table,
        )

        product_id = find_column(
            product_columns,
            (
                "product_id",
                "id",
            ),
        )

        selling_price = find_column(
            product_columns,
            (
                "store_price",
                "selling_price",
                "retail_price",
                "price",
                "sale_price",
            ),
        )

        average_cost = find_column(
            product_columns,
            (
                "average_cost",
                "avg_cost",
                "unit_cost",
                "cost",
                "purchase_cost",
            ),
        )

        inventory_product_id = find_column(
            inventory_columns,
            (
                "product_id",
                "local_product_id",
            ),
        )

        inventory_location_id = find_column(
            inventory_columns,
            (
                "location_id",
                "inventory_location_id",
            ),
        )

        quantity_on_hand = find_column(
            inventory_columns,
            (
                "quantity_on_hand",
                "quantity",
                "on_hand",
                "qty_on_hand",
            ),
        )

        location_id = find_column(
            location_columns,
            (
                "location_id",
                "id",
            ),
        )

        location_name = find_column(
            location_columns,
            (
                "location_name",
                "name",
            ),
        )

        location_type = find_column(
            location_columns,
            (
                "location_type",
                "type",
            ),
        )

        required_columns = {
            "product ID": product_id,
            "inventory product ID": inventory_product_id,
            "inventory location ID": inventory_location_id,
            "quantity": quantity_on_hand,
            "location ID": location_id,
            "location name": location_name,
        }

        missing_columns = [
            label
            for label, column
            in required_columns.items()
            if not column
        ]

        if missing_columns:
            raise RuntimeError(
                "Missing database fields: "
                + ", ".join(missing_columns)
            )

        selling_expression = (
            f"p.{quote_identifier(selling_price)}"
            if selling_price
            else "NULL"
        )

        cost_expression = (
            f"p.{quote_identifier(average_cost)}"
            if average_cost
            else "NULL"
        )

        location_type_expression = (
            f"l.{quote_identifier(location_type)}"
            if location_type
            else "NULL"
        )

        rows = connection.execute(
            f"""
            SELECT
                l.{quote_identifier(location_id)}
                    AS location_id,

                l.{quote_identifier(location_name)}
                    AS location_name,

                {location_type_expression}
                    AS location_type,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        ) != 0
                        THEN p.{quote_identifier(product_id)}
                    END
                ) AS product_count,

                COALESCE(
                    SUM(
                        COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        )
                    ),
                    0
                ) AS unit_count,

                COALESCE(
                    SUM(
                        COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        )
                        *
                        COALESCE(
                            {selling_expression},
                            0
                        )
                    ),
                    0
                ) AS retail_value,

                COALESCE(
                    SUM(
                        COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        )
                        *
                        COALESCE(
                            {cost_expression},
                            0
                        )
                    ),
                    0
                ) AS cost_value,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        ) > 0
                         AND (
                            {selling_expression} IS NULL
                            OR {selling_expression} <= 0
                         )
                        THEN p.{quote_identifier(product_id)}
                    END
                ) AS missing_price_count,

                COUNT(
                    DISTINCT
                    CASE
                        WHEN COALESCE(
                            i.{quote_identifier(quantity_on_hand)},
                            0
                        ) > 0
                         AND (
                            {cost_expression} IS NULL
                            OR {cost_expression} <= 0
                         )
                        THEN p.{quote_identifier(product_id)}
                    END
                ) AS missing_cost_count

            FROM {quote_identifier(locations_table)} l

            LEFT JOIN {quote_identifier(inventory_table)} i
              ON i.{quote_identifier(inventory_location_id)}
               = l.{quote_identifier(location_id)}

            LEFT JOIN {quote_identifier(products_table)} p
              ON p.{quote_identifier(product_id)}
               = i.{quote_identifier(inventory_product_id)}

            GROUP BY
                l.{quote_identifier(location_id)},
                l.{quote_identifier(location_name)},
                {location_type_expression}

            ORDER BY
                CASE
                    WHEN lower(
                        trim(
                            l.{quote_identifier(location_name)}
                        )
                    ) = lower(trim(?))
                    THEN 0
                    ELSE 1
                END,

                l.{quote_identifier(location_name)}
            """,
            (STOREFRONT_NAME,),
        ).fetchall()

        return {
            "error": None,
            "locations": [
                {
                    "location_id": int(
                        row["location_id"]
                    ),
                    "location_name": (
                        row["location_name"]
                    ),
                    "location_type": (
                        row["location_type"]
                    ),
                    "product_count": int(
                        row["product_count"]
                        or 0
                    ),
                    "unit_count": int(
                        row["unit_count"]
                        or 0
                    ),
                    "retail_value": money(
                        row["retail_value"]
                    ),
                    "cost_value": money(
                        row["cost_value"]
                    ),
                    "missing_price_count": int(
                        row["missing_price_count"]
                        or 0
                    ),
                    "missing_cost_count": int(
                        row["missing_cost_count"]
                        or 0
                    ),
                }
                for row in rows
            ],
        }

    except Exception as error:
        return {
            "error": str(error),
            "locations": [],
        }

    finally:
        connection.close()
