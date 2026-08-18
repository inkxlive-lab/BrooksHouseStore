"""Import Shopify product pictures into BrooksHouse Store."""

import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from sqlalchemy import inspect, select

from app.database.connection import SessionLocal
from app.database.models import (
    Product,
    ProductBarcode,
    ProductImage,
)


load_dotenv()


STORE = os.getenv(
    "SHOPIFY_STORE",
    "",
).strip()

CLIENT_ID = os.getenv(
    "SHOPIFY_CLIENT_ID",
    "",
).strip()

CLIENT_SECRET = os.getenv(
    "SHOPIFY_CLIENT_SECRET",
    "",
).strip()

API_VERSION = os.getenv(
    "SHOPIFY_API_VERSION",
    "2026-07",
).strip()


PROJECT_DIRECTORY = (
    Path(__file__).resolve().parents[2]
)

IMAGE_DIRECTORY = (
    PROJECT_DIRECTORY
    / "app"
    / "static"
    / "product_images"
    / "shopify"
)

STATIC_URL_PREFIX = (
    "/static/product_images/shopify"
)


GRAPHQL_QUERY = """
query ShopifyProductPictures(
  $cursor: String
) {
  productVariants(
    first: 100
    after: $cursor
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }

    nodes {
      id
      title
      barcode
      sku

      image {
        id
        url
        altText
        width
        height
      }

      product {
        id
        title

        featuredMedia {
          ... on MediaImage {
            id
            alt

            image {
              url
              altText
              width
              height
            }
          }
        }

        media(
          first: 25
        ) {
          nodes {
            mediaContentType

            ... on MediaImage {
              id
              alt

              image {
                url
                altText
                width
                height
              }
            }
          }
        }
      }
    }
  }
}
"""


def require_settings() -> None:
    missing = []

    if not STORE:
        missing.append("SHOPIFY_STORE")

    if not CLIENT_ID:
        missing.append("SHOPIFY_CLIENT_ID")

    if not CLIENT_SECRET:
        missing.append(
            "SHOPIFY_CLIENT_SECRET"
        )

    if missing:
        raise RuntimeError(
            "Missing Shopify settings: "
            + ", ".join(missing)
        )


def get_access_token(
    client: httpx.Client,
) -> str:
    response = client.post(
        (
            f"https://{STORE}"
            "/admin/oauth/access_token"
        ),
        data={
            "grant_type": (
                "client_credentials"
            ),
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )

    response.raise_for_status()

    payload = response.json()

    return payload["access_token"]


def graphql_request(
    client: httpx.Client,
    token: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        (
            f"https://{STORE}"
            f"/admin/api/{API_VERSION}"
            "/graphql.json"
        ),
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": (
                "application/json"
            ),
        },
        json={
            "query": GRAPHQL_QUERY,
            "variables": variables,
        },
    )

    response.raise_for_status()

    payload = response.json()

    if payload.get("errors"):
        raise RuntimeError(
            json.dumps(
                payload["errors"],
                indent=2,
            )
        )

    return payload["data"]


def normalize_barcode(
    value: str | None,
) -> str:
    if not value:
        return ""

    return re.sub(
        r"\D",
        "",
        value,
    )


def barcode_candidates(
    barcode: str,
) -> list[str]:
    exact = normalize_barcode(
        barcode
    )

    if not exact:
        return []

    values = [
        exact,
        exact.lstrip("0") or "0",
    ]

    if len(exact) > 1:
        without_check_digit = (
            exact[:-1]
        )

        values.extend(
            [
                without_check_digit,
                (
                    without_check_digit
                    .lstrip("0")
                    or "0"
                ),
            ]
        )

    return list(
        dict.fromkeys(values)
    )


def find_product(
    database,
    barcode: str,
) -> Product | None:
    candidates = barcode_candidates(
        barcode
    )

    if not candidates:
        return None

    barcode_records = database.scalars(
        select(ProductBarcode)
    ).all()

    for record in barcode_records:
        stored = normalize_barcode(
            getattr(
                record,
                "barcode",
                "",
            )
        )

        if not stored:
            continue

        stored_candidates = (
            barcode_candidates(
                stored
            )
        )

        if any(
            candidate
            in stored_candidates
            for candidate in candidates
        ):
            return record.product

    return None


