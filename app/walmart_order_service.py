import base64
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4

APP_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = APP_ROOT / "app" / "data" / "brookshouse_store.db"
ENV_PATH = APP_ROOT / ".env"


def load_local_env():
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def walmart_request(method, path, params=None, payload=None):
    load_local_env()
    client_id = os.environ.get("WALMART_CLIENT_ID", "").strip()
    client_secret = os.environ.get("WALMART_CLIENT_SECRET", "").strip()
    base_url = os.environ.get(
        "WALMART_API_BASE_URL", "https://marketplace.walmartapis.com"
    ).strip().rstrip("/")
    if not client_id or not client_secret:
        raise RuntimeError("Walmart API credentials are not configured in .env.")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
    token_request = UrlRequest(
        f"{base_url}/v3/token",
        data=urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "WM_SVC.NAME": "Walmart Marketplace",
            "WM_QOS.CORRELATION_ID": str(uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    token_data = _open_json(token_request)
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("Walmart did not return an access token.")

    url = f"{base_url}{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = UrlRequest(
        url,
        data=body,
        method=method.upper(),
        headers={
            "WM_SEC.ACCESS_TOKEN": access_token,
            "WM_SVC.NAME": "Walmart Marketplace",
            "WM_QOS.CORRELATION_ID": str(uuid4()),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    return _open_json(request)


def _open_json(request):
    try:
        with urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Walmart API returned {error.code}: {detail[:700]}") from error
    except URLError as error:
        raise RuntimeError(f"Could not connect to Walmart: {error.reason}") from error


def ensure_order_tables():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS walmart_orders (
                purchase_order_id TEXT PRIMARY KEY,
                customer_order_id TEXT,
                order_date TEXT,
                ship_by_date TEXT,
                walmart_status TEXT,
                local_status TEXT NOT NULL DEFAULT 'new',
                acknowledged_at TEXT,
                packed_at TEXT,
                shipped_at TEXT,
                carrier TEXT,
                tracking_number TEXT,
                order_total REAL NOT NULL DEFAULT 0,
                currency TEXT,
                raw_json TEXT,
                synced_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS walmart_order_lines (
                order_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_order_id TEXT NOT NULL,
                line_number TEXT NOT NULL,
                sku TEXT,
                upc TEXT,
                item_name TEXT,
                quantity INTEGER NOT NULL DEFAULT 1,
                pulled_quantity INTEGER NOT NULL DEFAULT 0,
                inventory_id INTEGER,
                product_id INTEGER,
                line_status TEXT,
                line_total REAL,
                currency TEXT,
                UNIQUE(purchase_order_id, line_number)
            );
            CREATE INDEX IF NOT EXISTS ix_walmart_order_lines_po
                ON walmart_order_lines(purchase_order_id);
            CREATE TABLE IF NOT EXISTS walmart_order_allocations (
                allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_line_id INTEGER NOT NULL,
                inventory_id INTEGER NOT NULL,
                planned_quantity INTEGER NOT NULL DEFAULT 0,
                pulled_quantity INTEGER NOT NULL DEFAULT 0,
                UNIQUE(order_line_id, inventory_id)
            );
            CREATE TABLE IF NOT EXISTS walmart_listings (
                walmart_listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_sku TEXT NOT NULL UNIQUE,
                item_name TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS walmart_product_links (
                walmart_product_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
                walmart_listing_id INTEGER NOT NULL UNIQUE,
                product_id INTEGER,
                match_status TEXT NOT NULL DEFAULT 'linked',
                matched_at TEXT
            );
            """
        )
        order_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(walmart_orders)"
            ).fetchall()
        }
        if "picked_at" not in order_columns:
            connection.execute(
                "ALTER TABLE walmart_orders ADD COLUMN picked_at TEXT"
            )
        if "staged_at" not in order_columns:
            connection.execute(
                "ALTER TABLE walmart_orders ADD COLUMN staged_at TEXT"
            )
        if "order_total" not in order_columns:
            connection.execute(
                "ALTER TABLE walmart_orders ADD COLUMN order_total REAL NOT NULL DEFAULT 0"
            )
        if "currency" not in order_columns:
            connection.execute(
                "ALTER TABLE walmart_orders ADD COLUMN currency TEXT"
            )
        line_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(walmart_order_lines)"
            ).fetchall()
        }
        if "line_total" not in line_columns:
            connection.execute(
                "ALTER TABLE walmart_order_lines ADD COLUMN line_total REAL"
            )
        if "currency" not in line_columns:
            connection.execute(
                "ALTER TABLE walmart_order_lines ADD COLUMN currency TEXT"
            )
        connection.commit()
    finally:
        connection.close()


def search_mapping_products(search_text="", limit=40):
    ensure_order_tables()
    term = str(search_text or "").strip()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        parameters = []
        where = "WHERE COALESCE(p.active, 1) = 1"
        if term:
            like = f"%{term.replace('*', '%').replace('?', '_')}%"
            digits = "".join(character for character in term if character.isdigit())
            where += """
                AND (p.product_name LIKE ? COLLATE NOCASE
                     OR CAST(p.product_id AS TEXT) = ?
                     OR EXISTS (SELECT 1 FROM product_barcodes pb
                                WHERE pb.product_id=p.product_id
                                  AND CAST(pb.barcode AS TEXT) LIKE ?))
            """
            parameters.extend((like, term, f"%{digits or term}%"))
        rows = connection.execute(
            f"""SELECT p.product_id, p.product_name,
                       (SELECT pb.barcode FROM product_barcodes pb
                        WHERE pb.product_id=p.product_id
                        ORDER BY pb.is_primary DESC, pb.barcode_id LIMIT 1) AS barcode
                FROM products p {where}
                ORDER BY p.product_name COLLATE NOCASE, p.product_id LIMIT ?""",
            (*parameters, max(1, min(100, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def save_walmart_product_mapping(order_line_id, product_id):
    ensure_order_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    now = datetime.now().astimezone().isoformat()
    try:
        line = connection.execute("SELECT * FROM walmart_order_lines WHERE order_line_id=?", (int(order_line_id),)).fetchone()
        product = connection.execute("SELECT product_id, product_name FROM products WHERE product_id=?", (int(product_id),)).fetchone()
        if line is None:
            raise ValueError("The Walmart order line was not found.")
        if product is None:
            raise ValueError("The BrooksHouse product was not found.")
        sku = str(line["sku"] or "").strip()
        if not sku:
            raise ValueError("This Walmart line does not have a seller SKU to map.")
        listing_columns = {row[1] for row in connection.execute("PRAGMA table_info(walmart_listings)")}
        listing = connection.execute("SELECT walmart_listing_id FROM walmart_listings WHERE TRIM(seller_sku)=? COLLATE NOCASE LIMIT 1", (sku,)).fetchone()
        if listing is None:
            fields, values = ["seller_sku"], [sku]
            title_column = "item_name" if "item_name" in listing_columns else "title" if "title" in listing_columns else None
            if title_column:
                fields.append(title_column); values.append(str(line["item_name"] or ""))
            if "created_at" in listing_columns:
                fields.append("created_at"); values.append(now)
            if "updated_at" in listing_columns:
                fields.append("updated_at"); values.append(now)
            cursor = connection.execute(f"INSERT INTO walmart_listings ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)
            listing_id = cursor.lastrowid
        else:
            listing_id = listing[0]
        link = connection.execute("SELECT rowid FROM walmart_product_links WHERE walmart_listing_id=? LIMIT 1", (listing_id,)).fetchone()
        if link:
            connection.execute("UPDATE walmart_product_links SET product_id=?, match_status='linked' WHERE rowid=?", (int(product_id), link[0]))
        else:
            link_columns = {row[1] for row in connection.execute("PRAGMA table_info(walmart_product_links)")}
            fields = ["walmart_listing_id", "product_id", "match_status"]
            values = [listing_id, int(product_id), "linked"]
            if "matched_at" in link_columns:
                fields.append("matched_at"); values.append(now)
            connection.execute(f"INSERT INTO walmart_product_links ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)
        connection.execute("UPDATE walmart_order_lines SET product_id=? WHERE TRIM(sku)=? COLLATE NOCASE", (int(product_id), sku))
        connection.commit()
        return sku, product["product_name"]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def remove_walmart_product_mapping(order_line_id):
    ensure_order_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        line = connection.execute("SELECT sku FROM walmart_order_lines WHERE order_line_id=?", (int(order_line_id),)).fetchone()
        if line is None:
            raise ValueError("The Walmart order line was not found.")
        sku = str(line["sku"] or "").strip()
        connection.execute("UPDATE walmart_order_lines SET product_id=NULL WHERE TRIM(sku)=? COLLATE NOCASE", (sku,))
        connection.execute("""UPDATE walmart_product_links SET product_id=NULL, match_status='unlinked'
                              WHERE walmart_listing_id IN (SELECT walmart_listing_id FROM walmart_listings
                                                           WHERE TRIM(seller_sku)=? COLLATE NOCASE)""", (sku,))
        connection.commit()
        return sku
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sync_orders(days_back=3):
    ensure_order_tables()
    try:
        days_back = max(1, min(30, int(days_back)))
    except (TypeError, ValueError):
        days_back = 3
    midnight = (
        datetime.now().astimezone()
        - timedelta(days=days_back - 1)
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    data = walmart_request(
        "GET", "/v3/orders", params={"createdStartDate": midnight.isoformat(), "limit": 200}
    )
    orders = _extract_orders(data)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    now = datetime.now().astimezone().isoformat()
    try:
        for order in orders:
            po = str(order.get("purchaseOrderId") or "").strip()
            if not po:
                continue
            shipping = order.get("shippingInfo") or {}
            status_value = _order_status(order)
            order_total, currency = _order_money(order)
            status_lower = status_value.casefold()
            initial_local_status = (
                "cancelled"
                if "cancel" in status_lower
                else "shipped"
                if "ship" in status_lower
                else "acknowledged"
                if "acknowledg" in status_lower
                else "new"
            )
            connection.execute(
                """
                INSERT INTO walmart_orders (
                    purchase_order_id, customer_order_id, order_date, ship_by_date,
                    walmart_status, local_status, order_total, currency,
                    raw_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(purchase_order_id) DO UPDATE SET
                    customer_order_id=excluded.customer_order_id,
                    order_date=excluded.order_date,
                    ship_by_date=excluded.ship_by_date,
                    walmart_status=excluded.walmart_status,
                    order_total=excluded.order_total,
                    currency=excluded.currency,
                    raw_json=excluded.raw_json,
                    synced_at=excluded.synced_at
                """,
                (
                    po,
                    str(order.get("customerOrderId") or ""),
                    str(order.get("orderDate") or ""),
                    str(shipping.get("estimatedShipDate") or shipping.get("estimatedDeliveryDate") or ""),
                    status_value,
                    initial_local_status,
                    order_total,
                    currency,
                    json.dumps(order),
                    now,
                ),
            )
            for line in _extract_lines(order):
                connection.execute(
                    """
                    INSERT INTO walmart_order_lines (
                        purchase_order_id, line_number, sku, upc, item_name,
                        quantity, line_status, line_total, currency
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(purchase_order_id, line_number) DO UPDATE SET
                        sku=excluded.sku, upc=excluded.upc,
                        item_name=excluded.item_name, quantity=excluded.quantity,
                        line_status=excluded.line_status,
                        line_total=excluded.line_total, currency=excluded.currency
                    """,
                    (po, *line),
                )
        connection.commit()
        return len(orders)
    finally:
        connection.close()


def sync_today_orders():
    """Backward-compatible one-day sync used by earlier installs."""
    return sync_orders(days_back=1)


def _extract_orders(data):
    candidates = [
        data.get("list", {}).get("elements", {}).get("order") if isinstance(data, dict) else None,
        data.get("orders", {}).get("order") if isinstance(data, dict) else None,
        data.get("orders") if isinstance(data, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict):
            return [candidate]
    return []


def _extract_lines(order):
    lines = (order.get("orderLines") or {}).get("orderLine") or []
    if isinstance(lines, dict):
        lines = [lines]
    result = []
    for line in lines:
        item = line.get("item") or {}
        charge = line.get("orderLineQuantity") or {}
        statuses = (line.get("orderLineStatuses") or {}).get("orderLineStatus") or []
        if isinstance(statuses, dict):
            statuses = [statuses]
        status_value = str((statuses[0] if statuses else {}).get("status") or "")
        quantity = charge.get("amount") or 1
        try:
            quantity = max(1, int(float(quantity)))
        except (TypeError, ValueError):
            quantity = 1
        line_total, line_currency = _line_money(line)
        result.append((
            str(line.get("lineNumber") or len(result) + 1),
            str(item.get("sku") or ""),
            str(item.get("upc") or ""),
            str(item.get("productName") or item.get("itemName") or item.get("sku") or "Walmart item"),
            quantity,
            status_value,
            line_total,
            line_currency,
        ))
    return result


def _order_status(order):
    lines = _extract_lines(order)
    statuses = [line[5] for line in lines if line[5]]
    return ", ".join(sorted(set(statuses))) or "Created"


def _line_money(line):
    """Gross product charge for one Walmart order line, excluding tax/shipping."""
    total = 0.0
    currency = "USD"
    found = False
    charges = (line.get("charges") or {}).get("charge") or []
    if isinstance(charges, dict):
        charges = [charges]
    for charge in charges:
        if str(charge.get("chargeType") or "").upper() != "PRODUCT":
            continue
        amount = charge.get("chargeAmount") or {}
        try:
            total += float(amount.get("amount") or 0)
            found = True
        except (TypeError, ValueError):
            pass
        currency = str(amount.get("currency") or currency)
    return (round(total, 2) if found else None), currency


def _order_money(order):
    """Gross product + shipping charges, excluding collected tax."""
    total = 0.0
    currency = "USD"
    lines = (order.get("orderLines") or {}).get("orderLine") or []
    if isinstance(lines, dict):
        lines = [lines]
    for line in lines:
        charges = (line.get("charges") or {}).get("charge") or []
        if isinstance(charges, dict):
            charges = [charges]
        for charge in charges:
            if str(charge.get("chargeType") or "").upper() not in {
                "PRODUCT", "SHIPPING"
            }:
                continue
            amount = charge.get("chargeAmount") or {}
            try:
                total += float(amount.get("amount") or 0)
            except (TypeError, ValueError):
                pass
            currency = str(amount.get("currency") or currency)
    return round(total, 2), currency


def shipment_payload(lines, carrier, tracking_number):
    shipped_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    return {
        "orderShipment": {
            "orderLines": {
                "orderLine": [
                    {
                        "lineNumber": str(line["line_number"]),
                        "orderLineStatuses": {
                            "orderLineStatus": [
                                {
                                    "status": "Shipped",
                                    "statusQuantity": {
                                        "unitOfMeasurement": "EACH",
                                        "amount": str(line["quantity"]),
                                    },
                                    "trackingInfo": {
                                        "shipDateTime": shipped_at,
                                        "carrierName": {"carrier": carrier},
                                        "methodCode": "Standard",
                                        "trackingNumber": tracking_number,
                                    },
                                }
                            ]
                        },
                    }
                    for line in lines
                ]
            }
        }
    }


def load_order_desk():
    ensure_order_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        order_rows = connection.execute(
            """
            SELECT * FROM walmart_orders
            ORDER BY
                CASE local_status
                    WHEN 'new' THEN 1 WHEN 'acknowledged' THEN 2
                    WHEN 'pulling' THEN 3 WHEN 'pulled' THEN 4
                    WHEN 'packed' THEN 5 ELSE 6 END,
                order_date
            """
        ).fetchall()
        orders = []
        for order_row in order_rows:
            order = dict(order_row)
            lines = []
            line_rows = connection.execute(
                "SELECT * FROM walmart_order_lines WHERE purchase_order_id=? ORDER BY CAST(line_number AS INTEGER)",
                (order["purchase_order_id"],),
            ).fetchall()
            for line_row in line_rows:
                line = dict(line_row)
                mapped = connection.execute(
                    """SELECT wpl.product_id, p.product_name
                       FROM walmart_product_links wpl
                       JOIN walmart_listings wl ON wl.walmart_listing_id=wpl.walmart_listing_id
                       JOIN products p ON p.product_id=wpl.product_id
                       WHERE TRIM(wl.seller_sku)=TRIM(?) COLLATE NOCASE
                         AND lower(COALESCE(wpl.match_status,''))='linked'
                       LIMIT 1""",
                    (line.get("sku") or "",),
                ).fetchone()
                keys = [line.get("upc"), line.get("sku")]
                normalized = [str(value).strip().lstrip("0") or "0" for value in keys if str(value or "").strip()]
                product_ids = []
                if line.get("product_id"):
                    product_ids.append(int(line["product_id"]))
                if mapped:
                    product_ids.append(int(mapped["product_id"]))
                    line["mapped_product_id"] = int(mapped["product_id"])
                    line["mapped_product_name"] = mapped["product_name"]
                if normalized:
                    placeholders = ",".join("?" for _ in normalized)
                    product_ids.extend(
                        row[0] for row in connection.execute(
                            f"SELECT DISTINCT product_id FROM product_barcodes WHERE ltrim(barcode, '0') IN ({placeholders})",
                            normalized,
                        ).fetchall()
                    )
                product_ids = sorted(set(product_ids))
                matched_barcode = None
                if product_ids:
                    matched_barcode_row = connection.execute(
                        """
                        SELECT barcode FROM product_barcodes
                        WHERE product_id IN (%s)
                        ORDER BY is_primary DESC, barcode_id
                        LIMIT 1
                        """ % ",".join("?" for _ in product_ids),
                        product_ids,
                    ).fetchone()
                    matched_barcode = (
                        matched_barcode_row[0]
                        if matched_barcode_row
                        else None
                    )
                line["product_barcode"] = (
                    line.get("upc")
                    or matched_barcode
                    or line.get("sku")
                    or ""
                )
                options = []
                if product_ids:
                    placeholders = ",".join("?" for _ in product_ids)
                    options = [dict(row) for row in connection.execute(
                        f"""
                        SELECT i.inventory_id, i.product_id, i.quantity_on_hand,
                               i.quantity_reserved, i.container_id,
                               l.location_name, l.location_type, p.product_name,
                               p.average_cost
                        FROM inventory i
                        JOIN inventory_locations l ON l.location_id=i.location_id
                        JOIN products p ON p.product_id=i.product_id
                        WHERE i.product_id IN ({placeholders})
                          AND i.quantity_on_hand > 0
                        ORDER BY (i.quantity_on_hand - COALESCE(i.quantity_reserved,0)) DESC,
                                 l.location_name, i.container_id
                        """,
                        product_ids,
                    ).fetchall()]
                    for option in options:
                        option["site_name"] = physical_site_name(
                            option.get("location_name"),
                            option.get("location_type"),
                        )
                        option["available_quantity"] = max(
                            0,
                            int(option.get("quantity_on_hand") or 0)
                            - int(option.get("quantity_reserved") or 0),
                        )
                image_url = None
                if product_ids:
                    image_row = connection.execute(
                        """
                        SELECT COALESCE(NULLIF(image_url,''), image_path)
                        FROM product_images
                        WHERE product_id IN (%s)
                        ORDER BY is_primary DESC, image_id
                        LIMIT 1
                        """ % ",".join("?" for _ in product_ids),
                        product_ids,
                    ).fetchone()
                    image_url = image_row[0] if image_row else None
                line["inventory_options"] = options
                line["image_url"] = image_url
                line["allocations"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT a.*, l.location_name, i.container_id
                        FROM walmart_order_allocations a
                        JOIN inventory i ON i.inventory_id=a.inventory_id
                        JOIN inventory_locations l ON l.location_id=i.location_id
                        WHERE a.order_line_id=? AND a.pulled_quantity > 0
                        ORDER BY a.allocation_id
                        """,
                        (line["order_line_id"],),
                    ).fetchall()
                ]
                line["available_quantity"] = sum(
                    int(option.get("available_quantity") or 0)
                    for option in options
                )
                line["availability_state"] = (
                    "ready"
                    if line["available_quantity"] >= int(line.get("quantity") or 0)
                    else "partial"
                    if line["available_quantity"] > 0
                    else "missing"
                )
                saved_cost = next(
                    (
                        float(option["average_cost"])
                        for option in options
                        if option.get("average_cost") is not None
                    ),
                    None,
                )
                line["estimated_cost"] = (
                    saved_cost * int(line.get("quantity") or 0)
                    if saved_cost is not None
                    else None
                )
                lines.append(line)
            order["lines"] = lines
            order["all_pulled"] = bool(lines) and all(int(x["pulled_quantity"] or 0) >= int(x["quantity"] or 0) for x in lines)
            order["stage_picked"] = bool(order["all_pulled"]) or order.get(
                "local_status"
            ) in {"picked", "packed", "staged", "shipment_submitted", "shipped"}
            order["stage_packed"] = order.get("local_status") in {
                "packed", "staged", "shipment_submitted", "shipped"
            }
            order["stage_staged"] = order.get("local_status") in {
                "staged", "shipment_submitted", "shipped"
            }
            order["stage_shipped"] = order.get("local_status") == "shipped"
            order["item_count"] = len(lines)
            order["unit_count"] = sum(int(line.get("quantity") or 0) for line in lines)
            order["estimated_cost"] = sum(
                float(line.get("estimated_cost") or 0) for line in lines
            )
            order["estimated_profit"] = (
                float(order.get("order_total") or 0)
                - order["estimated_cost"]
            )
            order["available_unit_count"] = sum(
                min(int(line.get("quantity") or 0), int(line.get("available_quantity") or 0))
                for line in lines
            )
            order["inventory_state"] = (
                "ready"
                if lines and all(line["availability_state"] == "ready" for line in lines)
                else "partial"
                if any(line["availability_state"] != "missing" for line in lines)
                else "missing"
            )
            order["site_plan"] = build_site_plan(lines)
            order["site_names"] = list(order["site_plan"].keys())
            order["single_site_candidates"] = single_site_candidates(lines)
            order["recommended_site"] = (
                order["single_site_candidates"][0]
                if order["single_site_candidates"]
                else None
            )
            order["split_location"] = (
                order["inventory_state"] == "ready"
                and not order["single_site_candidates"]
            )
            order["order_datetime"] = parse_walmart_datetime(order.get("order_date"))
            order["ship_by_datetime"] = parse_walmart_datetime(order.get("ship_by_date"))
            order["order_date_display"] = format_walmart_datetime(order.get("order_date"))
            order["ship_by_display"] = format_walmart_datetime(order.get("ship_by_date"))
            order["order_age"] = format_order_age(order["order_datetime"])
            orders.append(order)
        orders.sort(
            key=lambda item: item.get("order_datetime")
            or datetime.max.astimezone()
        )
        for sequence, order in enumerate(orders, start=1):
            order["fifo_sequence"] = sequence
        return orders
    finally:
        connection.close()


def physical_site_name(location_name, location_type=None):
    name = str(location_name or "").strip().casefold()
    location_type = str(location_type or "").strip().casefold()
    if name in {"brookshouse storefront", "store back room"}:
        return "Storefront"
    if name in {"trailer 1", "trailer 2", "trailer 3", "storage container"}:
        return "Storage Yard"
    if name == "warehouse" or location_type == "warehouse":
        return "Warehouse"
    if name == "on-the-road trailer" or location_type == "mobile_inventory":
        return "Mobile"
    if location_type in {"hold", "reserved"}:
        return "Hold / Review"
    return "Other"


def build_site_plan(lines):
    plan = {}
    for line in lines:
        for option in line.get("inventory_options") or []:
            available = int(option.get("available_quantity") or 0)
            if available <= 0:
                continue
            site = option.get("site_name") or "Other"
            site_data = plan.setdefault(site, {"available_units": 0, "locations": {}})
            site_data["available_units"] += available
            location_label = option.get("location_name") or "Unknown location"
            container = option.get("container_id") or "Loose inventory"
            key = f"{location_label} / {container}"
            site_data["locations"][key] = site_data["locations"].get(key, 0) + available
    return plan


def single_site_candidates(lines):
    if not lines:
        return []
    candidates = None
    site_totals = {}
    for line in lines:
        required = int(line.get("quantity") or 0)
        line_sites = {}
        for option in line.get("inventory_options") or []:
            site = option.get("site_name") or "Other"
            available = int(option.get("available_quantity") or 0)
            line_sites[site] = line_sites.get(site, 0) + available
            site_totals[site] = site_totals.get(site, 0) + available
        capable = {site for site, available in line_sites.items() if available >= required}
        candidates = capable if candidates is None else candidates & capable
    priority = {"Storefront": 0, "Storage Yard": 1, "Warehouse": 2, "Mobile": 3, "Other": 4, "Hold / Review": 9}
    return sorted(
        candidates or set(),
        key=lambda site: (priority.get(site, 8), -site_totals.get(site, 0), site),
    )


def parse_walmart_datetime(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            number = int(text)
            if number > 10_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number).astimezone()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def format_walmart_datetime(value):
    parsed = parse_walmart_datetime(value)
    return parsed.strftime("%a %m/%d/%Y %I:%M %p") if parsed else str(value or "—")


def format_order_age(parsed):
    if parsed is None:
        return "Unknown age"
    seconds = max(0, int((datetime.now().astimezone() - parsed).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h old"
    return f"{hours}h {minutes}m old"
