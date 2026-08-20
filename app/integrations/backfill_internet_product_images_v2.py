from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

from app.integrations.product_lookup import lookup_upc_online


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "app" / "data" / "brookshouse_store.db"

CANDIDATE_DIR = (
    PROJECT_ROOT
    / "app"
    / "static"
    / "product-images"
    / "candidates-v2"
)

REPORT_PATH = (
    PROJECT_ROOT
    / "reports"
    / "image-candidate-review-v2.html"
)


def clean_barcode(value):
    return "".join(
        ch for ch in str(value or "").strip()
        if ch.isdigit()
    )


def get_brave_key():
    key = os.getenv("BRAVE_SEARCH_API_KEY")

    if key:
        return key.strip()

    env_path = PROJECT_ROOT / ".env"

    if not env_path.exists():
        return None

    for raw in env_path.read_text(
        encoding="utf-8",
        errors="ignore",
    ).splitlines():

        raw = raw.strip()

        if (
            not raw
            or raw.startswith("#")
            or "=" not in raw
        ):
            continue

        name, value = raw.split("=", 1)

        if name.strip() == "BRAVE_SEARCH_API_KEY":
            return (
                value.strip()
                .strip('"')
                .strip("'")
                or None
            )

    return None


def master_description(conn, barcode):
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(master_catalog)"
        )
    }

    if not columns:
        return None

    barcode_columns = [
        name
        for name in (
            "barcode_exact",
            "barcode_lookup",
            "barcode_raw",
            "barcode",
        )
        if name in columns
    ]

    description_columns = [
        name
        for name in (
            "description",
            "product_name",
            "name",
        )
        if name in columns
    ]

    if not barcode_columns or not description_columns:
        return None

    for barcode_col in barcode_columns:
        for description_col in description_columns:

            try:
                row = conn.execute(
                    f"""
                    SELECT {description_col}
                    FROM master_catalog
                    WHERE {barcode_col} = ?
                    LIMIT 1
                    """,
                    (barcode,),
                ).fetchone()
            except Exception:
                continue

            if row and row[0]:
                return str(row[0]).strip()

    return None


def preferred_url_score(url):
    value = (url or "").lower()

    preferred = [
        ("walmartimages.com", 100),
        ("target.scene7.com", 95),
        ("walgreens.com", 90),
        ("amazon", 85),
        ("homedepot", 80),
        ("lowes", 80),
        ("office", 70),
    ]

    bad = [
        ("shld.net", -25),
        ("frys.com", -25),
        ("rackcdn.com", -10),
        ("unbeatablesale", -10),
    ]

    score = 0

    for term, points in preferred:
        if term in value:
            score += points

    for term, points in bad:
        if term in value:
            score += points

    if value.startswith("https://"):
        score += 10

    return score


def token_overlap_score(text, reference):
    stop = {
        "the", "and", "with", "for", "pack",
        "assorted", "item", "product"
    }

    ref_tokens = {
        x.lower()
        for x in re.findall(r"[A-Za-z0-9]+", reference or "")
        if len(x) > 2 and x.lower() not in stop
    }

    text_tokens = {
        x.lower()
        for x in re.findall(r"[A-Za-z0-9]+", text or "")
        if len(x) > 2 and x.lower() not in stop
    }

    return len(ref_tokens & text_tokens) * 10


