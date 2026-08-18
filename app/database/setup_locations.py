from sqlalchemy import select

from app.database.connection import SessionLocal
from app.database.models import InventoryLocation


LOCATIONS = [
    {
        "location_name": "BrooksHouse Storefront",
        "location_type": "store",
        "description": "Inventory physically available on the retail sales floor.",
    },
    {
        "location_name": "Store Back Room",
        "location_type": "storage",
        "description": "Inventory stored in the back room of the storefront.",
    },
    {
        "location_name": "Warehouse",
        "location_type": "warehouse",
        "description": "Primary warehouse and reserve inventory.",
    },
    {
        "location_name": "Trailer 1",
        "location_type": "trailer",
        "description": "Inventory stored in Trailer 1.",
    },
    {
        "location_name": "Trailer 2",
        "location_type": "trailer",
        "description": "Inventory stored in Trailer 2.",
    },
    {
        "location_name": "Trailer 3",
        "location_type": "trailer",
        "description": "Inventory stored in Trailer 3.",
    },
    {
        "location_name": "Storage Container",
        "location_type": "container",
        "description": "Inventory stored inside the storage container.",
    },
    {
        "location_name": "On-the-Road Trailer",
        "location_type": "mobile_inventory",
        "description": "Inventory currently loaded for transport, deliveries, or mobile sales.",
    },
    {
        "location_name": "Online Orders / Reserved",
        "location_type": "reserved",
        "description": "Inventory reserved for online, pickup, or pending orders.",
    },
    {
        "location_name": "Damaged / Returns",
        "location_type": "hold",
        "description": "Returned, damaged, incomplete, or temporarily unsellable inventory.",
    },
]


def setup_locations() -> None:
    created_count = 0
    updated_count = 0

    with SessionLocal() as database:
        for location_data in LOCATIONS:
            location = database.scalar(
                select(InventoryLocation).where(
                    InventoryLocation.location_name
                    == location_data["location_name"]
                )
            )

            if location is None:
                location = InventoryLocation(
                    location_name=location_data["location_name"],
                    location_type=location_data["location_type"],
                    description=location_data["description"],
                    active=True,
                )

                database.add(location)
                created_count += 1

            else:
                location.location_type = location_data["location_type"]
                location.description = location_data["description"]
                location.active = True
                updated_count += 1

        # Retire the original generic Trailer location if it exists.
        generic_trailer = database.scalar(
            select(InventoryLocation).where(
                InventoryLocation.location_name == "Trailer"
            )
        )

        if generic_trailer is not None:
            generic_trailer.active = False

        database.commit()

        locations = database.scalars(
            select(InventoryLocation).order_by(
                InventoryLocation.location_id
            )
        ).all()

    print()
    print("Inventory locations are ready.")
    print(f"Created: {created_count}")
    print(f"Updated: {updated_count}")
    print()

    for location in locations:
        status = "ACTIVE" if location.active else "INACTIVE"

        print(
            f"{location.location_id:>3} | "
            f"{location.location_name:<28} | "
            f"{location.location_type:<18} | "
            f"{status}"
        )


if __name__ == "__main__":
    setup_locations()
