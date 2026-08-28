import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.migrations.marketplace_publish_schema import initialize_schema
from app.services.hot_item_alerts import HotItemThresholds, evaluate_hot_item


class HotItemAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "alerts.db"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE products(product_id INTEGER PRIMARY KEY,product_name TEXT,brand TEXT,description TEXT,store_price NUMERIC,average_cost NUMERIC,active INTEGER);
            CREATE TABLE product_barcodes(barcode_id INTEGER PRIMARY KEY,product_id INTEGER,barcode TEXT,is_primary INTEGER);
            CREATE TABLE product_images(image_id INTEGER PRIMARY KEY,product_id INTEGER,image_url TEXT);
            CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,quantity_on_hand INTEGER,quantity_reserved INTEGER);
            CREATE TABLE walmart_catalog_matches(match_id INTEGER PRIMARY KEY,barcode_lookup TEXT,walmart_item_id TEXT,title TEXT,image_url TEXT,price_amount NUMERIC,price_currency TEXT,checked_at TEXT,match_status TEXT);
            CREATE TABLE walmart_listings(walmart_listing_id INTEGER PRIMARY KEY,seller_sku TEXT,walmart_item_id TEXT);
            CREATE TABLE walmart_product_links(walmart_product_link_id INTEGER PRIMARY KEY,walmart_listing_id INTEGER,product_id INTEGER,match_status TEXT);
            CREATE TABLE amazon_listings(amazon_listing_id INTEGER PRIMARY KEY,seller_sku TEXT,asin TEXT);
            CREATE TABLE amazon_product_links(amazon_product_link_id INTEGER PRIMARY KEY,amazon_listing_id INTEGER,product_id INTEGER,match_status TEXT);
        """)
        initialize_schema(self.db)
        self.db.execute("INSERT INTO products VALUES(1,'Treasure Widget','Acme','Useful',12,4,1)")
        self.db.execute("INSERT INTO product_barcodes VALUES(1,1,'012345678905',1)")
        self.db.execute("INSERT INTO inventory VALUES(1,1,0,0)")
        self.db.execute("INSERT INTO walmart_catalog_matches VALUES(1,'12345678905','WM-1','Walmart Widget',NULL,39.99,'USD','2026-08-27','MATCH')")
        self.db.execute("""INSERT INTO marketplace_publish_queue
            (channel,product_id,seller_sku,gtin,catalog_status,submission_type,proposed_price,
             proposed_quantity,shipping_weight_lb,estimated_shipping_cost,marketplace_fee_rate,
             status,idempotency_key,created_at,updated_at)
            VALUES('walmart',1,'BH-WM-1','012345678905','MATCH','offer',39.99,0,1,6,10,
                   'READY','alert-test','2026-08-27','2026-08-27')""")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_jackpot_inventory_hunt_is_read_only_and_preserves_scope(self):
        before = list(self.db.execute("SELECT * FROM inventory"))
        alert = evaluate_hot_item(
            self.db, 1, "012345678905", location_id=7, container_id="TRAILER-2",
            thresholds=HotItemThresholds.from_environment(),
        )
        self.assertEqual(alert["level"], "JACKPOT")
        self.assertTrue(alert["inventory_hunt"])
        self.assertEqual(alert["known_scope"], {"location_id": 7, "container_id": "TRAILER-2"})
        self.assertIn("location_id=7", alert["actions"][0]["url"])
        self.assertIn("container_id=TRAILER-2", alert["actions"][0]["url"])
        self.assertEqual(before, list(self.db.execute("SELECT * FROM inventory")))

    def test_thresholds_and_wording_are_configurable(self):
        with patch.dict(os.environ, {
            "HOT_ITEM_MIN_WALMART_PRICE": "100.00",
            "HOT_ITEM_JACKPOT_TITLE": "BrooksHouse treasure!",
        }):
            self.assertIsNone(evaluate_hot_item(self.db, 1, "012345678905"))
        with patch.dict(os.environ, {"HOT_ITEM_JACKPOT_TITLE": "BrooksHouse treasure!"}):
            alert = evaluate_hot_item(self.db, 1, "012345678905")
        self.assertEqual(alert["title"], "BrooksHouse treasure!")

    def test_non_walmart_match_has_no_alert(self):
        self.db.execute("UPDATE walmart_catalog_matches SET match_status='NOT_FOUND'")
        self.assertIsNone(evaluate_hot_item(self.db, 1, "012345678905"))


if __name__ == "__main__":
    unittest.main()