def collect_images(
    variant: dict[str, Any],
) -> list[dict[str, Any]]:
    collected: list[
        dict[str, Any]
    ] = []

    seen_urls: set[str] = set()

    def add_image(
        image: dict[str, Any] | None,
        media_id: str | None,
        source_type: str,
        fallback_alt: str = "",
    ) -> None:
        if not image:
            return

        url = str(
            image.get("url") or ""
        ).strip()

        if not url:
            return

        if url in seen_urls:
            return

        seen_urls.add(url)

        collected.append(
            {
                "url": url,
                "media_id": media_id,
                "source_type": source_type,
                "alt_text": (
                    image.get("altText")
                    or fallback_alt
                    or variant.get(
                        "product",
                        {},
                    ).get(
                        "title",
                        "",
                    )
                ),
                "width": image.get(
                    "width"
                ),
                "height": image.get(
                    "height"
                ),
            }
        )

    variant_image = variant.get(
        "image"
    )

    add_image(
        image=variant_image,
        media_id=(
            variant_image.get("id")
            if variant_image
            else None
        ),
        source_type="variant",
        fallback_alt=variant.get(
            "title",
            "",
        ),
    )

    product = (
        variant.get("product")
        or {}
    )

    featured_media = (
        product.get(
            "featuredMedia"
        )
        or {}
    )

    add_image(
        image=featured_media.get(
            "image"
        ),
        media_id=featured_media.get(
            "id"
        ),
        source_type="featured",
        fallback_alt=(
            featured_media.get("alt")
            or product.get(
                "title",
                "",
            )
        ),
    )

    media_connection = (
        product.get("media")
        or {}
    )

    for media in media_connection.get(
        "nodes",
        [],
    ):
        if (
            media.get(
                "mediaContentType"
            )
            != "IMAGE"
        ):
            continue

        add_image(
            image=media.get("image"),
            media_id=media.get("id"),
            source_type=(
                "product_media"
            ),
            fallback_alt=(
                media.get("alt")
                or product.get(
                    "title",
                    "",
                )
            ),
        )

    return collected


def choose_extension(
    image_url: str,
    content_type: str,
) -> str:
    parsed_path = urlparse(
        image_url
    ).path

    suffix = Path(
        parsed_path
    ).suffix.lower()

    allowed_suffixes = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".avif",
    }

    if suffix in allowed_suffixes:
        return suffix

    guessed = mimetypes.guess_extension(
        content_type.split(
            ";",
            1,
        )[0]
    )

    if guessed:
        return guessed

    return ".jpg"


def safe_filename(
    product_id: int,
    image_url: str,
    media_id: str | None,
    extension: str,
) -> str:
    identity = (
        media_id
        or image_url
    )

    digest = hashlib.sha256(
        identity.encode(
            "utf-8"
        )
    ).hexdigest()[:16]

    return (
        f"product-{product_id}-"
        f"{digest}{extension}"
    )


def download_image(
    client: httpx.Client,
    product_id: int,
    image_data: dict[str, Any],
) -> tuple[Path, str]:
    image_url = image_data["url"]

    response = client.get(
        image_url,
        follow_redirects=True,
    )

    response.raise_for_status()

    content_type = response.headers.get(
        "content-type",
        "image/jpeg",
    )

    if not content_type.lower().startswith(
        "image/"
    ):
        raise RuntimeError(
            f"URL did not return an image: "
            f"{image_url}"
        )

    extension = choose_extension(
        image_url=image_url,
        content_type=content_type,
    )

    filename = safe_filename(
        product_id=product_id,
        image_url=image_url,
        media_id=image_data.get(
            "media_id"
        ),
        extension=extension,
    )

    local_path = (
        IMAGE_DIRECTORY
        / filename
    )

    if not local_path.exists():
        local_path.write_bytes(
            response.content
        )

    static_url = (
        f"{STATIC_URL_PREFIX}/"
        f"{filename}"
    )

    return local_path, static_url


def model_columns() -> set[str]:
    mapper = inspect(
        ProductImage
    )

    return {
        column.key
        for column in mapper.columns
    }


def image_exists(
    database,
    product_id: int,
    image_url: str,
    static_url: str,
    columns: set[str],
) -> bool:
    records = database.scalars(
        select(ProductImage).where(
            ProductImage.product_id
            == product_id
        )
    ).all()

    possible_fields = [
        "image_url",
        "source_url",
        "external_url",
        "url",
        "image_path",
        "local_path",
        "file_path",
    ]

    for record in records:
        for field_name in possible_fields:
            if field_name not in columns:
                continue

            value = getattr(
                record,
                field_name,
                None,
            )

            if value in {
                image_url,
                static_url,
            }:
                return True

    return False


def create_image_record(
    product: Product,
    image_data: dict[str, Any],
    local_path: Path,
    static_url: str,
    columns: set[str],
    is_primary: bool,
) -> ProductImage:
    values: dict[str, Any] = {}

    if "product_id" in columns:
        values["product_id"] = (
            product.product_id
        )

    url_field_names = [
        "image_url",
        "url",
    ]

    for field_name in url_field_names:
        if field_name in columns:
            values[field_name] = (
                static_url
            )

            break

    source_url_fields = [
        "source_url",
        "external_url",
    ]

    for field_name in source_url_fields:
        if field_name in columns:
            values[field_name] = (
                image_data["url"]
            )

            break

    path_fields = [
        "image_path",
        "local_path",
        "file_path",
    ]

    for field_name in path_fields:
        if field_name in columns:
            values[field_name] = str(
                local_path
            )

            break

    if "alt_text" in columns:
        values["alt_text"] = (
            image_data.get(
                "alt_text"
            )
            or product.product_name
        )

    if "caption" in columns:
        values["caption"] = (
            image_data.get(
                "alt_text"
            )
            or product.product_name
        )

    if "is_primary" in columns:
        values["is_primary"] = (
            is_primary
        )

    if "primary_image" in columns:
        values["primary_image"] = (
            is_primary
        )

    if "source" in columns:
        values["source"] = "Shopify"

    if "image_source" in columns:
        values["image_source"] = (
            "Shopify"
        )

    if "external_id" in columns:
        values["external_id"] = (
            image_data.get(
                "media_id"
            )
        )

    if "width" in columns:
        values["width"] = (
            image_data.get("width")
        )

    if "height" in columns:
        values["height"] = (
            image_data.get("height")
        )

    return ProductImage(
        **values
    )


