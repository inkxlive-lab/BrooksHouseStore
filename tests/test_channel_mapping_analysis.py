import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.services.channel_inventory_reconciliation import connect_read_only
from app.services.channel_mapping_analysis import analyze_unmatched


SCHEMA = """
CREATE TABLE products(product_id INTEGER PRIMARY KEY,product_name TEXT,active INTEGER);
CREATE TABLE product_barcodes(barcode_id INTEGER PRIMARY KEY,product_id INTEGER,barcode TEXT);
CREATE TABLE master_catalog(catalog_id INTEGER PRIMARY KEY,barcode_lookup TEXT,barcode_exact TEXT,description TEXT);
CREATE TABLE inventory_locations(location_id INTEGER PRIMARY KEY,location_name TEXT,location_type TEXT,active INTEGER);
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,location_id INTEGER,container_id TEXT,quantity_on_hand INTEGER,quantity_reserved INTEGER);
CREATE TABLE sales_channels(channel_id INTEGER PRIMARY KEY,channel_name TEXT);
CREATE TABLE channel_listings(listing_id INTEGER PRIMARY KEY,channel_id INTEGER,external_product_id TEXT,external_variant_id TEXT,listing_title TEXT,variant_title TEXT,sku TEXT,barcode_raw TEXT,barcode_exact TEXT,barcode_lookup TEXT);
CREATE TABLE channel_sales_product_rules(rule_id INTEGER PRIMARY KEY,channel_name TEXT,source_key TEXT,source_title TEXT,source_sku TEXT,source_barcode TEXT,product_id INTEGER,rule_status TEXT);
CREATE TABLE amazon_listings(amazon_listing_id INTEGER PRIMARY KEY,seller_sku TEXT,asin TEXT);
CREATE TABLE amazon_product_links(amazon_product_link_id INTEGER PRIMARY KEY,amazon_listing_id INTEGER,product_id INTEGER,match_status TEXT);
CREATE TABLE walmart_listings(walmart_listing_id INTEGER PRIMARY KEY,seller_sku TEXT);
CREATE TABLE walmart_product_links(walmart_product_link_id INTEGER PRIMARY KEY,walmart_listing_id INTEGER,product_id INTEGER,match_status TEXT);
CREATE TABLE shopify_sales_orders(shopify_order_id TEXT PRIMARY KEY,processed_at TEXT,test_order INTEGER,cancelled_at TEXT,financial_status TEXT,fulfillment_status TEXT,refund_amount REAL);
CREATE TABLE shopify_sales_lines(shopify_line_id TEXT PRIMARY KEY,shopify_order_id TEXT,title TEXT,variant_title TEXT,sku TEXT,barcode TEXT,shopify_product_id TEXT,shopify_variant_id TEXT,product_id INTEGER,quantity INTEGER,current_quantity INTEGER,match_status TEXT,match_method TEXT,inventory_applied INTEGER);
CREATE TABLE amazon_order_history(amazon_order_id TEXT PRIMARY KEY,created_time TEXT,last_updated_time TEXT,fulfillment_status TEXT,fulfilled_by TEXT);
CREATE TABLE amazon_order_item_history(amazon_order_id TEXT,order_item_id TEXT,seller_sku TEXT,asin TEXT,title TEXT,quantity_ordered INTEGER,product_id INTEGER);
CREATE TABLE walmart_orders(purchase_order_id TEXT PRIMARY KEY,order_date TEXT,walmart_status TEXT,synced_at TEXT);
CREATE TABLE walmart_order_lines(order_line_id INTEGER PRIMARY KEY,purchase_order_id TEXT,line_number TEXT,sku TEXT,upc TEXT,item_name TEXT,quantity INTEGER,product_id INTEGER,line_status TEXT);
"""


def fixture_copy(directory: Path) -> Path:
    source = directory / "mapping-source.db"
    copied = directory / "mapping-copy.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO products VALUES(?,?,1)", [(10,"Exact Widget"),(11,"Rule Widget"),(12,"Blue Widget 12 Ounce"),(13,"Unrelated Item"),(14,"First Duplicate"),(15,"Second Duplicate")])
        connection.executemany("INSERT INTO product_barcodes VALUES(?,?,?)", [(1,10,'001234567890'),(2,14,'009999999999'),(3,15,'009999999999')])
        connection.execute("INSERT INTO master_catalog VALUES(1,'1234567890','001234567890','Exact Widget Catalog')")
        connection.execute("INSERT INTO inventory_locations VALUES(1,'BrooksHouse Storefront','store',1)")
        connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?)", [(1,10,1,'',20,0),(2,11,1,'',20,0),(3,12,1,'',20,0)])
        connection.execute("INSERT INTO sales_channels VALUES(1,'Shopify')")
        connection.execute("INSERT INTO channel_sales_product_rules VALUES(1,'shopify','shopify:variant:v2','Rule Widget','RULE','',11,'active')")
        connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-20',0,NULL,'paid','fulfilled',0)")
        connection.executemany("INSERT INTO shopify_sales_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ('L1','O1','Exact Widget','','','001234567890','P1','V1',None,2,2,'unmatched','',0),
            ('L2','O1','Exact Widget','','','001234567890','P1','V1',None,1,1,'unmatched','',0),
            ('L3','O1','Rule Widget','','RULE','','P2','V2',None,1,1,'unmatched','',0),
            ('L4','O1','Blue Widget 12 Ounce','','','','P3','V3',None,1,1,'unmatched','',0),
            ('L5','O1','Mystery Product','','','','P4','V4',None,1,1,'unmatched','',0),
            ('L6','O1','Duplicate Barcode Product','','','009999999999','P5','V5',None,1,1,'ambiguous','duplicate_barcode',0),
        ])
        connection.commit()
    shutil.copy2(source, copied)
    return copied


class MappingAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = fixture_copy(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_grouped_ranked_proposals_and_projection(self):
        with connect_read_only(self.database) as connection:
            proposals, summary = analyze_unmatched(connection, "2026-01-01")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE products SET active=0")
        by_key = {proposal.source_key: proposal for proposal in proposals}
        self.assertEqual(by_key["shopify:variant:v1"].match_classification, "A_EXACT")
        self.assertEqual(by_key["shopify:variant:v1"].order_line_count, 2)
        self.assertEqual(by_key["shopify:variant:v2"].match_classification, "B_STRONG")
        self.assertEqual(by_key["shopify:variant:v3"].match_classification, "C_CANDIDATE")
        self.assertEqual(by_key["shopify:variant:v4"].match_classification, "D_NO_MATCH")
        self.assertEqual(by_key["shopify:variant:v5"].match_classification, "E_AMBIGUOUS")
        self.assertEqual(summary["lines_matched_if_exact_approved"], 2)
        self.assertEqual(summary["additional_lines_if_strong_approved"], 1)
        self.assertEqual(summary["remaining_manual_review_lines_after_exact_and_strong"], 3)
        self.assertEqual(summary["projected_reconciliation_exact_only"]["unmatched_or_ambiguous"], 4)
        self.assertEqual(summary["projected_reconciliation_exact_and_strong"]["unmatched_or_ambiguous"], 3)


if __name__ == "__main__":
    unittest.main()
