import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.services.approved_mapping_application import MappingPlan, apply_safe_plans, inventory_fingerprint


SCHEMA = """
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,location_id INTEGER,container_id TEXT,quantity_on_hand INTEGER,quantity_reserved INTEGER,reorder_level INTEGER,updated_at TEXT);
CREATE TABLE inventory_transactions(transaction_id INTEGER PRIMARY KEY);
CREATE TABLE products(product_id INTEGER PRIMARY KEY,product_name TEXT,active INTEGER);
CREATE TABLE shopify_sales_lines(shopify_line_id TEXT PRIMARY KEY,shopify_order_id TEXT,product_id INTEGER,match_status TEXT,match_method TEXT,updated_at TEXT);
CREATE TABLE channel_sales_product_rules(rule_id INTEGER PRIMARY KEY,channel_name TEXT,source_key TEXT UNIQUE,source_title TEXT,source_sku TEXT,source_barcode TEXT,product_id INTEGER,rule_status TEXT,created_at TEXT,updated_at TEXT);
CREATE TABLE channel_match_audit(audit_id INTEGER PRIMARY KEY,channel_name TEXT,source_key TEXT,source_title TEXT,source_sku TEXT,source_barcode TEXT,product_id INTEGER,action_name TEXT,match_method TEXT,confidence INTEGER,affected_lines INTEGER,affected_units INTEGER,affected_sales REAL,source_row_ids_json TEXT,created_at TEXT);
"""


class ApprovedMappingApplicationTests(unittest.TestCase):
    def test_apply_uses_existing_mapping_tables_and_preserves_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            copied = Path(directory) / "copy.db"
            with closing(sqlite3.connect(source)) as connection:
                connection.executescript(SCHEMA)
                connection.execute("INSERT INTO inventory VALUES(1,10,1,'',7,2,0,'before')")
                connection.execute("INSERT INTO products VALUES(10,'Widget',1)")
                connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1',NULL,'unmatched','',NULL)")
                connection.commit()
            shutil.copy2(source, copied)
            with closing(sqlite3.connect(copied)) as connection:
                before = inventory_fingerprint(connection)
                plan = MappingPlan(
                    channel="shopify", source_key="shopify:variant:v1", product_id=10,
                    product_name="Widget", source_title="Widget", source_sku="SKU",
                    source_barcode="", line_ids=[{"order_id": "O1", "order_line_id": "L1"}],
                    affected_lines=1, affected_units=2, status="safe", reason="", operations=[],
                )
                connection.execute("BEGIN IMMEDIATE")
                results = apply_safe_plans(connection, [plan])
                connection.commit()
                after = inventory_fingerprint(connection)
                mapped = connection.execute("SELECT product_id,match_method FROM shopify_sales_lines").fetchone()
                rule = connection.execute("SELECT product_id FROM channel_sales_product_rules").fetchone()
                audit = connection.execute("SELECT action_name FROM channel_match_audit").fetchone()
            self.assertEqual(results[0]["apply_status"], "applied")
            self.assertEqual(tuple(mapped), (10, "martel_review_20260821"))
            self.assertEqual(tuple(rule), (10,))
            self.assertEqual(tuple(audit), ("approved_strong_apply",))
            self.assertEqual(before, after)

    def test_conflict_plan_is_skipped(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            plan = MappingPlan(
                channel="shopify", source_key="shopify:variant:v1", product_id=10,
                product_name="Widget", source_title="Widget", source_sku="SKU",
                source_barcode="", line_ids=[], affected_lines=1, affected_units=1,
                status="conflict", reason="different existing product", operations=[],
            )
            results = apply_safe_plans(connection, [plan])
        self.assertEqual(results[0]["apply_status"], "conflict_skipped")


if __name__ == "__main__":
    unittest.main()