def main() -> None:
    require_settings()

    IMAGE_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    columns = model_columns()

    print()
    print("Shopify image import")
    print("--------------------")

    print(
        "ProductImage fields: "
        + ", ".join(
            sorted(columns)
        )
    )

    variants_read = 0
    products_matched = 0
    products_not_matched = 0
    images_downloaded = 0
    image_records_created = 0
    duplicate_images_skipped = 0
    download_errors = 0

    matched_product_ids: set[
        int
    ] = set()

    cursor = None

    with httpx.Client(
        timeout=60,
        follow_redirects=True,
    ) as client:
        token = get_access_token(
            client
        )

        with SessionLocal() as database:
            while True:
                data = graphql_request(
                    client=client,
                    token=token,
                    variables={
                        "cursor": cursor,
                    },
                )

                connection = data[
                    "productVariants"
                ]

                for variant in connection[
                    "nodes"
                ]:
                    variants_read += 1

                    barcode = normalize_barcode(
                        variant.get(
                            "barcode"
                        )
                    )

                    if not barcode:
                        products_not_matched += 1
                        continue

                    product = find_product(
                        database=database,
                        barcode=barcode,
                    )

                    if product is None:
                        products_not_matched += 1
                        continue

                    if (
                        product.product_id
                        not in matched_product_ids
                    ):
                        matched_product_ids.add(
                            product.product_id
                        )

                        products_matched += 1

                    images = collect_images(
                        variant
                    )

                    existing_images = (
                        database.scalars(
                            select(
                                ProductImage
                            ).where(
                                ProductImage.product_id
                                == product.product_id
                            )
                        ).all()
                    )

                    has_primary = any(
                        bool(
                            getattr(
                                record,
                                "is_primary",
                                False,
                            )
                        )
                        for record
                        in existing_images
                    )

                    for image_data in images:
                        try:
                            local_path, static_url = (
                                download_image(
                                    client=client,
                                    product_id=(
                                        product.product_id
                                    ),
                                    image_data=(
                                        image_data
                                    ),
                                )
                            )

                            if image_exists(
                                database=database,
                                product_id=(
                                    product.product_id
                                ),
                                image_url=(
                                    image_data[
                                        "url"
                                    ]
                                ),
                                static_url=(
                                    static_url
                                ),
                                columns=columns,
                            ):
                                duplicate_images_skipped += 1
                                continue

                            image_record = (
                                create_image_record(
                                    product=product,
                                    image_data=(
                                        image_data
                                    ),
                                    local_path=(
                                        local_path
                                    ),
                                    static_url=(
                                        static_url
                                    ),
                                    columns=columns,
                                    is_primary=(
                                        not has_primary
                                    ),
                                )
                            )

                            database.add(
                                image_record
                            )

                            database.flush()

                            has_primary = True
                            images_downloaded += 1
                            image_records_created += 1

                        except Exception as error:
                            download_errors += 1

                            print(
                                "Image error | "
                                f"{product.product_name} | "
                                f"{error}"
                            )

                    database.commit()

                page_info = connection[
                    "pageInfo"
                ]

                print(
                    f"Variants processed: "
                    f"{variants_read}"
                )

                if not page_info[
                    "hasNextPage"
                ]:
                    break

                cursor = page_info[
                    "endCursor"
                ]

    print()
    print("Shopify image import complete")
    print("-----------------------------")
    print(
        f"Variants read: "
        f"{variants_read}"
    )
    print(
        f"BrooksHouse products matched: "
        f"{products_matched}"
    )
    print(
        f"Variants not matched: "
        f"{products_not_matched}"
    )
    print(
        f"Images downloaded: "
        f"{images_downloaded}"
    )
    print(
        f"Image records created: "
        f"{image_records_created}"
    )
    print(
        f"Duplicate images skipped: "
        f"{duplicate_images_skipped}"
    )
    print(
        f"Download errors: "
        f"{download_errors}"
    )
    print()
    print(
        "Local image folder:"
    )
    print(IMAGE_DIRECTORY)


if __name__ == "__main__":
    main()
