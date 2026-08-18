import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)


def required_setting(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"{name} is missing from {ENV_PATH}"
        )

    return value


def clean_store_domain(value: str) -> str:
    store = value.strip()

    store = store.removeprefix("https://")
    store = store.removeprefix("http://")
    store = store.rstrip("/")

    if not store.endswith(".myshopify.com"):
        raise RuntimeError(
            "SHOPIFY_STORE must use the "
            ".myshopify.com domain."
        )

    return store


def request_access_token(
    store: str,
    client_id: str,
    client_secret: str,
) -> str:
    token_url = (
        f"https://{store}/admin/oauth/access_token"
    )

    request_body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        token_url,
        data=request_body,
        method="POST",
        headers={
            "Content-Type": (
                "application/x-www-form-urlencoded"
            ),
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        response_text = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Shopify rejected the token request. "
            f"HTTP {error.code}: {response_text}"
        ) from error

    access_token = result.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Shopify did not return an access token."
        )

    return access_token


def run_graphql_query(
    store: str,
    api_version: str,
    access_token: str,
    query: str,
) -> dict:
    graphql_url = (
        f"https://{store}/admin/api/"
        f"{api_version}/graphql.json"
    )

    request_body = json.dumps(
        {"query": query}
    ).encode("utf-8")

    request = urllib.request.Request(
        graphql_url,
        data=request_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        response_text = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Shopify rejected the GraphQL request. "
            f"HTTP {error.code}: {response_text}"
        ) from error

    if result.get("errors"):
        raise RuntimeError(
            "Shopify GraphQL errors: "
            + json.dumps(
                result["errors"],
                indent=2,
            )
        )

    return result


def main() -> None:
    store = clean_store_domain(
        required_setting("SHOPIFY_STORE")
    )

    client_id = required_setting(
        "SHOPIFY_CLIENT_ID"
    )

    client_secret = required_setting(
        "SHOPIFY_CLIENT_SECRET"
    )

    api_version = os.getenv(
        "SHOPIFY_API_VERSION",
        "2026-07",
    ).strip()

    print("Requesting Shopify access token...")

    access_token = request_access_token(
        store=store,
        client_id=client_id,
        client_secret=client_secret,
    )

    print("Access token received successfully.")
    print("Testing read-only Shopify connection...")

    query = """
    query BrooksHouseConnectionTest {
      shop {
        name
        myshopifyDomain
        currencyCode
        plan {
          displayName
        }
      }
      productVariants(first: 3) {
        nodes {
          id
          title
          sku
          barcode
          price
          product {
            id
            title
            status
            vendor
          }
        }
      }
    }
    """

    result = run_graphql_query(
        store=store,
        api_version=api_version,
        access_token=access_token,
        query=query,
    )

    data = result["data"]
    shop = data["shop"]
    variants = data["productVariants"]["nodes"]

    print("")
    print("Shopify connection successful!")
    print(f"Store: {shop['name']}")
    print(
        "Shopify domain: "
        f"{shop['myshopifyDomain']}"
    )
    print(f"Currency: {shop['currencyCode']}")
    print(
        "Plan: "
        f"{shop['plan']['displayName']}"
    )

    print("")
    print("First product variants:")

    if not variants:
        print("No product variants were returned.")

    for variant in variants:
        product = variant["product"]

        print("-" * 50)
        print(f"Product: {product['title']}")
        print(f"Status: {product['status']}")
        print(f"Variant: {variant['title']}")
        print(f"SKU: {variant['sku'] or 'None'}")
        print(
            f"Barcode: "
            f"{variant['barcode'] or 'None'}"
        )
        print(f"Price: ${variant['price']}")


if __name__ == "__main__":
    main()
