from sqlalchemy import select

from app.database.connection import Base, SessionLocal, DATABASE_PATH, engine
from app.database.models import InventoryLocation


def create_database() -> None:
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as database:
        existing_location = database.scalar(
            select(InventoryLocation).where(
                InventoryLocation.location_name == "BrooksHouse Storefront"
            )
        )

        if existing_location is None:
            storefront = InventoryLocation(
                location_name="BrooksHouse Storefront",
                location_type="store",
                description="Main BrooksHouse Store retail location",
                active=True,
            )

            database.add(storefront)
            database.commit()

    print("")
    print("BrooksHouse Store database created successfully.")
    print(f"Database file: {DATABASE_PATH}")
    print("Default location: BrooksHouse Storefront")


if __name__ == "__main__":
    create_database()
