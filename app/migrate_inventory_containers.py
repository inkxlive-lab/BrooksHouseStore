from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path.cwd()
DATABASE_PATH = (
    PROJECT_ROOT
    / "app"
    / "data"
    / "brookshouse_store.db"
)

MODELS_PATH = (
    PROJECT_ROOT
    / "app"
    / "database"
    / "models.py"
)

BACKUP_ROOT = (
    PROJECT_ROOT
    / "backups"
    / (
        "container-migration-"
        + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
)


def backup_files() -> None:
    BACKUP_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.copy2(
        DATABASE_PATH,
        BACKUP_ROOT / DATABASE_PATH.name,
    )

    shutil.copy2(
        MODELS_PATH,
        BACKUP_ROOT / MODELS_PATH.name,
    )

    print("Backup created:")
    print(BACKUP_ROOT)


def column_exists(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    rows = connection.execute(
        f'PRAGMA table_info("{table_name}")'
    ).fetchall()

    return any(
        row[1] == column_name
        for row in rows
    )


def migrate_database() -> None:
    connection = sqlite3.connect(
        DATABASE_PATH
    )

    try:
        connection.execute(
            "PRAGMA foreign_keys = OFF"
        )

        connection.execute("BEGIN")

        inventory_has_container = column_exists(
            connection,
            "inventory",
            "container_id",
        )

        if not inventory_has_container:
            connection.execute(
                """
                CREATE TABLE inventory_container_upgrade (
                    inventory_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    location_id INTEGER NOT NULL,
                    container_id VARCHAR(120)
                        NOT NULL DEFAULT '',
                    quantity_on_hand INTEGER NOT NULL,
                    quantity_reserved INTEGER NOT NULL,
                    reorder_level INTEGER NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (inventory_id),
                    CONSTRAINT
                        uq_inventory_product_location_container
                        UNIQUE (
                            product_id,
                            location_id,
                            container_id
                        ),
                    FOREIGN KEY(product_id)
                        REFERENCES products (product_id),
                    FOREIGN KEY(location_id)
                        REFERENCES inventory_locations (
                            location_id
                        )
                )
                """
            )

            connection.execute(
                """
                INSERT INTO inventory_container_upgrade (
                    inventory_id,
                    product_id,
                    location_id,
                    container_id,
                    quantity_on_hand,
                    quantity_reserved,
                    reorder_level,
                    updated_at
                )
                SELECT
                    inventory_id,
                    product_id,
                    location_id,
                    '',
                    quantity_on_hand,
                    quantity_reserved,
                    reorder_level,
                    updated_at
                FROM inventory
                """
            )

            old_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory
                """
            ).fetchone()[0]

            new_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM inventory_container_upgrade
                """
            ).fetchone()[0]

            if old_count != new_count:
                raise RuntimeError(
                    "Inventory row count changed during "
                    f"migration: {old_count} versus "
                    f"{new_count}."
                )

            connection.execute(
                "DROP TABLE inventory"
            )

            connection.execute(
                """
                ALTER TABLE inventory_container_upgrade
                RENAME TO inventory
                """
            )

            connection.execute(
                """
                CREATE INDEX ix_inventory_product_id
                ON inventory (product_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX ix_inventory_location_id
                ON inventory (location_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX ix_inventory_container_id
                ON inventory (container_id)
                """
            )

            print(
                "Inventory table upgraded:",
                new_count,
                "rows preserved.",
            )
        else:
            print(
                "Inventory.container_id already exists."
            )

        transaction_has_container = column_exists(
            connection,
            "inventory_transactions",
            "container_id",
        )

        if not transaction_has_container:
            connection.execute(
                """
                ALTER TABLE inventory_transactions
                ADD COLUMN container_id VARCHAR(120)
                    NOT NULL DEFAULT ''
                """
            )

            connection.execute(
                """
                CREATE INDEX
                    ix_inventory_transactions_container_id
                ON inventory_transactions (container_id)
                """
            )

            print(
                "Inventory transactions upgraded."
            )
        else:
            print(
                "InventoryTransaction.container_id "
                "already exists."
            )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        connection.close()


def patch_inventory_model(
    inventory_section: str,
) -> str:
    inventory_section = inventory_section.replace(
        '''        UniqueConstraint(
            "product_id",
            "location_id",
            name="uq_inventory_product_location",
        ),''',
        '''        UniqueConstraint(
            "product_id",
            "location_id",
            "container_id",
            name=(
                "uq_inventory_product_"
                "location_container"
            ),
        ),''',
        1,
    )

    location_column = '''    location_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_locations.location_id"),
        nullable=False,
        index=True,
    )
'''

    container_column = '''
    container_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default="",
        index=True,
    )
'''

    if "container_id: Mapped[str]" not in inventory_section:
        if location_column not in inventory_section:
            raise RuntimeError(
                "Could not locate Inventory.location_id."
            )

        inventory_section = inventory_section.replace(
            location_column,
            location_column + container_column,
            1,
        )

    return inventory_section


def patch_transaction_model(
    transaction_section: str,
) -> str:
    location_column = '''    location_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_locations.location_id"),
        nullable=False,
        index=True,
    )
'''

    container_column = '''
    container_id: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        default="",
        index=True,
    )
'''

    if (
        "container_id: Mapped[str]"
        not in transaction_section
    ):
        if location_column not in transaction_section:
            raise RuntimeError(
                "Could not locate "
                "InventoryTransaction.location_id."
            )

        transaction_section = (
            transaction_section.replace(
                location_column,
                location_column + container_column,
                1,
            )
        )

    return transaction_section


def patch_models() -> None:
    text = MODELS_PATH.read_text(
        encoding="utf-8"
    )

    inventory_start = text.index(
        "class Inventory(Base):"
    )

    location_start = text.index(
        "class InventoryLocation(Base):",
        inventory_start,
    )

    transaction_start = text.index(
        "class InventoryTransaction(Base):",
        location_start,
    )

    inventory_section = text[
        inventory_start:location_start
    ]

    middle_section = text[
        location_start:transaction_start
    ]

    transaction_section = text[
        transaction_start:
    ]

    inventory_section = patch_inventory_model(
        inventory_section
    )

    transaction_section = patch_transaction_model(
        transaction_section
    )

    updated_text = (
        text[:inventory_start]
        + inventory_section
        + middle_section
        + transaction_section
    )

    MODELS_PATH.write_text(
        updated_text,
        encoding="utf-8",
    )

    print("SQLAlchemy models updated.")


def verify_database() -> None:
    connection = sqlite3.connect(
        DATABASE_PATH
    )

    try:
        inventory_columns = connection.execute(
            'PRAGMA table_info("inventory")'
        ).fetchall()

        transaction_columns = connection.execute(
            'PRAGMA table_info("inventory_transactions")'
        ).fetchall()

        inventory_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM inventory
            """
        ).fetchone()[0]

        blank_container_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM inventory
            WHERE container_id = ''
            """
        ).fetchone()[0]

        print()
        print("Verification")
        print("-" * 50)

        print(
            "Inventory columns:",
            [
                row[1]
                for row in inventory_columns
            ],
        )

        print(
            "Transaction columns:",
            [
                row[1]
                for row in transaction_columns
            ],
        )

        print(
            "Inventory rows:",
            inventory_count,
        )

        print(
            "Existing rows awaiting Container ID:",
            blank_container_count,
        )

    finally:
        connection.close()


def main() -> None:
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(
            f"Database not found: {DATABASE_PATH}"
        )

    if not MODELS_PATH.exists():
        raise FileNotFoundError(
            f"Models file not found: {MODELS_PATH}"
        )

    backup_files()
    migrate_database()
    patch_models()
    verify_database()

    print()
    print(
        "Container ID database migration completed."
    )


if __name__ == "__main__":
    main()
