import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "app" / "data" / "brookshouse_store.db"


def _age_label(seconds):
    if seconds is None:
        return "Unknown"
    hours = max(0, int(seconds // 3600))
    if hours < 1:
        return "Less than 1 hour ago"
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)=lower(?)",
        (name,),
    ).fetchone() is not None


def _columns(connection, table):
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _scalar(connection, sql, parameters=()):
    row = connection.execute(sql, parameters).fetchone()
    return int(row[0] or 0) if row else 0


def _check(key, title, status, summary, detail, action_url=None, action_label=None, count=None):
    return {
        "key": key, "title": title, "status": status, "summary": summary,
        "detail": detail, "action_url": action_url, "action_label": action_label,
        "count": count,
    }


def build_system_check():
    checks = []
    generated_at = datetime.now().astimezone()
    if not DB_PATH.exists():
        checks.append(_check("database", "Database", "red", "Database file is missing",
                             str(DB_PATH), "/dashboard", "Return to Dashboard"))
        return _finish(checks, generated_at)

    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        checks.append(_check("database", "Database", "green" if integrity == "ok" else "red",
                             "Connected and passed quick check" if integrity == "ok" else "Integrity check failed",
                             f"{DB_PATH.name} · {DB_PATH.stat().st_size / 1048576:.1f} MB"))

        backup_files = []
        for folder in (PROJECT_ROOT / "backups", PROJECT_ROOT):
            if folder.exists():
                backup_files.extend(path for path in folder.rglob("*.db") if path.resolve() != DB_PATH.resolve())
                backup_files.extend(folder.glob("*.zip"))
        newest = max(backup_files, key=lambda path: path.stat().st_mtime, default=None)
        backup_age = generated_at.timestamp() - newest.stat().st_mtime if newest else None
        backup_status = "green" if backup_age is not None and backup_age <= 86400 else "yellow" if newest else "red"
        checks.append(_check("backup", "Database backup", backup_status,
                             f"Newest backup: {_age_label(backup_age)}" if newest else "No database backup found",
                             newest.name if newest else "Create a recoverable database backup before major changes."))

        negative = _scalar(connection, "SELECT COUNT(*) FROM inventory WHERE COALESCE(quantity_on_hand,0) < 0")
        over_reserved = _scalar(connection, "SELECT COUNT(*) FROM inventory WHERE COALESCE(quantity_reserved,0) > COALESCE(quantity_on_hand,0)")
        checks.append(_check("quantities", "Inventory quantities", "red" if negative else "yellow" if over_reserved else "green",
                             f"{negative} negative · {over_reserved} over-reserved",
                             "Negative or over-reserved rows require review." if negative or over_reserved else "No impossible inventory quantities found.",
                             "/inventory/search?run_report=1", "Review Inventory", negative + over_reserved))

        missing_container = _scalar(connection, """SELECT COUNT(*) FROM inventory i JOIN inventory_locations l ON l.location_id=i.location_id
            WHERE COALESCE(i.quantity_on_hand,0)>0 AND lower(COALESCE(l.location_type,'')) IN ('trailer','storage','warehouse','mobile_storage')
              AND trim(COALESCE(i.container_id,''))=''""")
        checks.append(_check("containers", "Storage placement", "yellow" if missing_container else "green",
                             f"{missing_container} stocked rows missing a container ID",
                             "Trailer, storage, and warehouse inventory should have a tote, pallet, rack, or container." if missing_container else "Storage inventory has container placement.",
                             "/inventory/search?stock_status=in_stock&run_report=1", "Find Inventory", missing_container))

        no_barcode = _scalar(connection, """SELECT COUNT(*) FROM products p WHERE COALESCE(p.active,1)=1
            AND NOT EXISTS (SELECT 1 FROM product_barcodes pb WHERE pb.product_id=p.product_id)""")
        no_image = _scalar(connection, """SELECT COUNT(*) FROM products p WHERE COALESCE(p.active,1)=1
            AND NOT EXISTS (SELECT 1 FROM product_images pi WHERE pi.product_id=p.product_id AND (trim(COALESCE(pi.image_url,''))<>'' OR trim(COALESCE(pi.image_path,''))<>''))""")
        no_price = _scalar(connection, "SELECT COUNT(*) FROM products WHERE COALESCE(active,1)=1 AND store_price IS NULL")
        product_issues = no_barcode + no_image + no_price
        checks.append(_check("products", "Product completeness", "yellow" if product_issues else "green",
                             f"{no_barcode} no barcode · {no_image} no image · {no_price} no store price",
                             "Open Products to complete records used by labels and marketplaces." if product_issues else "Active product records are complete.",
                             "/products", "Open Products", product_issues))

        review_count = 0
        if _table_exists(connection, "inventory_locations"):
            review_count = _scalar(connection, """SELECT COALESCE(SUM(i.quantity_on_hand),0) FROM inventory i
                JOIN inventory_locations l ON l.location_id=i.location_id
                WHERE lower(l.location_name) LIKE '%prob%' OR lower(l.location_type) IN ('hold','review')""")
        checks.append(_check("review", "Exception inventory", "yellow" if review_count else "green",
                             f"{review_count} units in hold/review locations",
                             "Resolve uncertain scans and inventory exceptions." if review_count else "No units are waiting in exception locations.",
                             "/inventory/search?location_id=12&run_report=1", "Open Review Inventory", review_count))

        for channel, env_names, table, mapping_table, mapping_status in (
            ("Walmart", ("WALMART_CLIENT_ID", "WALMART_CLIENT_SECRET"), "walmart_orders", "walmart_product_links", "linked"),
            ("Amazon", ("SP_API_REFRESH_TOKEN", "LWA_APP_ID", "LWA_CLIENT_SECRET"), "amazon_order_history", "amazon_product_links", "linked"),
            ("Shopify", ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ADMIN_ACCESS_TOKEN"), "shopify_orders", None, None),
        ):
            configured = all(str(os.environ.get(name, "")).strip() for name in env_names)
            table_candidates = ("shopify_orders", "shopify_order_history", "sales_orders") if channel == "Shopify" else (table,)
            saved = 0
            for candidate in table_candidates:
                if _table_exists(connection, candidate):
                    saved = max(saved, _scalar(connection, f'SELECT COUNT(*) FROM "{candidate}"'))
            status = "green" if configured and saved else "yellow" if configured or saved else "red"
            detail = f"Credentials {'configured' if configured else 'not detected'} · {saved} saved order record{'s' if saved != 1 else ''}."
            checks.append(_check(channel.lower(), f"{channel} connection", status,
                                 "Configured with saved data" if status == "green" else "Needs configuration or a successful sync", detail,
                                 "/channels/orders", "Marketplace Orders"))

        unmatched_walmart = 0
        if _table_exists(connection, "walmart_order_lines"):
            unmatched_walmart = _scalar(connection, "SELECT COUNT(*) FROM walmart_order_lines WHERE product_id IS NULL")
        unmatched_amazon = 0
        if _table_exists(connection, "amazon_product_links"):
            unmatched_amazon = _scalar(connection, "SELECT COUNT(*) FROM amazon_product_links WHERE lower(COALESCE(match_status,'')) <> 'linked' OR product_id IS NULL")
        unmatched = unmatched_walmart + unmatched_amazon
        checks.append(_check("mapping", "Marketplace product mapping", "yellow" if unmatched else "green",
                             f"{unmatched_walmart} Walmart · {unmatched_amazon} Amazon unmatched",
                             "Unmatched lines are excluded from reliable inventory and profit comparisons." if unmatched else "Saved marketplace products are linked.",
                             "/reports/product-matching", "Fix Product Matches", unmatched))

        stuck_reserved = 0
        if _table_exists(connection, "walmart_order_inventory_sync"):
            sync_columns = _columns(connection, "walmart_order_inventory_sync")
            if "quantity_added" in sync_columns:
                stuck_reserved = _scalar(connection, "SELECT COALESCE(SUM(quantity_added),0) FROM walmart_order_inventory_sync WHERE COALESCE(quantity_added,0)>0")
        checks.append(_check("reserved", "Online Orders / Reserved", "yellow" if stuck_reserved else "green",
                             f"{stuck_reserved} Walmart units currently tracked as reserved",
                             "Review old reservations against marketplace order status." if stuck_reserved else "No Walmart reservations need review.",
                             "/channels/walmart/orders", "Review Walmart Orders", stuck_reserved))

        checks.append(_check("receive", "Receive Inventory", "green",
                             "Barcode packaging helper installed",
                             "Unit scans default to 1; mapped cases and packs use quantity-per-scan.",
                             "/inventory/receive", "Test Receive Screen"))
    except Exception as error:
        checks.append(_check("system_error", "System-check query", "red", "A health query failed", str(error)))
    finally:
        connection.close()
    return _finish(checks, generated_at)


def _finish(checks, generated_at):
    counts = {color: sum(check["status"] == color for check in checks) for color in ("green", "yellow", "red")}
    overall = "red" if counts["red"] else "yellow" if counts["yellow"] else "green"
    return {"checks": checks, "counts": counts, "overall": overall,
            "generated_at": generated_at.strftime("%m/%d/%Y %I:%M:%S %p")}
