import sqlite3
from pathlib import Path


DATABASE_PATH = Path(
    r"C:\BrooksHouseStore"
    r"\app\data\brookshouse_store.db"
)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def tables(connection):
    return {
        row["name"].lower(): row["name"]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
    }


def columns(connection, table):
    return {
        row["name"].lower(): row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({quote(table)})"
        ).fetchall()
    }


def find_column(column_map, candidates):
    for candidate in candidates:
        if candidate.lower() in column_map:
            return column_map[candidate.lower()]

    return None


connection = sqlite3.connect(
    DATABASE_PATH
)

connection.row_factory = sqlite3.Row

table_map = tables(connection)

products_table = table_map.get("products")
barcodes_table = table_map.get("product_barcodes")
channel_table = (
    table_map.get("channel_listings")
    or table_map.get("sales_channel_listings")
)

if not products_table:
    raise RuntimeError("Products table was not found.")

if not channel_table:
    raise RuntimeError(
        "Shopify channel-listings table was not found."
    )

product_columns = columns(
    connection,
    products_table,
)

channel_columns = columns(
    connection,
    channel_table,
)

product_id_column = find_column(
    product_columns,
    (
        "product_id",
        "id",
    ),
)

product_name_column = find_column(
    product_columns,
    (
        "product_name",
        "name",
        "title",
    ),
)

product_price_column = find_column(
    product_columns,
    (
        "store_price",
        "selling_price",
        "retail_price",
        "price",
        "sale_price",
    ),
)

channel_price_column = find_column(
    channel_columns,
    (
        "listed_price",
        "price",
        "selling_price",
        "variant_price",
    ),
)

channel_product_id_column = find_column(
    channel_columns,
    (
        "product_id",
        "local_product_id",
    ),
)

channel_status_column = find_column(
    channel_columns,
    (
        "listing_status",
        "status",
    ),
)

channel_barcode_column = find_column(
    channel_columns,
    (
        "barcode_exact",
        "barcode_raw",
        "barcode",
        "upc",
    ),
)

if not product_id_column:
    raise RuntimeError(
        "Could not identify the products primary-key column."
    )

if not product_price_column:
    print()
    print("PRODUCT COLUMNS")
    print("---------------")

    for column_name in product_columns.values():
        print(column_name)

    raise RuntimeError(
        "Could not identify the BrooksHouse selling-price column."
    )

if not channel_price_column:
    print()
    print("CHANNEL LISTING COLUMNS")
    print("-----------------------")

    for column_name in channel_columns.values():
        print(column_name)

    raise RuntimeError(
        "Could not identify the Shopify listed-price column."
    )


before = connection.execute(
    f"""
    SELECT
        COUNT(*) AS total_products,

        SUM(
            CASE
                WHEN {quote(product_price_column)} IS NOT NULL
                 AND {quote(product_price_column)} > 0
                THEN 1
                ELSE 0
            END
        ) AS priced_products,

        SUM(
            CASE
                WHEN {quote(product_price_column)} IS NULL
                  OR {quote(product_price_column)} <= 0
                THEN 1
                ELSE 0
            END
        ) AS missing_prices

    FROM {quote(products_table)}
    """
).fetchone()


print()
print("PRICE REPAIR DIAGNOSTIC")
print("-----------------------")
print("Products:", before["total_products"])
print("Products with prices:", before["priced_products"] or 0)
print("Products missing prices:", before["missing_prices"] or 0)
print("Local price column:", product_price_column)
print("Shopify price column:", channel_price_column)
print()


updates = {}


# Best match: ChannelListing already linked to Product.
if channel_product_id_column:
    status_filter = ""

    if channel_status_column:
        status_filter = (
            f"""
            AND upper(
                trim(
                    coalesce(
                        {quote(channel_status_column)},
                        ''
                    )
                )
            ) = 'ACTIVE'
            """
        )

    rows = connection.execute(
        f"""
        SELECT
            {quote(channel_product_id_column)}
                AS product_id,

            MAX(
                CAST(
                    {quote(channel_price_column)}
                    AS REAL
                )
            ) AS shopify_price

        FROM {quote(channel_table)}

        WHERE {quote(channel_product_id_column)}
              IS NOT NULL

          AND CAST(
                coalesce(
                    {quote(channel_price_column)},
                    0
                )
                AS REAL
              ) > 0

          {status_filter}

        GROUP BY
            {quote(channel_product_id_column)}
        """
    ).fetchall()

    for row in rows:
        updates[int(row["product_id"])] = float(
            row["shopify_price"]
        )


