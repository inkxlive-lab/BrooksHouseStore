"""Copy-only transactional cross-channel inventory engine.

Every mutating entry point refuses the known production database path.  The
schema and engine are intentionally not installed by application startup.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.channel_inventory_mapping import validate_mapping


PRODUCTION_DB = (Path(__file__).resolve().parents[1] / "data" / "brookshouse_store.db").resolve()
STOREFRONT = "BrooksHouse Storefront"
BACK_ROOM = "Store Back Room"
RESERVED = "Online Orders / Reserved"
ALLOCATION_POLICIES = {"single_location_only", "ordered_multi_location"}
DEFAULT_ELIGIBLE_LOCATIONS = (STOREFRONT, BACK_ROOM)
RESERVE_TYPES = {"warehouse", "trailer", "container", "mobile_inventory", "mobile_storage", "storage"}

COPY_SCHEMA = """
CREATE TABLE channel_inventory_ledger (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name TEXT NOT NULL,
    order_id TEXT NOT NULL,
    order_line_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    product_id INTEGER,
    ordered_quantity INTEGER NOT NULL DEFAULT 0,
    quantity_change INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    source_version TEXT,
    allocation_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    CHECK(channel_name IN ('shopify','amazon','walmart')),
    CHECK(ordered_quantity>=0), CHECK(length(trim(event_type))>0),
    UNIQUE(channel_name, order_id, order_line_id, event_type),
    FOREIGN KEY(product_id) REFERENCES products(product_id),
    FOREIGN KEY(allocation_id) REFERENCES channel_inventory_allocations(allocation_id)
);
CREATE TABLE channel_inventory_allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name TEXT NOT NULL,
    order_id TEXT NOT NULL,
    order_line_id TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    ordered_quantity INTEGER NOT NULL,
    deducted_quantity INTEGER NOT NULL DEFAULT 0,
    staged_quantity INTEGER NOT NULL DEFAULT 0,
    unlocated_quantity INTEGER NOT NULL DEFAULT 0,
    restored_quantity INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    source_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(channel_name IN ('shopify','amazon','walmart')),
    CHECK(ordered_quantity>=0 AND deducted_quantity>=0 AND staged_quantity>=0 AND unlocated_quantity>=0 AND restored_quantity>=0),
    CHECK(status IN ('deducted','replenishment_needed','unlocated','staged','cancelled')),
    UNIQUE(channel_name, order_id, order_line_id),
    FOREIGN KEY(product_id) REFERENCES products(product_id)
);
CREATE TABLE channel_inventory_allocation_inventory (
    allocation_inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id INTEGER NOT NULL,
    inventory_id INTEGER NOT NULL,
    deducted_quantity INTEGER NOT NULL DEFAULT 0,
    staged_quantity INTEGER NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    CHECK(deducted_quantity>=0 AND staged_quantity>=0 AND reserved_quantity>=0),
    UNIQUE(allocation_id, inventory_id),
    FOREIGN KEY(allocation_id) REFERENCES channel_inventory_allocations(allocation_id),
    FOREIGN KEY(inventory_id) REFERENCES inventory(inventory_id)
);
CREATE TABLE channel_inventory_event_transactions (
    event_id INTEGER NOT NULL,
    inventory_transaction_id INTEGER NOT NULL UNIQUE,
    inventory_id INTEGER NOT NULL,
    quantity_change INTEGER NOT NULL,
    PRIMARY KEY(event_id, inventory_transaction_id),
    FOREIGN KEY(event_id) REFERENCES channel_inventory_ledger(event_id),
    FOREIGN KEY(inventory_transaction_id) REFERENCES inventory_transactions(transaction_id),
    FOREIGN KEY(inventory_id) REFERENCES inventory(inventory_id)
);
"""


class ProductionWriteRefused(RuntimeError):
    pass


class StalePreview(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceLine:
    channel: str
    order_id: str
    order_line_id: str
    product_id: int | None
    quantity: int
    sku: str
    asin: str
    title: str
    status: str
    source_version: str
    mapping_status: str


@dataclass(frozen=True)
class EnginePlan:
    channel: str
    order_id: str
    order_line_id: str
    product_id: int | None
    quantity: int
    source_version: str
    action: str
    location_name: str
    inventory_deductions: tuple[tuple[int, int], ...]
    replenishment_candidates: tuple[dict, ...]
    reason: str
    allocation_policy: str = "single_location_only"
    eligible_locations: tuple[str, ...] = DEFAULT_ELIGIBLE_LOCATIONS

    def as_dict(self) -> dict:
        result = asdict(self)
        result["inventory_deductions"] = [list(item) for item in self.inventory_deductions]
        result["replenishment_candidates"] = list(self.replenishment_candidates)
        return result


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _event_suffix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def assert_copy_database(database: str | Path) -> Path:
    resolved = Path(database).resolve()
    if resolved == PRODUCTION_DB or resolved.parent == PRODUCTION_DB.parent:
        raise ProductionWriteRefused(f"Copy-only engine refuses database path: {resolved}")
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def connect_copy(database: str | Path) -> sqlite3.Connection:
    path = assert_copy_database(database)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def initialize_copy_schema(database: str | Path) -> None:
    connection = connect_copy(database)
    try:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('channel_inventory_ledger','channel_inventory_allocations','channel_inventory_allocation_inventory','channel_inventory_event_transactions')"
        ).fetchall()
        if existing:
            raise RuntimeError("Copy ledger/allocation schema already exists; refusing implicit replacement")
        connection.executescript(COPY_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def _require_copy_schema(connection: sqlite3.Connection) -> None:
    required = {"channel_inventory_ledger", "channel_inventory_allocations", "channel_inventory_allocation_inventory", "channel_inventory_event_transactions"}
    found = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)", tuple(required)
    )}
    if found != required:
        raise RuntimeError("Copy ledger/allocation schema is not initialized")


def load_source_line(connection: sqlite3.Connection, channel: str, order_id: str, order_line_id: str) -> SourceLine:
    channel = channel.casefold()
    if channel == "shopify":
        row = connection.execute(
            """SELECT l.product_id,l.quantity,l.current_quantity,l.sku,'' asin,l.title,l.match_status,
                      o.cancelled_at,o.fulfillment_status,
                      COALESCE(l.updated_at,'') || ':' || COALESCE(o.cancelled_at,'') || ':' ||
                      COALESCE(o.fulfillment_status,'') source_version
                 FROM shopify_sales_lines l JOIN shopify_sales_orders o ON o.shopify_order_id=l.shopify_order_id
                WHERE l.shopify_order_id=? AND l.shopify_line_id=?""", (order_id, order_line_id)
        ).fetchone()
        if row is None:
            raise ValueError("Shopify source line not found")
        quantity = max(int(row["current_quantity"] if row["current_quantity"] is not None else row["quantity"] or 0), 0)
        status = "cancelled" if row["cancelled_at"] else _text(row["fulfillment_status"])
        mapping_status = _text(row["match_status"])
    elif channel == "amazon":
        row = connection.execute(
            """SELECT i.product_id,i.quantity_ordered quantity,i.seller_sku sku,i.asin,i.title,
                      o.fulfillment_status status,i.synced_at source_version
                 FROM amazon_order_item_history i JOIN amazon_order_history o ON o.amazon_order_id=i.amazon_order_id
                WHERE i.amazon_order_id=? AND i.order_item_id=?""", (order_id, order_line_id)
        ).fetchone()
        if row is None:
            raise ValueError("Amazon source line not found")
        quantity = max(int(row["quantity"] or 0), 0)
        status = _text(row["status"])
        mapping_status = "matched" if row["product_id"] is not None else "unmatched"
    elif channel == "walmart":
        row = connection.execute(
            """SELECT l.product_id,l.quantity,l.sku,'' asin,l.item_name title,
                      COALESCE(NULLIF(l.line_status,''),o.walmart_status) status,o.synced_at source_version
                 FROM walmart_order_lines l JOIN walmart_orders o ON o.purchase_order_id=l.purchase_order_id
                WHERE l.purchase_order_id=? AND CAST(l.order_line_id AS TEXT)=?""", (order_id, str(order_line_id))
        ).fetchone()
        if row is None:
            raise ValueError("Walmart source line not found")
        quantity = max(int(row["quantity"] or 0), 0)
        status = _text(row["status"])
        mapping_status = "matched" if row["product_id"] is not None else "unmatched"
    else:
        raise ValueError(f"Unsupported channel: {channel}")
    return SourceLine(
        channel=channel, order_id=str(order_id), order_line_id=str(order_line_id),
        product_id=int(row["product_id"]) if row["product_id"] is not None else None,
        quantity=quantity, sku=_text(row["sku"]), asin=_text(row["asin"]), title=_text(row["title"]),
        status=status, source_version=_text(row["source_version"]), mapping_status=mapping_status,
    )


def list_source_lines(connection: sqlite3.Connection, cutoff: str) -> list[tuple[str, str, str]]:
    keys = []
    keys.extend(("shopify", row[0], row[1]) for row in connection.execute(
        """SELECT l.shopify_order_id,l.shopify_line_id FROM shopify_sales_lines l
             JOIN shopify_sales_orders o ON o.shopify_order_id=l.shopify_order_id
            WHERE o.test_order=0 AND o.processed_at>=?""", (cutoff,)
    ))
    keys.extend(("amazon", row[0], row[1]) for row in connection.execute(
        """SELECT i.amazon_order_id,i.order_item_id FROM amazon_order_item_history i
             JOIN amazon_order_history o ON o.amazon_order_id=i.amazon_order_id WHERE o.created_time>=?""", (cutoff,)
    ))
    keys.extend(("walmart", row[0], str(row[1])) for row in connection.execute(
        """SELECT l.purchase_order_id,l.order_line_id FROM walmart_order_lines l
             JOIN walmart_orders o ON o.purchase_order_id=l.purchase_order_id
            WHERE (CASE WHEN trim(COALESCE(o.order_date,'')) GLOB '[0-9]*' AND length(trim(COALESCE(o.order_date,'')))>=13
                        THEN datetime(CAST(o.order_date AS INTEGER)/1000,'unixepoch') ELSE datetime(o.order_date) END)>=datetime(?)""", (cutoff,)
    ))
    return keys


def _location_rows(connection: sqlite3.Connection, product_id: int, name: str) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT i.inventory_id,i.location_id,i.container_id,i.quantity_on_hand,i.quantity_reserved
             FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
            WHERE i.product_id=? AND l.active=1 AND lower(l.location_name)=lower(?)
            ORDER BY (i.quantity_on_hand-COALESCE(i.quantity_reserved,0)) DESC,i.inventory_id""", (product_id, name)
    ).fetchall()


