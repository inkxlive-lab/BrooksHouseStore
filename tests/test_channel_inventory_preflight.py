import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.migrations.channel_inventory_engine_schema import apply_to_copy, preview
from app.services.channel_inventory_engine import PRODUCTION_DB, ProductionWriteRefused
from app.services.channel_inventory_preflight import build_report
from app.services.channel_inventory_controls import effective_control, set_copy_control


SCHEMA = """
CREATE TABLE products(product_id INTEGER PRIMARY KEY);
CREATE TABLE inventory_locations(location_id INTEGER PRIMARY KEY,location_name TEXT,location_type TEXT,active INTEGER);
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,location_id INTEGER,container_id TEXT,quantity_on_hand INTEGER,quantity_reserved INTEGER,reorder_level INTEGER,updated_at TEXT);
CREATE TABLE inventory_transactions(transaction_id INTEGER PRIMARY KEY);
CREATE TABLE shopify_sales_orders(shopify_order_id TEXT PRIMARY KEY,processed_at TEXT,updated_at TEXT,last_imported_at TEXT,cancelled_at TEXT,financial_status TEXT,fulfillment_status TEXT,test_order INTEGER);
CREATE TABLE shopify_sales_lines(shopify_line_id TEXT PRIMARY KEY,shopify_order_id TEXT,product_id INTEGER,sku TEXT,quantity INTEGER,current_quantity INTEGER,inventory_applied INTEGER,match_status TEXT,updated_at TEXT);
CREATE TABLE amazon_order_history(amazon_order_id TEXT PRIMARY KEY,created_time TEXT,last_updated_time TEXT,fulfillment_status TEXT,fulfilled_by TEXT,synced_at TEXT);
CREATE TABLE amazon_order_item_history(amazon_order_id TEXT,order_item_id TEXT,product_id INTEGER,seller_sku TEXT,quantity_ordered INTEGER,synced_at TEXT);
CREATE TABLE walmart_orders(purchase_order_id TEXT PRIMARY KEY,order_date TEXT,walmart_status TEXT,synced_at TEXT);
CREATE TABLE walmart_order_lines(order_line_id INTEGER PRIMARY KEY,purchase_order_id TEXT,line_number TEXT,product_id INTEGER,sku TEXT,quantity INTEGER,line_status TEXT);
CREATE TABLE amazon_order_inventory_sync(amazon_order_id TEXT,order_item_id TEXT,quantity_added INTEGER);
CREATE TABLE walmart_order_inventory_sync(purchase_order_id TEXT,line_number TEXT,quantity_added INTEGER);
"""


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "copy.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(SCHEMA)
            connection.execute("INSERT INTO products VALUES(10)")
            connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)", [
                (1, "BrooksHouse Storefront", "store"), (2, "Store Back Room", "storage"),
                (3, "Warehouse", "warehouse"), (5, "Online Orders / Reserved", "reserved")])
            connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?)", [
                (1,10,1,"front",5,0,0,"x"),(2,10,2,"back",5,0,0,"x"),
                (3,10,3,"reserve",8,0,0,"x"),(4,10,5,"owed",0,0,0,"x")])
            connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-21','2026-08-21','2026-08-21',NULL,'PAID','UNFULFILLED',0)")
            connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1',10,'SKU',2,2,0,'matched','v1')")
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_is_idempotent_and_has_no_backfill(self):
        first = apply_to_copy(self.db)
        second = apply_to_copy(self.db)
        self.assertFalse(first.tables_to_create)
        self.assertFalse(second.tables_to_create)
        self.assertEqual(second.row_counts["channel_inventory_ledger"], 0)
        self.assertEqual(second.row_counts["channel_inventory_engine_control"], 0)

    def test_production_apply_is_refused(self):
        with self.assertRaises(ProductionWriteRefused):
            apply_to_copy(PRODUCTION_DB)

    def test_read_only_preview_uses_storefront(self):
        report = build_report(self.db, cutoff="2026-08-20T00:00:00+00:00")
        self.assertEqual(report["counts"], {"storefront_fulfillable": 1})
        self.assertEqual(report["rows"][0]["location_name"], "BrooksHouse Storefront")

    def test_legacy_marker_forces_review(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE shopify_sales_lines SET inventory_applied=1")
            connection.commit()
        report = build_report(self.db, cutoff="2026-08-20T00:00:00+00:00")
        self.assertEqual(report["counts"], {"legacy_overlap": 1})

    def test_controls_default_disabled_and_pause_wins(self):
        apply_to_copy(self.db)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(effective_control(connection, "shopify")["mode"], "disabled")
        set_copy_control(self.db, "global", mode="enabled", paused=False, cutover_at="2026-08-21", source_checkpoint="g1", reason="test")
        set_copy_control(self.db, "shopify", mode="dry_run", paused=True, cutover_at="2026-08-21", source_checkpoint="s1", reason="test pause")
        with closing(sqlite3.connect(self.db)) as connection:
            control = effective_control(connection, "shopify")
        self.assertEqual(control["mode"], "dry_run")
        self.assertTrue(control["paused"])


if __name__ == "__main__":
    unittest.main()