# Fallback match: Shopify barcode to local product barcode.
if (
    barcodes_table
    and channel_barcode_column
):
    barcode_columns = columns(
        connection,
        barcodes_table,
    )

    barcode_product_id_column = find_column(
        barcode_columns,
        (
            "product_id",
            "local_product_id",
        ),
    )

    barcode_value_column = find_column(
        barcode_columns,
        (
            "barcode",
            "barcode_value",
            "upc",
        ),
    )

    if (
        barcode_product_id_column
        and barcode_value_column
    ):
        status_filter = ""

        if channel_status_column:
            status_filter = (
                f"""
                AND upper(
                    trim(
                        coalesce(
                            cl.{quote(channel_status_column)},
                            ''
                        )
                    )
                ) = 'ACTIVE'
                """
            )

        rows = connection.execute(
            f"""
            SELECT
                pb.{quote(barcode_product_id_column)}
                    AS product_id,

                MAX(
                    CAST(
                        cl.{quote(channel_price_column)}
                        AS REAL
                    )
                ) AS shopify_price

            FROM {quote(barcodes_table)} pb

            JOIN {quote(channel_table)} cl
              ON ltrim(
                    replace(
                        replace(
                            trim(
                                pb.{quote(barcode_value_column)}
                            ),
                            '-',
                            ''
                        ),
                        ' ',
                        ''
                    ),
                    '0'
                 )
               =
                 ltrim(
                    replace(
                        replace(
                            trim(
                                cl.{quote(channel_barcode_column)}
                            ),
                            '-',
                            ''
                        ),
                        ' ',
                        ''
                    ),
                    '0'
                 )

            WHERE CAST(
                    coalesce(
                        cl.{quote(channel_price_column)},
                        0
                    )
                    AS REAL
                  ) > 0

              {status_filter}

            GROUP BY
                pb.{quote(barcode_product_id_column)}
            """
        ).fetchall()

        for row in rows:
            product_id = int(
                row["product_id"]
            )

            if product_id not in updates:
                updates[product_id] = float(
                    row["shopify_price"]
                )


repaired = 0

for product_id, shopify_price in updates.items():
    result = connection.execute(
        f"""
        UPDATE {quote(products_table)}

        SET {quote(product_price_column)} = ?

        WHERE {quote(product_id_column)} = ?

          AND (
                {quote(product_price_column)} IS NULL
                OR
                {quote(product_price_column)} <= 0
              )
        """,
        (
            shopify_price,
            product_id,
        ),
    )

    repaired += result.rowcount


connection.commit()


after = connection.execute(
    f"""
    SELECT
        COUNT(*) AS total_products,

        SUM(
            CASE
                WHEN {quote(product_price_column)} IS NOT NULL
                 AND {quote(product_price_column)} > 0
                THEN 1
                ELSE 0
            END
        ) AS priced_products,

        SUM(
            CASE
                WHEN {quote(product_price_column)} IS NULL
                  OR {quote(product_price_column)} <= 0
                THEN 1
                ELSE 0
            END
        ) AS missing_prices

    FROM {quote(products_table)}
    """
).fetchone()


print("PRICE REPAIR COMPLETE")
print("---------------------")
print("Shopify product matches found:", len(updates))
print("Local prices repaired:", repaired)
print("Products now priced:", after["priced_products"] or 0)
print("Products still missing prices:", after["missing_prices"] or 0)


print()
print("EXAMPLE PRICED PRODUCTS")
print("-----------------------")

examples = connection.execute(
    f"""
    SELECT
        {quote(product_id_column)}
            AS product_id,

        {quote(product_name_column)}
            AS product_name,

        {quote(product_price_column)}
            AS store_price

    FROM {quote(products_table)}

    WHERE {quote(product_price_column)} > 0

    ORDER BY
        {quote(product_price_column)} DESC

    LIMIT 15
    """
).fetchall()

for row in examples:
    print(
        f"{row['product_id']:>5} | "
        f"${float(row['store_price']):>8.2f} | "
        f"{row['product_name']}"
    )


connection.close()