def _deduction_plan(rows: list[sqlite3.Row], quantity: int) -> tuple[tuple[int, int], ...]:
    available = sum(max(int(row["quantity_on_hand"] or 0) - int(row["quantity_reserved"] or 0), 0) for row in rows)
    if available < quantity:
        return ()
    remaining = quantity
    result = []
    for row in rows:
        take = min(remaining, max(int(row["quantity_on_hand"] or 0) - int(row["quantity_reserved"] or 0), 0))
        if take:
            result.append((int(row["inventory_id"]), take))
            remaining -= take
        if remaining == 0:
            break
    return tuple(result)


def _policy_deduction_plan(connection: sqlite3.Connection, product_id: int, quantity: int,
                           allocation_policy: str, eligible_locations: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    if allocation_policy not in ALLOCATION_POLICIES:
        raise ValueError(f"Unsupported allocation policy: {allocation_policy}")
    if not eligible_locations:
        raise ValueError("At least one eligible location is required")
    unapproved = [name for name in eligible_locations if name not in DEFAULT_ELIGIBLE_LOCATIONS]
    if unapproved:
        raise ValueError(f"Unapproved marketplace fulfillment locations: {', '.join(unapproved)}")
    if allocation_policy == "single_location_only":
        for location in eligible_locations:
            plan = _deduction_plan(_location_rows(connection, product_id, location), quantity)
            if plan:
                return plan
        return ()
    remaining = quantity
    selected = []
    for location in eligible_locations:
        for inventory_id, available in _deduction_plan_partial(_location_rows(connection, product_id, location), remaining):
            selected.append((inventory_id, available)); remaining -= available
        if remaining == 0:
            return tuple(selected)
    return ()


def _deduction_plan_partial(rows: list[sqlite3.Row], quantity: int) -> tuple[tuple[int, int], ...]:
    remaining, result = quantity, []
    for row in rows:
        take = min(remaining, max(int(row["quantity_on_hand"] or 0) - int(row["quantity_reserved"] or 0), 0))
        if take:
            result.append((int(row["inventory_id"]), take)); remaining -= take
        if remaining == 0:
            break
    return tuple(result)


def _replenishment_candidates(connection: sqlite3.Connection, product_id: int) -> tuple[dict, ...]:
    rows = connection.execute(
        """SELECT i.inventory_id,l.location_id,l.location_name,l.location_type,i.container_id,
                  i.quantity_on_hand,i.quantity_reserved
             FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
            WHERE i.product_id=? AND l.active=1 ORDER BY l.location_name,i.inventory_id""", (product_id,)
    ).fetchall()
    candidates = []
    for row in rows:
        name = _text(row["location_name"])
        kind = _text(row["location_type"]).casefold()
        if name.casefold() in {STOREFRONT.casefold(), BACK_ROOM.casefold(), RESERVED.casefold()}:
            continue
        if kind not in RESERVE_TYPES:
            continue
        available = max(int(row["quantity_on_hand"] or 0) - int(row["quantity_reserved"] or 0), 0)
        if available:
            candidates.append({
                "inventory_id": int(row["inventory_id"]), "location_id": int(row["location_id"]),
                "location_name": name, "container_id": _text(row["container_id"]),
                "quantity_available": available,
            })
    return tuple(candidates)


def preview_line(connection: sqlite3.Connection, channel: str, order_id: str, order_line_id: str,
                 allocation_policy: str = "single_location_only",
                 eligible_locations: tuple[str, ...] = DEFAULT_ELIGIBLE_LOCATIONS) -> EnginePlan:
    source = load_source_line(connection, channel, order_id, order_line_id)
    if connection.execute(
        "SELECT 1 FROM channel_inventory_ledger WHERE channel_name=? AND order_id=? AND order_line_id=? AND event_type='sale_commitment'",
        (source.channel, source.order_id, source.order_line_id),
    ).fetchone():
        return EnginePlan(source.channel, source.order_id, source.order_line_id, source.product_id, source.quantity,
                          source.source_version, "already_applied", "", (), (), "sale commitment already exists")
    mapping = validate_mapping(connection, source.channel, source.product_id, source.sku, source.asin, source.mapping_status)
    if not mapping.safe:
        return EnginePlan(source.channel, source.order_id, source.order_line_id, None, source.quantity,
                          source.source_version, "review", "", (), (), f"unsafe mapping ({mapping.status}): {mapping.reason}")
    if "cancel" in source.status.casefold():
        return EnginePlan(source.channel, source.order_id, source.order_line_id, source.product_id, source.quantity,
                          source.source_version, "review", "", (), (), "source line is cancelled")
    if source.quantity <= 0:
        return EnginePlan(source.channel, source.order_id, source.order_line_id, source.product_id, source.quantity,
                          source.source_version, "review", "", (), (), "source quantity is zero")
    deductions = _policy_deduction_plan(connection, source.product_id, source.quantity,
                                        allocation_policy, eligible_locations)
    if deductions:
        used_ids = {row_id for row_id, _ in deductions}
        used_locations = [location for location in eligible_locations if any(
            int(row["inventory_id"]) in used_ids for row in _location_rows(connection, source.product_id, location))]
        return EnginePlan(source.channel, source.order_id, source.order_line_id, source.product_id, source.quantity,
                          source.source_version, "deduct", " -> ".join(used_locations), deductions, (), "",
                          allocation_policy, eligible_locations)
    candidates = _replenishment_candidates(connection, source.product_id)
    return EnginePlan(source.channel, source.order_id, source.order_line_id, source.product_id, source.quantity,
                      source.source_version, "owe", "", (), candidates,
                      "eligible inventory unavailable; replenishment required" if candidates else "inventory unlocated company-wide",
                      allocation_policy, eligible_locations)


def _verify_preview(current: EnginePlan, expected: EnginePlan | None) -> None:
    if expected is None:
        return
    fields = ("channel", "order_id", "order_line_id", "product_id", "quantity", "source_version", "action", "location_name", "inventory_deductions")
    changed = [field for field in fields if getattr(current, field) != getattr(expected, field)]
    if changed:
        raise StalePreview(f"Preview is stale; changed fields: {', '.join(changed)}")


def _allocation(connection: sqlite3.Connection, source: SourceLine) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM channel_inventory_allocations WHERE channel_name=? AND order_id=? AND order_line_id=?",
        (source.channel, source.order_id, source.order_line_id),
    ).fetchone()