def upc_candidates(barcode, reference):
    result = lookup_upc_online(barcode) or {}

    if not isinstance(result, dict):
        return [], result

    images = result.get("images") or []

    if isinstance(images, str):
        images = [images]

    candidates = []

    if isinstance(images, (list, tuple)):
        for index, url in enumerate(images):

            if not isinstance(url, str):
                continue

            url = url.strip()

            if not url.startswith(("http://", "https://")):
                continue

            score = (
                500
                + preferred_url_score(url)
                - index
            )

            candidates.append({
                "url": url,
                "source": "UPCitemdb",
                "title": result.get("title") or "",
                "score": score,
            })

    extra = (
        result.get("image_url")
        or result.get("image")
        or result.get("thumbnail")
    )

    if (
        isinstance(extra, str)
        and extra.startswith(("http://", "https://"))
    ):
        candidates.append({
            "url": extra,
            "source": str(
                result.get("source")
                or "UPC lookup"
            ),
            "title": result.get("title") or "",
            "score": 450 + preferred_url_score(extra),
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    return candidates, result


def brave_candidates(barcode, reference, count=10):
    key = get_brave_key()

    if not key:
        return []

    # Exact UPC remains the strongest identifier.
    if reference:
        query = f'{barcode} "{reference}"'
    else:
        query = barcode

    url = (
        "https://api.search.brave.com/res/v1/images/search"
        f"?q={quote_plus(query)}"
        "&country=US"
        "&search_lang=en"
        "&safesearch=strict"
        f"&count={count}"
    )

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": key,
            "User-Agent": (
                "Mozilla/5.0 BrooksHouseStore/2.0"
            ),
        },
    )

    try:
        with urlopen(request, timeout=25) as response:
            payload = json.loads(
                response.read().decode(
                    "utf-8",
                    errors="replace",
                )
            )
    except Exception as exc:
        print("  Brave error:", exc)
        return []

    candidates = []

    for index, result in enumerate(
        payload.get("results") or []
    ):
        if not isinstance(result, dict):
            continue

        title = str(
            result.get("title")
            or result.get("description")
            or ""
        )

        properties = result.get("properties") or {}
        candidate_urls = []

        if isinstance(properties, dict):
            value = properties.get("url")
            if isinstance(value, str):
                candidate_urls.append(value)

        thumb = result.get("thumbnail")

        if isinstance(thumb, dict):
            for field in ("original", "src"):
                value = thumb.get(field)
                if isinstance(value, str):
                    candidate_urls.append(value)

        elif isinstance(thumb, str):
            candidate_urls.append(thumb)

        for candidate_url in candidate_urls:

            if not candidate_url.startswith(
                ("http://", "https://")
            ):
                continue

            score = (
                200
                + preferred_url_score(candidate_url)
                + token_overlap_score(
                    title,
                    reference,
                )
                - index
            )

            candidates.append({
                "url": candidate_url,
                "source": "Brave Image Search",
                "title": title,
                "score": score,
            })

            break

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    return candidates


def extension_for(url, content_type):
    suffix = Path(
        urlparse(url).path
    ).suffix.lower()

    if suffix in {
        ".jpg", ".jpeg", ".png",
        ".webp", ".gif"
    }:
        return ".jpg" if suffix == ".jpeg" else suffix

    if content_type:
        content_type = (
            content_type
            .split(";", 1)[0]
            .strip()
        )

        guessed = mimetypes.guess_extension(
            content_type
        )

        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed

    return ".jpg"


def download_candidate(
    url,
    product_id,
    barcode,
    candidate_number,
):
    CANDIDATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 BrooksHouseStore/2.0"
            )
        },
    )

    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get(
            "Content-Type",
            "",
        )

        data = response.read(
            12 * 1024 * 1024 + 1
        )

    if not data:
        raise RuntimeError("empty download")

    if len(data) > 12 * 1024 * 1024:
        raise RuntimeError("image larger than 12MB")

    extension = extension_for(
        url,
        content_type,
    )

    filename = (
        f"{product_id}-{barcode}"
        f"-candidate-{candidate_number}"
        f"{extension}"
    )

    path = CANDIDATE_DIR / filename
    path.write_bytes(data)

    return path


def dedupe_candidates(candidates):
    seen = set()
    output = []

    for item in candidates:
        url = item["url"]

        if url in seen:
            continue

        seen.add(url)
        output.append(item)

    return output


def build_gallery(products):
    REPORT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    cards = []

    for product in products:

        candidate_html = []

        for candidate in product["downloaded"]:

            uri = candidate["path"].resolve().as_uri()

            candidate_html.append(f"""
            <div class="candidate">
                <img src="{html.escape(uri)}">

                <div class="source">
                    {html.escape(candidate["source"])}
                </div>

                <div class="score">
                    Score: {candidate["score"]}
                </div>

                <div class="ctitle">
                    {html.escape(candidate["title"] or "")}
                </div>

                <div class="url">
                    {html.escape(candidate["url"])}
                </div>
            </div>
            """)

        cards.append(f"""
        <section class="product">
            <h2>
                #{product["product_id"]}
                — {html.escape(product["name"])}
            </h2>

            <div>
                <strong>Barcode:</strong>
                {html.escape(product["barcode"])}
            </div>

            <div>
                <strong>Resolved search title:</strong>
                {html.escape(product["reference"] or "")}
            </div>

            <div class="candidates">
                {''.join(candidate_html)}
            </div>
        </section>
        """)

    page = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>BrooksHouse V2 Candidate Review</title>

