import sqlite3
from pathlib import Path


database_path = (
    Path(__file__).resolve().parent
    / "data"
    / "brookshouse_store.db"
)

connection = sqlite3.connect(database_path)

try:
    existing = connection.execute(
        """
        SELECT location_id, location_name
        FROM inventory_locations
        WHERE LOWER(location_name) = LOWER(?)
        LIMIT 1
        """,
        ("PROB - Inventory Review",),
    ).fetchone()

    if existing:
        print(
            "Location already exists:",
            existing[0],
            existing[1],
        )
    else:
        cursor = connection.execute(
            """
            INSERT INTO inventory_locations (
                location_name,
                location_type,
                description,
                active,
                created_at
            )
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (
                "PROB - Inventory Review",
                "hold",
                (
                    "Temporary inventory differences "
                    "awaiting physical verification or "
                    "transfer to the correct location."
                ),
            ),
        )

        connection.commit()

        print(
            "Created PROB location with ID:",
            cursor.lastrowid,
        )

finally:
    connection.close()
