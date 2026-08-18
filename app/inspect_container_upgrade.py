from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

from app.database.connection import engine
from app.database.models import (
    Inventory,
    InventoryLocation,
    InventoryTransaction,
)


def print_heading(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


print_heading("DATABASE CONNECTION")

database_url = str(engine.url)
print("Engine URL:", database_url)


print_heading("INVENTORY MODEL")

try:
    print(inspect.getsource(Inventory))
except Exception as error:
    print("Could not inspect Inventory:", error)
    print("Table:", Inventory.__table__)


print_heading("INVENTORY LOCATION MODEL")

try:
    print(inspect.getsource(InventoryLocation))
except Exception as error:
    print("Could not inspect InventoryLocation:", error)
    print("Table:", InventoryLocation.__table__)


print_heading("INVENTORY TRANSACTION MODEL")

try:
    print(inspect.getsource(InventoryTransaction))
except Exception as error:
    print("Could not inspect InventoryTransaction:", error)
    print("Table:", InventoryTransaction.__table__)


print_heading("SQLALCHEMY INVENTORY TABLE")

print(Inventory.__table__)

print()
print("Columns:")

for column in Inventory.__table__.columns:
    print(
        f"- {column.name}: "
        f"type={column.type}, "
        f"nullable={column.nullable}, "
        f"primary_key={column.primary_key}, "
        f"default={column.default}"
    )

print()
print("Constraints:")

for constraint in Inventory.__table__.constraints:
    print("-", repr(constraint))


if database_url.startswith("sqlite:///"):
    database_path_text = database_url.replace(
        "sqlite:///",
        "",
        1,
    )

    database_path = Path(database_path_text)

    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path

    database_path = database_path.resolve()

    print_heading("SQLITE DATABASE")

    print("Database path:", database_path)
    print("Exists:", database_path.exists())

    if database_path.exists():
        connection = sqlite3.connect(database_path)

        try:
            cursor = connection.cursor()

            cursor.execute(
                """
                SELECT name, sql
                FROM sqlite_master
                WHERE type = 'table'
                  AND lower(name) LIKE '%invent%'
                ORDER BY name
                """
            )

            rows = cursor.fetchall()

            for table_name, create_sql in rows:
                print()
                print(f"TABLE: {table_name}")
                print(create_sql)

                cursor.execute(
                    f'PRAGMA table_info("{table_name}")'
                )

                print("Columns:")

                for column in cursor.fetchall():
                    print(column)

                cursor.execute(
                    f'PRAGMA index_list("{table_name}")'
                )

                indexes = cursor.fetchall()

                print("Indexes:")

                for index in indexes:
                    print(index)

                    index_name = index[1]

                    cursor.execute(
                        f'PRAGMA index_info("{index_name}")'
                    )

                    print(
                        "  Index columns:",
                        cursor.fetchall(),
                    )

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM inventory
                """
            )

            print()
            print(
                "Current inventory rows:",
                cursor.fetchone()[0],
            )

        finally:
            connection.close()
else:
    print()
    print(
        "This is not a SQLite database. "
        "No raw SQLite inspection was performed."
    )
