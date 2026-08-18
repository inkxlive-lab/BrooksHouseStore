#!/usr/bin/env python
from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
ORDERS_URL = "https://marketplace.walmartapis.com/v3/orders"
TARGET_LOCATION_ID = 5
TARGET_LOCATION_NAME = "Online Orders / Reserved"
OPEN_STATUSES = {"Created", "Acknowledged"}
CLOSED_STATUSES = {"Shipped", "Delivered", "Cancelled"}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


class WalmartOrdersClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self.access_token = None
        self.expires_at = utc_now()

    def base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "WM_SVC.NAME": "Walmart Marketplace",
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        }

    def authenticate(self) -> None:
        encoded = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        response = self.session.post(
            TOKEN_URL,
            headers={
                **self.base_headers(),
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        lifetime = int(payload.get("expires_in") or 900)
        self.expires_at = utc_now() + timedelta(seconds=max(60, lifetime - 60))

    def ensure_token(self) -> None:
        if not self.access_token or utc_now() >= self.expires_at:
            self.authenticate()

    def get_orders(self, created_start_date: str) -> dict[str, Any]:
        self.ensure_token()
        params = {
            "createdStartDate": created_start_date,
            "limit": 200,
            "productInfo": "false",
            "shipNodeType": "SellerFulfilled",
            "replacementInfo": "false",
            "incentiveInfo": "false",
        }

        response = self.session.get(
            ORDERS_URL,
            params=params,
            headers={
                **self.base_headers(),
                "WM_SEC.ACCESS_TOKEN": str(self.access_token),
            },
            timeout=45,
        )

        if response.status_code == 401:
            self.access_token = None
            self.ensure_token()
            response = self.session.get(
                ORDERS_URL,
                params=params,
                headers={
                    **self.base_headers(),
                    "WM_SEC.ACCESS_TOKEN": str(self.access_token),
                },
                timeout=45,
            )

        response.raise_for_status()
        return response.json()

    def get_order(self, purchase_order_id: str) -> dict[str, Any]:
        """Refresh one tracked PO, including reservations older than --days."""
        self.ensure_token()
        response = self.session.get(
            f"{ORDERS_URL}/{purchase_order_id}",
            params={"productInfo": "false"},
            headers={
                **self.base_headers(),
                "WM_SEC.ACCESS_TOKEN": str(self.access_token),
            },
            timeout=45,
        )
        if response.status_code == 401:
            self.access_token = None
            self.ensure_token()
            response = self.session.get(
                f"{ORDERS_URL}/{purchase_order_id}",
                params={"productInfo": "false"},
                headers={
                    **self.base_headers(),
                    "WM_SEC.ACCESS_TOKEN": str(self.access_token),
                },
                timeout=45,
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload.get("order"), dict):
            return payload["order"]
        return payload


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def extract_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = []

    if isinstance(payload.get("list"), dict):
        elements = payload["list"].get("elements")
        if isinstance(elements, dict):
            found.extend(as_list(elements.get("order")))

    if isinstance(payload.get("orders"), dict):
        found.extend(as_list(payload["orders"].get("order")))

    found.extend(as_list(payload.get("order")))
    return [row for row in found if isinstance(row, dict)]


def current_line_status(line: dict[str, Any]) -> str:
    wrapper = line.get("orderLineStatuses")
    if not isinstance(wrapper, dict):
        return ""

    statuses = [
        str(row.get("status") or "").strip()
        for row in as_list(wrapper.get("orderLineStatus"))
        if isinstance(row, dict)
    ]

    # Walmart can return more than one status entry. Terminal states must win
    # over earlier Created/Acknowledged history.
    for status in ("Cancelled", "Delivered", "Shipped", "Acknowledged", "Created"):
        if status in statuses:
            return status

    return statuses[-1] if statuses else ""


def line_quantity(line: dict[str, Any]) -> int:
    quantity = line.get("orderLineQuantity")
    if not isinstance(quantity, dict):
        return 0

    try:
        return max(int(float(quantity.get("amount"))), 0)
    except (TypeError, ValueError):
        return 0


def create_sync_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS walmart_order_inventory_sync (
            sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_order_id TEXT NOT NULL,
            line_number TEXT NOT NULL,
            seller_sku TEXT,
            product_id INTEGER,
            quantity_added INTEGER NOT NULL DEFAULT 0,
            target_location_id INTEGER NOT NULL,
            walmart_status TEXT,
            processed_at TEXT NOT NULL,
            UNIQUE (purchase_order_id, line_number)
        )
        """
    )
    existing_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(walmart_order_inventory_sync)"
        ).fetchall()
    }
    if "updated_at" not in existing_columns:
        conn.execute(
            "ALTER TABLE walmart_order_inventory_sync ADD COLUMN updated_at TEXT"
        )
    if "closed_at" not in existing_columns:
        conn.execute(
            "ALTER TABLE walmart_order_inventory_sync ADD COLUMN closed_at TEXT"
        )
    conn.commit()


def validate_target_location(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT location_name
        FROM inventory_locations
        WHERE location_id = ?
        """,
        (TARGET_LOCATION_ID,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Location 5 does not exist.")

    actual = str(row["location_name"] or "").strip()
    if actual.casefold() != TARGET_LOCATION_NAME.casefold():
        raise RuntimeError(
            f"Location 5 is named {actual!r}, not {TARGET_LOCATION_NAME!r}. "
            "Nothing was changed."
        )


def find_product(conn: sqlite3.Connection, seller_sku: str):
    sku = (seller_sku or "").strip()
    if not sku:
        return None

    try:
        row = conn.execute(
            """
            SELECT
                wpl.product_id,
                p.product_name
            FROM walmart_product_links AS wpl
            JOIN walmart_listings AS wl
              ON wl.walmart_listing_id = wpl.walmart_listing_id
            JOIN products AS p
              ON p.product_id = wpl.product_id
            WHERE TRIM(wl.seller_sku) = ?
              AND LOWER(COALESCE(wpl.match_status, '')) = 'linked'
            LIMIT 1
            """,
            (sku,),
        ).fetchone()
        if row is not None:
            return row
    except sqlite3.OperationalError:
        pass

    digits = "".join(ch for ch in sku if ch.isdigit())
    if not digits:
        return None

    lookup = digits.lstrip("0") or "0"

    return conn.execute(
        """
        SELECT
            p.product_id,
            p.product_name
        FROM product_barcodes AS pb
        JOIN products AS p
          ON p.product_id = pb.product_id
        WHERE TRIM(CAST(pb.barcode AS TEXT)) = ?
           OR LTRIM(TRIM(CAST(pb.barcode AS TEXT)), '0') = ?
        ORDER BY pb.is_primary DESC, pb.barcode_id
        LIMIT 1
        """,
        (digits, lookup),
    ).fetchone()


def get_sync_record(
    conn: sqlite3.Connection,
    po: str,
    line_number: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM walmart_order_inventory_sync
        WHERE purchase_order_id = ?
          AND line_number = ?
        LIMIT 1
        """,
        (po, line_number),
    ).fetchone()


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_name.replace("_", "").isalnum():
        return set()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if exists is None:
        return set()
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def update_local_order_status(
    conn: sqlite3.Connection,
    *,
    po: str,
    line_number: str,
    status: str,
) -> int:
    """Update known BrooksHouse Walmart order tables when they exist."""
    updates = 0
    candidates = (
        ("walmart_order_lines", ("purchase_order_id", "purchase_order_number"),
         ("line_number", "order_line_number"), ("status", "line_status", "walmart_status")),
    )
    for table_name, po_names, line_names, status_names in candidates:
        columns = table_columns(conn, table_name)
        if not columns:
            continue
        po_column = next((name for name in po_names if name in columns), None)
        status_column = next((name for name in status_names if name in columns), None)
        if po_column is None or status_column is None:
            continue
        line_column = next((name for name in line_names if name in columns), None)
        sql = f"UPDATE {table_name} SET {status_column} = ? WHERE {po_column} = ?"
        params: list[Any] = [status, po]
        if line_column is not None:
            sql += f" AND CAST({line_column} AS TEXT) = ?"
            params.append(line_number)
        cursor = conn.execute(sql, params)
        updates += max(cursor.rowcount, 0)
    return updates


def aggregate_order_status(statuses: list[str]) -> str:
    recognized = [status for status in statuses if status]
    if not recognized:
        return ""
    if any(status in OPEN_STATUSES for status in recognized):
        return "Acknowledged" if "Acknowledged" in recognized else "Created"
    if all(status == "Cancelled" for status in recognized):
        return "Cancelled"
    if all(status in {"Delivered", "Cancelled"} for status in recognized):
        return "Delivered"
    if all(status in CLOSED_STATUSES for status in recognized):
        return "Shipped"
    return recognized[-1]


def update_local_order_header_status(
    conn: sqlite3.Connection,
    *,
    po: str,
    status: str,
) -> int:
    columns = table_columns(conn, "walmart_orders")
    if not columns or not status:
        return 0
    po_column = next(
        (name for name in ("purchase_order_id", "purchase_order_number") if name in columns),
        None,
    )
    status_column = next(
        (name for name in ("status", "order_status", "walmart_status") if name in columns),
        None,
    )
    if po_column is None or status_column is None:
        return 0
    cursor = conn.execute(
        f"UPDATE walmart_orders SET {status_column} = ? WHERE {po_column} = ?",
        (status, po),
    )
    return max(cursor.rowcount, 0)


def inventory_record(
    conn: sqlite3.Connection,
    product_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT inventory_id, quantity_on_hand
        FROM inventory
        WHERE product_id = ?
          AND location_id = ?
          AND COALESCE(container_id, '') = ''
        ORDER BY inventory_id
        LIMIT 1
        """,
        (product_id, TARGET_LOCATION_ID),
    ).fetchone()


def apply_inventory_delta(
    conn: sqlite3.Connection,
    *,
    po: str,
    line_number: str,
    sku: str,
    product_id: int,
    product_name: str,
    delta: int,
    previous_reserved: int,
    desired_reserved: int,
    status: str,
) -> tuple[int, int]:
    row = inventory_record(conn, product_id)

    previous = int(row["quantity_on_hand"] or 0) if row else 0
    new_quantity = previous + delta
    if new_quantity < 0:
        raise RuntimeError(
            f"Cannot release {-delta} unit(s) for PO {po} line {line_number}; "
            f"Location 5 only contains {previous} unit(s) of product {product_id}."
        )
    timestamp = now_text()

    if row:
        conn.execute(
            """
            UPDATE inventory
            SET quantity_on_hand = ?,
                updated_at = ?
            WHERE inventory_id = ?
            """,
            (new_quantity, timestamp, row["inventory_id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO inventory (
                product_id,
                location_id,
                container_id,
                quantity_on_hand,
                quantity_reserved,
                reorder_level,
                updated_at
            )
            VALUES (?, ?, '', ?, 0, 0, ?)
            """,
            (product_id, TARGET_LOCATION_ID, new_quantity, timestamp),
        )

    reference = f"WALMART-{po}-L{line_number}"[:100]
    action = "added" if delta > 0 else "released"
    notes = (
        f"Walmart order reservation {action}. "
        f"PO: {po}. Line: {line_number}. SKU: {sku}. "
        f"Status: {status}. Reservation: {previous_reserved} -> "
        f"{desired_reserved}. Quantity change: {delta:+d}. "
        f"Previous location quantity: {previous}. "
        f"New location quantity: {new_quantity}."
    )

    conn.execute(
        """
        INSERT INTO inventory_transactions (
            product_id,
            location_id,
            container_id,
            transaction_type,
            quantity_change,
            unit_cost,
            reference_number,
            notes,
            created_at
        )
        VALUES (?, ?, '', ?, ?, NULL, ?, ?, ?)
        """,
        (
            product_id,
            TARGET_LOCATION_ID,
            "walmart_order_reserve" if delta > 0 else "walmart_order_release",
            delta,
            reference,
            notes,
            timestamp,
        ),
    )

    return previous, new_quantity


def save_sync_record(
    conn: sqlite3.Connection,
    *,
    existing: sqlite3.Row | None,
    po: str,
    line_number: str,
    sku: str,
    product_id: int,
    desired_reserved: int,
    status: str,
) -> None:
    timestamp = now_text()
    closed_at = timestamp if status in CLOSED_STATUSES else None
    if existing is None:
        conn.execute(
            """
            INSERT INTO walmart_order_inventory_sync (
                purchase_order_id, line_number, seller_sku, product_id,
                quantity_added, target_location_id, walmart_status,
                processed_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                po, line_number, sku, product_id, desired_reserved,
                TARGET_LOCATION_ID, status, timestamp, timestamp, closed_at,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE walmart_order_inventory_sync
            SET seller_sku = ?, product_id = ?, quantity_added = ?,
                walmart_status = ?, updated_at = ?, closed_at = ?
            WHERE purchase_order_id = ? AND line_number = ?
            """,
            (
                sku, product_id, desired_reserved, status, timestamp,
                closed_at, po, line_number,
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="app/data/brookshouse_store.db")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.days <= 180:
        raise RuntimeError("--days must be between 1 and 180.")

    load_env(Path(args.env))

    client_id = os.getenv("WALMART_CLIENT_ID")
    client_secret = os.getenv("WALMART_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError(
            "WALMART_CLIENT_ID and WALMART_CLIENT_SECRET are required in .env."
        )

    conn = sqlite3.connect(args.database, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    create_sync_table(conn)
    validate_target_location(conn)

    start_date = (
        utc_now() - timedelta(days=args.days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = WalmartOrdersClient(client_id, client_secret)
    payload = client.get_orders(start_date)
    orders = extract_orders(payload)

    # The date window discovers new orders. Refresh every older PO that still
    # has reserved units so a later shipment/cancellation cannot stay stale.
    returned_pos = {
        str(order.get("purchaseOrderId") or "").strip()
        for order in orders
    }
    active_sync_pos = {
        str(row["purchase_order_id"] or "").strip()
        for row in conn.execute(
            """
            SELECT DISTINCT purchase_order_id
            FROM walmart_order_inventory_sync
            WHERE quantity_added > 0
            """
        ).fetchall()
    }
    refreshed_older = 0
    for tracked_po in sorted(active_sync_pos - returned_pos):
        if not tracked_po:
            continue
        try:
            tracked_order = client.get_order(tracked_po)
        except requests.HTTPError as error:
            print(f"WARNING could not refresh tracked PO {tracked_po}: {error}")
            continue
        if isinstance(tracked_order, dict):
            orders.append(tracked_order)
            refreshed_older += 1

    print(f"Target: Location 5 - {TARGET_LOCATION_NAME}")
    print("Mode:", "APPLY" if args.apply else "PREVIEW")
    print(f"Orders returned: {len(orders)}")
    print(f"Older active POs refreshed: {refreshed_older}")
    print()

    open_lines = matched = unchanged = status_updates = 0
    additions = adjustments = releases = reopenings = 0
    units_added = units_released = 0
    unmatched = []

    for order in orders:
        po = str(order.get("purchaseOrderId") or "").strip()
        wrapper = order.get("orderLines")
        lines = (
            as_list(wrapper.get("orderLine"))
            if isinstance(wrapper, dict)
            else []
        )
        order_line_statuses: list[str] = []

        for line in lines:
            if not isinstance(line, dict):
                continue

            status = current_line_status(line)
            if status:
                order_line_statuses.append(status)
            quantity = line_quantity(line)
            if status not in OPEN_STATUSES | CLOSED_STATUSES:
                continue
            line_number = str(line.get("lineNumber") or "").strip()
            item = line.get("item") if isinstance(line.get("item"), dict) else {}
            sku = str(item.get("sku") or "").strip()
            walmart_name = str(item.get("productName") or "").strip()

            if not po or not line_number:
                unmatched.append((po, line_number, sku, walmart_name, "missing PO/line"))
                continue

            existing = get_sync_record(conn, po, line_number)
            desired_reserved = quantity if status in OPEN_STATUSES else 0
            previous_reserved = (
                int(existing["quantity_added"] or 0)
                if existing is not None
                else 0
            )

            if status in OPEN_STATUSES:
                open_lines += 1
                if quantity <= 0:
                    unmatched.append(
                        (po, line_number, sku, walmart_name, "open line has zero quantity")
                    )
                    continue

            product = None
            if existing is not None and existing["product_id"] is not None:
                product = conn.execute(
                    "SELECT product_id, product_name FROM products WHERE product_id = ?",
                    (int(existing["product_id"]),),
                ).fetchone()
            if product is None and desired_reserved > 0:
                product = find_product(conn, sku)

            # A closed line that was never reserved needs only a local status
            # update; it must not create a new inventory sync record.
            if product is None and desired_reserved == 0 and existing is None:
                if args.apply:
                    status_updates += update_local_order_status(
                        conn,
                        po=po,
                        line_number=line_number,
                        status=status,
                    )
                    conn.commit()
                continue

            if product is None:
                unmatched.append((po, line_number, sku, walmart_name, "no BrooksHouse match"))
                print(f"UNMATCHED | PO {po} | line {line_number} | SKU {sku} | {walmart_name}")
                continue

            matched += 1
            delta = desired_reserved - previous_reserved
            old_status = str(existing["walmart_status"] or "") if existing else ""

            if delta > 0:
                units_added += delta
                if existing is not None and previous_reserved == 0:
                    reopenings += 1
                    action_label = "REOPEN"
                elif existing is None:
                    additions += 1
                    action_label = "ADD"
                else:
                    adjustments += 1
                    action_label = "INCREASE"
            elif delta < 0:
                units_released += -delta
                if desired_reserved == 0:
                    releases += 1
                    action_label = "CLOSE/RELEASE"
                else:
                    adjustments += 1
                    action_label = "DECREASE"
            else:
                unchanged += 1
                action_label = "STATUS" if old_status != status else "UNCHANGED"

            if not args.apply:
                print(
                    f"PREVIEW {action_label} {delta:+d} | "
                    f"{product['product_name']} | product {product['product_id']} | "
                    f"reserved {previous_reserved} -> {desired_reserved} | "
                    f"status {old_status or 'new'} -> {status} | "
                    f"PO {po} | line {line_number}"
                )
                continue

            previous = new_quantity = None
            if delta != 0:
                previous, new_quantity = apply_inventory_delta(
                    conn,
                    po=po,
                    line_number=line_number,
                    sku=sku,
                    product_id=int(product["product_id"]),
                    product_name=str(product["product_name"] or ""),
                    delta=delta,
                    previous_reserved=previous_reserved,
                    desired_reserved=desired_reserved,
                    status=status,
                )
            save_sync_record(
                conn,
                existing=existing,
                po=po,
                line_number=line_number,
                sku=sku,
                product_id=int(product["product_id"]),
                desired_reserved=desired_reserved,
                status=status,
            )
            status_updates += update_local_order_status(
                conn,
                po=po,
                line_number=line_number,
                status=status,
            )
            conn.commit()

            location_text = (
                f" | Location 5: {previous} -> {new_quantity}"
                if previous is not None
                else ""
            )
            print(
                f"{action_label} {delta:+d} | {product['product_name']} | "
                f"reserved {previous_reserved} -> {desired_reserved}"
                f"{location_text} | status {status} | PO {po} | line {line_number}"
            )

        if args.apply and po and order_line_statuses:
            status_updates += update_local_order_header_status(
                conn,
                po=po,
                status=aggregate_order_status(order_line_statuses),
            )
            conn.commit()

    print()
    print("Summary")
    print("-------")
    print(f"Open order lines:  {open_lines}")
    print(f"Matched lines:     {matched}")
    print(f"New reservations:  {additions}")
    print(f"Adjusted lines:     {adjustments}")
    print(f"Closed/released:    {releases}")
    print(f"Reopened lines:     {reopenings}")
    print(f"Unchanged lines:    {unchanged}")
    print(f"Units to add:       {units_added}")
    print(f"Units to release:   {units_released}")
    if args.apply:
        print(f"Local status rows:  {status_updates}")
    print(f"Unmatched lines:    {len(unmatched)}")

    if not args.apply:
        print()
        print("PREVIEW ONLY - no inventory quantities were changed.")
        print("If additions, adjustments, and releases look right, rerun with --apply.")

    if unmatched:
        print()
        print("Unmatched lines")
        for po, line_number, sku, name, reason in unmatched:
            print(
                f"PO {po} | line {line_number} | SKU {sku} | "
                f"{name} | {reason}"
            )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