<style>
body {{
    font-family: Arial, sans-serif;
    background: #f4f4f4;
    margin: 25px;
}}

.product {{
    background: white;
    padding: 18px;
    margin-bottom: 25px;
    border-radius: 12px;
}}

.candidates {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(250px, 1fr));
    gap: 15px;
    margin-top: 15px;
}}

.candidate {{
    border: 1px solid #ddd;
    padding: 12px;
    border-radius: 10px;
}}

.candidate img {{
    width: 100%;
    height: 240px;
    object-fit: contain;
}}

.source {{
    font-weight: bold;
    margin-top: 8px;
}}

.score {{
    margin-top: 4px;
}}

.ctitle {{
    margin-top: 8px;
}}

.url {{
    margin-top: 8px;
    font-size: 10px;
    color: #777;
    word-break: break-all;
}}
</style>
</head>

<body>

<h1>BrooksHouse V2 Image Candidate Test</h1>

<p>
No product_images records were changed.
These are candidate downloads only.
</p>

{''.join(cards)}

</body>
</html>
"""

    REPORT_PATH.write_text(
        page,
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--product-ids",
        default="429,430,432,439,450",
        help="Comma-separated product IDs",
    )

    parser.add_argument(
        "--candidates",
        type=int,
        default=3,
        help="Candidates to successfully download per product",
    )

    args = parser.parse_args()

    ids = [
        int(x.strip())
        for x in args.product_ids.split(",")
        if x.strip()
    ]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    completed = []

    for product_id in ids:

        row = conn.execute(
            """
            SELECT
                p.product_id,
                p.product_name,
                pb.barcode
            FROM products p
            JOIN product_barcodes pb
              ON pb.product_id = p.product_id
             AND pb.is_primary = 1
            WHERE p.product_id = ?
            """,
            (product_id,),
        ).fetchone()

        if not row:
            print(
                f"Product {product_id}: not found"
            )
            continue

        barcode = clean_barcode(
            row["barcode"]
        )

        current_name = str(
            row["product_name"] or ""
        ).strip()

        master_name = master_description(
            conn,
            barcode,
        )

        reference = (
            master_name
            or current_name
        )

        print()
        print("=" * 75)
        print(
            f"Product #{product_id}"
            f" | {barcode}"
            f" | {current_name}"
        )
        print("Search title:", reference)

        upc, lookup_data = upc_candidates(
            barcode,
            reference,
        )

        if (
            isinstance(lookup_data, dict)
            and lookup_data.get("title")
        ):
            resolved_title = str(
                lookup_data["title"]
            ).strip()

            if resolved_title:
                reference = resolved_title
                print(
                    "UPC resolved title:",
                    reference,
                )

        brave = brave_candidates(
            barcode,
            reference,
            count=10,
        )

        all_candidates = dedupe_candidates(
            upc + brave
        )

        all_candidates.sort(
            key=lambda x: x["score"],
            reverse=True,
        )

        downloaded = []

        for candidate in all_candidates:

            if len(downloaded) >= args.candidates:
                break

            try:
                path = download_candidate(
                    candidate["url"],
                    product_id,
                    barcode,
                    len(downloaded) + 1,
                )

            except Exception as exc:
                print(
                    "  SKIP:",
                    candidate["source"],
                    str(exc),
                )
                continue

            item = dict(candidate)
            item["path"] = path
            downloaded.append(item)

            print(
                f"  Candidate {len(downloaded)}:",
                candidate["source"],
                "| score",
                candidate["score"],
            )

        completed.append({
            "product_id": product_id,
            "barcode": barcode,
            "name": current_name,
            "reference": reference,
            "downloaded": downloaded,
        })

    conn.close()

    build_gallery(completed)

    print()
    print("=" * 75)
    print("V2 TEST COMPLETE")
    print("=" * 75)
    print(
        "Database writes: NONE"
    )
    print(
        "Candidate folder:",
        CANDIDATE_DIR,
    )
    print(
        "Review gallery:",
        REPORT_PATH,
    )


if __name__ == "__main__":
    main()
