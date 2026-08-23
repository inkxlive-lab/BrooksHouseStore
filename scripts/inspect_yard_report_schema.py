"""Read-only schema/status inspection for building the yard report."""

import sqlite3


def main() -> int:
    connection = sqlite3.connect("file:app/data/brookshouse_store.db?mode=ro", uri=True)
    tables = [
        "walmart_orders", "walmart_order_lines", "amazon_order_history",
        "amazon_order_item_history", "products", "inventory", "inventory_locations",
        "amazon_listings", "amazon_product_links", "walmart_listings",
        "walmart_product_links", "marketplace_sync_runs", "operations_work_queue",
    ]
    existing = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables:
        if table not in existing:
            continue
        columns = [r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')]
        print(f"TABLE {table}: {', '.join(columns)}")
    for label, sql in [
        ("WALMART ORDER STATUS", "SELECT walmart_status, COUNT(*) FROM walmart_orders GROUP BY walmart_status ORDER BY 2 DESC"),
        ("WALMART LINE STATUS", "SELECT line_status, COUNT(*), SUM(quantity) FROM walmart_order_lines GROUP BY line_status ORDER BY 2 DESC"),
        ("AMAZON STATUS/FULFILLER", "SELECT fulfillment_status, fulfilled_by, COUNT(*), SUM(unit_count) FROM amazon_order_history GROUP BY fulfillment_status, fulfilled_by ORDER BY 3 DESC"),
        ("SYNC RUNS", "SELECT * FROM marketplace_sync_runs ORDER BY rowid DESC LIMIT 4"),
    ]:
        print(label)
        try:
            for row in connection.execute(sql):
                print(tuple(row))
        except sqlite3.Error as error:
            print(f"ERROR {error}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