def _insert_event(connection: sqlite3.Connection, source: SourceLine, event_type: str, outcome: str,
                  quantity_change: int, allocation_id: int | None, metadata: dict | None = None) -> int:
    cursor = connection.execute(
        """INSERT INTO channel_inventory_ledger(channel_name,order_id,order_line_id,event_type,
               product_id,ordered_quantity,quantity_change,outcome,source_version,allocation_id,metadata_json,created_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source.channel, source.order_id, source.order_line_id, event_type, source.product_id,
         source.quantity, quantity_change, outcome, source.source_version, allocation_id,
         json.dumps(metadata or {}, separators=(",", ":")), _now()),
    )
    return int(cursor.lastrowid)


def _inventory_transaction(connection: sqlite3.Connection, event_id: int, inventory_id: int,
                           quantity_change: int, transaction_type: str, source: SourceLine) -> int:
    inventory = connection.execute(
        "SELECT product_id,location_id,container_id FROM inventory WHERE inventory_id=?", (inventory_id,)
    ).fetchone()
    if inventory is None or int(inventory["product_id"]) != int(source.product_id or 0):
        raise RuntimeError("Inventory row no longer belongs to the mapped product")
    cursor = connection.execute(
        """INSERT INTO inventory_transactions(product_id,location_id,container_id,transaction_type,
               quantity_change,unit_cost,reference_number,notes,created_at)
             VALUES(?,?,?,?,?,NULL,?,?,?)""",
        (source.product_id, inventory["location_id"], _text(inventory["container_id"]), transaction_type,
         quantity_change, f"{source.channel.upper()}-{source.order_id}-{source.order_line_id}"[:100],
         f"Copy-only channel engine event {event_id}; {source.channel} order {source.order_id} line {source.order_line_id}.", _now()),
    )
    transaction_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO channel_inventory_event_transactions VALUES(?,?,?,?)",
        (event_id, transaction_id, inventory_id, quantity_change),
    )
    return transaction_id


def _work_item(connection: sqlite3.Connection, source: SourceLine, candidates: tuple[dict, ...], quantity: int) -> None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations_work_queue'").fetchone() is None:
        return
    candidate = candidates[0] if candidates else {}
    destination = connection.execute(
        "SELECT location_id FROM inventory_locations WHERE lower(location_name)=lower(?)", (STOREFRONT,)
    ).fetchone()
    product = connection.execute("SELECT product_name FROM products WHERE product_id=?", (source.product_id,)).fetchone()
    barcode = connection.execute(
        "SELECT barcode FROM product_barcodes WHERE product_id=? ORDER BY is_primary DESC,barcode_id LIMIT 1", (source.product_id,)
    ).fetchone()
    task_type = "directed_replenishment" if candidates else "online_order_inventory_investigation"
    details = {
        "channel": source.channel, "order_id": source.order_id, "order_line_id": source.order_line_id,
        "product_id": source.product_id, "product_name": _text(product[0] if product else ""),
        "barcode": _text(barcode[0] if barcode else ""), "quantity_needed": quantity,
        "candidate": candidate, "suggested_destination": STOREFRONT,
        "reason": "online order cannot be fulfilled from Storefront or Store Back Room",
    }
    now = _now()
    connection.execute(
        """INSERT INTO operations_work_queue(task_key,task_type,title,details,priority,status,
               source_channel,source_reference,product_id,location_id,requested_quantity,created_at,updated_at,
               source_location_id,source_container_id,destination_location_id,destination_container_id)
             VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(task_key) DO UPDATE SET details=excluded.details,requested_quantity=excluded.requested_quantity,
               updated_at=excluded.updated_at,status=CASE WHEN operations_work_queue.status IN ('completed','cancelled')
                 THEN operations_work_queue.status ELSE 'open' END""",
        (f"channel-replenishment:{source.channel}:{source.order_id}:{source.order_line_id}", task_type,
         f"Online order inventory needed: {_text(product[0] if product else source.product_id)}",
         json.dumps(details, separators=(",", ":")), "high", source.channel,
         f"{source.order_id}|{source.order_line_id}", source.product_id,
         candidate.get("location_id"), quantity, now, now, candidate.get("location_id"),
         candidate.get("container_id", ""), int(destination[0]) if destination else None, ""),
    )


def apply_sale_to_copy(database: str | Path, channel: str, order_id: str, order_line_id: str,
                       expected_preview: EnginePlan | None = None,
                       allocation_policy: str = "single_location_only",
                       eligible_locations: tuple[str, ...] = DEFAULT_ELIGIBLE_LOCATIONS) -> dict:
    connection = connect_copy(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_copy_schema(connection)
        current = preview_line(connection, channel, order_id, order_line_id, allocation_policy, eligible_locations)
        if current.action == "already_applied":
            source = load_source_line(connection, channel, order_id, order_line_id)
            allocation = _allocation(connection, source)
            if allocation is not None and allocation["status"] == "cancelled" and "cancel" not in source.status.casefold():
                connection.rollback()
                connection.close()
                return apply_quantity_change_to_copy(database, channel, order_id, order_line_id)
            connection.rollback()
            return {"status": "already_applied", "plan": current.as_dict()}
        _verify_preview(current, expected_preview)
        if current.action == "review":
            connection.rollback()
            return {"status": "review", "plan": current.as_dict()}
        source = load_source_line(connection, channel, order_id, order_line_id)
        now = _now()
        status = "deducted" if current.action == "deduct" else ("replenishment_needed" if current.replenishment_candidates else "unlocated")
        cursor = connection.execute(
            """INSERT INTO channel_inventory_allocations(channel_name,order_id,order_line_id,product_id,
                   ordered_quantity,deducted_quantity,staged_quantity,unlocated_quantity,restored_quantity,status,
                   source_version,created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,0,?,?,?,?)""",
            (source.channel, source.order_id, source.order_line_id, source.product_id, source.quantity,
             source.quantity if current.action == "owe" else 0, status, source.source_version, now, now),
        )
        allocation_id = int(cursor.lastrowid)
        event_id = _insert_event(connection, source, "sale_commitment", status,
                                 -source.quantity if current.action == "deduct" else 0, allocation_id,
                                 {"location": current.location_name, "allocation_policy": current.allocation_policy,
                                  "eligible_locations": current.eligible_locations,
                                  "replenishment_candidates": current.replenishment_candidates})
        if current.action == "deduct":
            for inventory_id, quantity in current.inventory_deductions:
                updated = connection.execute(
                    """UPDATE inventory SET quantity_on_hand=quantity_on_hand-?,updated_at=?
                        WHERE inventory_id=? AND quantity_on_hand-COALESCE(quantity_reserved,0)>=?""",
                    (quantity, now, inventory_id, quantity),
                )
                if updated.rowcount != 1:
                    raise StalePreview("Eligible inventory changed during apply")
                connection.execute(
                    "INSERT INTO channel_inventory_allocation_inventory(allocation_id,inventory_id,deducted_quantity) VALUES(?,?,?)",
                    (allocation_id, inventory_id, quantity),
                )
                _inventory_transaction(connection, event_id, inventory_id, -quantity, "channel_order_deduction", source)
            connection.execute(
                "UPDATE channel_inventory_allocations SET deducted_quantity=?,unlocated_quantity=0 WHERE allocation_id=?",
                (source.quantity, allocation_id),
            )
        else:
            _work_item(connection, source, current.replenishment_candidates, source.quantity)
        connection.commit()
        return {"status": status, "event_id": event_id, "allocation_id": allocation_id, "plan": current.as_dict()}
    except sqlite3.IntegrityError as error:
        connection.rollback()
        if "UNIQUE constraint failed: channel_inventory_ledger" in str(error):
            return {"status": "already_applied"}
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _allocation_source(connection: sqlite3.Connection, channel: str, order_id: str, order_line_id: str) -> tuple[SourceLine, sqlite3.Row]:
    source = load_source_line(connection, channel, order_id, order_line_id)
    allocation = _allocation(connection, source)
    if allocation is None:
        raise RuntimeError("No allocation exists for this order line")
    if int(allocation["product_id"]) != int(source.product_id or 0):
        raise StalePreview("Product mapping changed after allocation")
    return source, allocation


def _restore_deducted(connection: sqlite3.Connection, source: SourceLine, allocation: sqlite3.Row,
                      event_id: int, quantity: int, transaction_type: str) -> int:
    remaining = quantity
    restored = 0
    rows = connection.execute(
        "SELECT * FROM channel_inventory_allocation_inventory WHERE allocation_id=? AND deducted_quantity>0 ORDER BY allocation_inventory_id DESC",
        (allocation["allocation_id"],),
    ).fetchall()
    for row in rows:
        amount = min(remaining, int(row["deducted_quantity"]))
        if not amount:
            continue
        connection.execute("UPDATE inventory SET quantity_on_hand=quantity_on_hand+?,updated_at=? WHERE inventory_id=?",
                           (amount, _now(), row["inventory_id"]))
        connection.execute(
            "UPDATE channel_inventory_allocation_inventory SET deducted_quantity=deducted_quantity-? WHERE allocation_inventory_id=?",
            (amount, row["allocation_inventory_id"]),
        )
        _inventory_transaction(connection, event_id, int(row["inventory_id"]), amount, transaction_type, source)
        remaining -= amount
        restored += amount
        if remaining == 0:
            break
    if remaining:
        raise RuntimeError("Allocation does not contain enough deducted inventory to restore")
    return restored


def apply_quantity_change_to_copy(database: str | Path, channel: str, order_id: str, order_line_id: str) -> dict:
    connection = connect_copy(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_copy_schema(connection)
        source, allocation = _allocation_source(connection, channel, order_id, order_line_id)
        old_quantity = int(allocation["ordered_quantity"])
        event_type = f"quantity_change:{_event_suffix(source.source_version + ':' + str(source.quantity))}"
        existing = connection.execute(
            "SELECT event_id FROM channel_inventory_ledger WHERE channel_name=? AND order_id=? AND order_line_id=? AND event_type=?",
            (source.channel, source.order_id, source.order_line_id, event_type),
        ).fetchone()
        if existing:
            connection.rollback()
            return {"status": "already_applied", "event_id": int(existing[0])}
        if source.quantity == old_quantity:
            connection.rollback()
            return {"status": "unchanged"}
        event_id = _insert_event(connection, source, event_type, "quantity_adjusted", 0,
                                 int(allocation["allocation_id"]), {"old_quantity": old_quantity})
        deducted = int(allocation["deducted_quantity"])
        restored = 0
        additionally_deducted = 0
        if source.quantity < deducted:
            restored = _restore_deducted(connection, source, allocation, event_id, deducted-source.quantity,
                                          "channel_order_quantity_decrease_restore")
            deducted -= restored
        needed = max(source.quantity-deducted-int(allocation["staged_quantity"]), 0)
        if needed:
            for location in (STOREFRONT, BACK_ROOM):
                additions = _deduction_plan(_location_rows(connection, int(source.product_id), location), needed)
                if not additions:
                    continue
                for inventory_id, quantity in additions:
                    updated = connection.execute(
                        """UPDATE inventory SET quantity_on_hand=quantity_on_hand-?,updated_at=?
                            WHERE inventory_id=? AND quantity_on_hand-COALESCE(quantity_reserved,0)>=?""",
                        (quantity, _now(), inventory_id, quantity),
                    )
                    if updated.rowcount != 1:
                        raise StalePreview("Eligible inventory changed during quantity increase")
                    connection.execute(
                        """INSERT INTO channel_inventory_allocation_inventory(allocation_id,inventory_id,deducted_quantity)
                             VALUES(?,?,?) ON CONFLICT(allocation_id,inventory_id) DO UPDATE SET
                             deducted_quantity=deducted_quantity+excluded.deducted_quantity""",
                        (allocation["allocation_id"], inventory_id, quantity),
                    )
                    _inventory_transaction(connection, event_id, inventory_id, -quantity,
                                           "channel_order_quantity_increase", source)
                    additionally_deducted += quantity
                deducted += additionally_deducted
                break
        unlocated = max(source.quantity-deducted-int(allocation["staged_quantity"]), 0)
        status = "deducted" if deducted == source.quantity else ("replenishment_needed" if _replenishment_candidates(connection, int(source.product_id)) else "unlocated")
        connection.execute("UPDATE channel_inventory_ledger SET quantity_change=? WHERE event_id=?",
                           (restored-additionally_deducted, event_id))
        connection.execute(
            """UPDATE channel_inventory_allocations SET ordered_quantity=?,deducted_quantity=?,
                   unlocated_quantity=?,restored_quantity=restored_quantity+?,status=?,source_version=?,updated_at=?
                 WHERE allocation_id=?""",
            (source.quantity, deducted, unlocated, restored, status, source.source_version, _now(), allocation["allocation_id"]),
        )
        if unlocated:
            _work_item(connection, source, _replenishment_candidates(connection, int(source.product_id)), unlocated)
        connection.commit()
        return {"status": status, "event_id": event_id, "old_quantity": old_quantity,
                "new_quantity": source.quantity, "restored_quantity": restored, "unlocated_quantity": unlocated}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cancel_before_fulfillment_to_copy(database: str | Path, channel: str, order_id: str, order_line_id: str) -> dict:
    connection = connect_copy(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_copy_schema(connection)
        source, allocation = _allocation_source(connection, channel, order_id, order_line_id)
        if "cancel" not in source.status.casefold():
            raise StalePreview("Source line is not cancelled")
        event_type = f"cancellation:{_event_suffix(source.source_version)}"
        existing = connection.execute(
            "SELECT event_id FROM channel_inventory_ledger WHERE channel_name=? AND order_id=? AND order_line_id=? AND event_type=?",
            (source.channel, source.order_id, source.order_line_id, event_type),
        ).fetchone()
        if existing:
            connection.rollback()
            return {"status": "already_applied", "event_id": int(existing[0])}
        event_id = _insert_event(connection, source, event_type, "cancelled", 0,
                                 int(allocation["allocation_id"]), {"prior_status": allocation["status"]})
        deducted = int(allocation["deducted_quantity"])
        restored = _restore_deducted(connection, source, allocation, event_id, deducted,
                                     "channel_order_cancellation_restore") if deducted else 0
        connection.execute("UPDATE channel_inventory_ledger SET quantity_change=? WHERE event_id=?",
                           (restored, event_id))
        for row in connection.execute(
            "SELECT * FROM channel_inventory_allocation_inventory WHERE allocation_id=? AND reserved_quantity>0",
            (allocation["allocation_id"],),
        ).fetchall():
            connection.execute(
                "UPDATE inventory SET quantity_reserved=quantity_reserved-?,updated_at=? WHERE inventory_id=? AND quantity_reserved>=?",
                (row["reserved_quantity"], _now(), row["inventory_id"], row["reserved_quantity"]),
            )
            connection.execute(
                "UPDATE channel_inventory_allocation_inventory SET reserved_quantity=0,staged_quantity=0 WHERE allocation_inventory_id=?",
                (row["allocation_inventory_id"],),
            )
        connection.execute(
            """UPDATE channel_inventory_allocations SET ordered_quantity=0,deducted_quantity=0,
                   staged_quantity=0,unlocated_quantity=0,restored_quantity=restored_quantity+?,
                   status='cancelled',source_version=?,updated_at=? WHERE allocation_id=?""",
            (restored, source.source_version, _now(), allocation["allocation_id"]),
        )
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations_work_queue'").fetchone():
            connection.execute(
                "UPDATE operations_work_queue SET status='cancelled',updated_at=? WHERE task_key=? AND status NOT IN ('completed','cancelled')",
                (_now(), f"channel-replenishment:{source.channel}:{source.order_id}:{source.order_line_id}"),
            )
        connection.commit()
        return {"status": "cancelled", "event_id": event_id, "restored_quantity": restored}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_refund_notice_to_copy(database: str | Path, channel: str, order_id: str, order_line_id: str,
                                 refund_reference: str) -> dict:
    connection = connect_copy(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_copy_schema(connection)
        source, allocation = _allocation_source(connection, channel, order_id, order_line_id)
        event_type = f"refund_notice:{_event_suffix(refund_reference)}"
        try:
            event_id = _insert_event(connection, source, event_type, "review_no_restock", 0,
                                     int(allocation["allocation_id"]), {"refund_reference": refund_reference})
        except sqlite3.IntegrityError:
            connection.rollback()
            return {"status": "already_applied"}
        connection.commit()
        return {"status": "review_no_restock", "event_id": event_id, "inventory_change": 0}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def confirm_staged_inventory_to_copy(database: str | Path, channel: str, order_id: str,
                                     order_line_id: str, inventory_id: int, quantity: int,
                                     staging_reference: str) -> dict:
    """Tie already-physical Reserved stock to one allocation; never create on-hand."""
    if quantity <= 0 or not staging_reference.strip():
        raise ValueError("Confirmed staging requires positive quantity and a reference")
    connection = connect_copy(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_copy_schema(connection)
        source, allocation = _allocation_source(connection, channel, order_id, order_line_id)
        event_type = f"stage:{_event_suffix(staging_reference)}"
        if connection.execute(
            "SELECT 1 FROM channel_inventory_ledger WHERE channel_name=? AND order_id=? AND order_line_id=? AND event_type=?",
            (source.channel, source.order_id, source.order_line_id, event_type),
        ).fetchone():
            connection.rollback()
            return {"status": "already_applied"}
        inventory = connection.execute(
            """SELECT i.product_id,i.quantity_on_hand,i.quantity_reserved,l.location_name
                 FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
                WHERE i.inventory_id=?""", (inventory_id,)
        ).fetchone()
        if inventory is None or int(inventory["product_id"]) != int(source.product_id or 0):
            raise StalePreview("Staged inventory does not belong to the mapped product")
        if _text(inventory["location_name"]).casefold() != RESERVED.casefold():
            raise StalePreview("Confirmed staged inventory must already be physically in Online Orders / Reserved")
        available = int(inventory["quantity_on_hand"] or 0) - int(inventory["quantity_reserved"] or 0)
        if available < quantity or int(allocation["unlocated_quantity"]) < quantity:
            raise StalePreview("Staged physical quantity or allocation owed quantity is insufficient")
        event_id = _insert_event(connection, source, event_type, "staged_and_reserved", 0,
                                 int(allocation["allocation_id"]),
                                 {"staging_reference": staging_reference, "inventory_id": inventory_id, "quantity": quantity})
        connection.execute(
            "UPDATE inventory SET quantity_reserved=quantity_reserved+?,updated_at=? WHERE inventory_id=?",
            (quantity, _now(), inventory_id),
        )
        connection.execute(
            """INSERT INTO channel_inventory_allocation_inventory(allocation_id,inventory_id,staged_quantity,reserved_quantity)
                 VALUES(?,?,?,?) ON CONFLICT(allocation_id,inventory_id) DO UPDATE SET
                 staged_quantity=staged_quantity+excluded.staged_quantity,
                 reserved_quantity=reserved_quantity+excluded.reserved_quantity""",
            (allocation["allocation_id"], inventory_id, quantity, quantity),
        )
        connection.execute(
            """UPDATE channel_inventory_allocations SET staged_quantity=staged_quantity+?,
                   unlocated_quantity=unlocated_quantity-?,status=CASE WHEN unlocated_quantity-?=0
                   THEN 'staged' ELSE status END,updated_at=? WHERE allocation_id=?""",
            (quantity, quantity, quantity, _now(), allocation["allocation_id"]),
        )
        connection.commit()
        return {"status": "staged_and_reserved", "event_id": event_id, "quantity": quantity}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def confirm_restock_to_copy(database: str | Path, channel: str, order_id: str, order_line_id: str,
                            inventory_id: int, quantity: int, restock_reference: str) -> dict:
    if quantity <= 0 or not restock_reference.strip():
        raise ValueError("Confirmed restock requires positive quantity and a reference")
    connection = connect_copy(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_copy_schema(connection)
        source, allocation = _allocation_source(connection, channel, order_id, order_line_id)
        event_type = f"restock:{_event_suffix(restock_reference)}"
        if connection.execute(
            "SELECT 1 FROM channel_inventory_ledger WHERE channel_name=? AND order_id=? AND order_line_id=? AND event_type=?",
            (source.channel, source.order_id, source.order_line_id, event_type),
        ).fetchone():
            connection.rollback()
            return {"status": "already_applied"}
        totals = connection.execute(
            """SELECT COALESCE(SUM(CASE WHEN cet.quantity_change<0 THEN -cet.quantity_change ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN cet.quantity_change>0 THEN cet.quantity_change ELSE 0 END),0)
                 FROM channel_inventory_ledger l
                 JOIN channel_inventory_event_transactions cet ON cet.event_id=l.event_id
                WHERE l.allocation_id=?""", (allocation["allocation_id"],)).fetchone()
        returnable = max(int(totals[0])-int(totals[1]), 0)
        applied_quantity = min(quantity, returnable)
        outcome = "physically_restocked" if applied_quantity == quantity else (
            "partially_restocked_capped" if applied_quantity else "over_restock_blocked")
        event_id = _insert_event(connection, source, event_type, outcome, applied_quantity,
                                 int(allocation["allocation_id"]), {"restock_reference": restock_reference,
                                 "requested_quantity": quantity, "returnable_before": returnable,
                                 "applied_quantity": applied_quantity})
        if applied_quantity == 0:
            connection.commit()
            return {"status": "over_restock_blocked", "event_id": event_id, "requested_quantity": quantity,
                    "quantity": 0, "returnable_quantity": 0}
        inventory = connection.execute("SELECT product_id FROM inventory WHERE inventory_id=?", (inventory_id,)).fetchone()
        if inventory is None or int(inventory[0]) != int(source.product_id or 0):
            raise StalePreview("Restock destination does not belong to mapped product")
        connection.execute("UPDATE inventory SET quantity_on_hand=quantity_on_hand+?,updated_at=? WHERE inventory_id=?",
                           (applied_quantity, _now(), inventory_id))
        _inventory_transaction(connection, event_id, inventory_id, applied_quantity, "channel_return_restock", source)
        connection.execute(
            "UPDATE channel_inventory_allocations SET restored_quantity=restored_quantity+?,updated_at=? WHERE allocation_id=?",
            (applied_quantity, _now(), allocation["allocation_id"]),
        )
        connection.commit()
        return {"status": outcome, "event_id": event_id, "requested_quantity": quantity,
                "quantity": applied_quantity, "returnable_quantity": returnable-applied_quantity}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
