"""Explicit, provenance-preserving Smart Lookup product-image saves."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlparse


def save_lookup_image(
    connection: sqlite3.Connection, *, product_id: int, image_url: str,
    make_primary: bool = False, created_at: str,
) -> int:
    value = str(image_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Smart Lookup gallery images must use a valid HTTP or HTTPS URL.")
    duplicate = connection.execute(
        "SELECT image_id FROM product_images WHERE product_id=? AND image_url=? LIMIT 1",
        (product_id, value),
    ).fetchone()
    if duplicate is not None:
        return int(duplicate[0])
    if make_primary:
        connection.execute("UPDATE product_images SET is_primary=0 WHERE product_id=?", (product_id,))
    cursor = connection.execute(
        """INSERT INTO product_images
           (product_id,image_path,image_url,image_type,is_primary,created_at)
           VALUES (?,NULL,?,'internet_lookup',?,?)""",
        (product_id, value, int(make_primary), created_at),
    )
    return int(cursor.lastrowid)
