"""Pure helpers for Smart Scan product updates."""
from __future__ import annotations


def approved_product_update_values(
    product_name: str | None,
    description: str | None,
    brand: str | None,
    category: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Keep title and description distinct when binding the update query."""
    return product_name, description, brand, category
