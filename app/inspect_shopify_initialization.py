import sqlite3
from pathlib import Path


DATABASE_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "brookshouse_store.db"
)

print(f"Database: {DATABASE_PATH}")
print(f"Exists: {DATABASE_PATH.exists()}")

if not DATABASE_PATH.exists():
    raise FileNotFoundError(
        f"Database not found: {DATABASE_PATH}"
    )


connection = sqlite3.connect(DATABASE_PATH)
connection.row_factory = sqlite3.Row

try:
    print()
    print("=" * 70)
    print("DATABASE TABLES")
    print("=" * 70)

    tables = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

    table_names = [
        row["name"]
        for row in tables
    ]

    for table_name in table_names:
        print(table_name)

    print()
    print("=" * 70)
    print("LOCATION-LIKE TABLES")
    print("=" * 70)

    location_tables = [
        table_name
        for table_name in table_names
        if "loc" in table_name.lower()
    ]

    if not location_tables:
        print("No location-like table names found.")

    for table_name in location_tables:
        print()
        print(f"TABLE: {table_name}")

        columns = connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()

        for column in columns:
            print(
                f"  {column['name']} | "
                f"{column['type']} | "
                f"nullable={not bool(column['notnull'])}"
            )

        rows = connection.execute(
            f'SELECT * FROM "{table_name}" LIMIT 25'
        ).fetchall()

        print("  SAMPLE ROWS:")

        if not rows:
            print("    No rows")

        for row in rows:
            print(
                "   ",
                dict(row),
            )

    print()
    print("=" * 70)
    print("IMPORTANT TABLE STRUCTURES")
    print("=" * 70)

    likely_tables = [
        "inventory",
        "inventory_transactions",
        "products",
        "sales_channels",
        "channel_listings",
    ]

    for table_name in likely_tables:
        if table_name not in table_names:
            print()
            print(
                f"TABLE NOT FOUND: {table_name}"
            )
            continue

        print()
        print(f"TABLE: {table_name}")

        columns = connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()

        for column in columns:
            print(
                f"  {column['name']} | "
                f"{column['type']} | "
                f"nullable={not bool(column['notnull'])}"
            )

    print()
    print("=" * 70)
    print("SHOPIFY COUNTS")
    print("=" * 70)

    if (
        "sales_channels" in table_names
        and "channel_listings" in table_names
    ):
        shopify = connection.execute(
            """
            SELECT *
            FROM sales_channels
            WHERE LOWER(channel_name) = 'shopify'
            LIMIT 1
            """
        ).fetchone()

        if shopify is None:
            print("Shopify channel not found.")
        else:
            print(
                "Shopify channel:",
                dict(shopify),
            )

            channel_id = shopify["channel_id"]

            total = connection.execute(
                """
                SELECT COUNT(*)
                FROM channel_listings
                WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchone()[0]

            positive = connection.execute(
                """
                SELECT COUNT(*)
                FROM channel_listings
                WHERE channel_id = ?
                  AND COALESCE(
                        quantity_available,
                        0
                      ) > 0
                """,
                (channel_id,),
            ).fetchone()[0]

            total_quantity = connection.execute(
                """
                SELECT COALESCE(
                    SUM(quantity_available),
                    0
                )
                FROM channel_listings
                WHERE channel_id = ?
                  AND COALESCE(
                        quantity_available,
                        0
                      ) > 0
                """,
                (channel_id,),
            ).fetchone()[0]

            print(
                f"Total Shopify listings: {total}"
            )
            print(
                "Listings with positive quantity: "
                f"{positive}"
            )
            print(
                "Total positive Shopify units: "
                f"{total_quantity}"
            )
    else:
        print(
            "Shopify tables were not found under "
            "the expected names."
        )

finally:
    connection.close()
