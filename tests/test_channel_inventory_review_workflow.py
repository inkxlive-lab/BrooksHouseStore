import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.requests import Request
from jinja2 import Environment, FileSystemLoader

from app.channel_inventory_admin import install_channel_inventory_admin
from app.services.approved_mapping_application import inventory_fingerprint
from app.services.channel_inventory_review_workflow import (
    EXPLICIT_MAPPING_CONFIRMATION, EXPLICIT_REVIEW_CONFIRMATION, apply_confirmed_mapping,
    mapping_confirmation_preview, mark_reviewed, search_products,
)
from tests.test_channel_inventory_engine import SCHEMA


class ChannelInventoryReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name)/"copy.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(SCHEMA)
            connection.execute("ALTER TABLE products ADD COLUMN brand TEXT")
            connection.execute("ALTER TABLE products ADD COLUMN description TEXT")
            connection.execute("ALTER TABLE operations_work_queue ADD COLUMN completed_at TEXT")
            connection.execute("ALTER TABLE walmart_orders ADD COLUMN raw_json TEXT")
            connection.execute("ALTER TABLE walmart_order_lines ADD COLUMN line_number TEXT")
            connection.execute("ALTER TABLE walmart_order_lines ADD COLUMN upc TEXT")
            connection.execute("ALTER TABLE walmart_listings ADD COLUMN item_name TEXT")
            connection.execute("ALTER TABLE walmart_listings ADD COLUMN created_at TEXT")
            connection.execute("ALTER TABLE walmart_listings ADD COLUMN updated_at TEXT")
            connection.executescript("""
                CREATE TABLE sales_channels(channel_id INTEGER PRIMARY KEY,channel_name TEXT);
                CREATE TABLE channel_listings(
                    listing_id INTEGER PRIMARY KEY,channel_id INTEGER,external_product_id TEXT,
                    external_variant_id TEXT,listing_title TEXT,sku TEXT,barcode_raw TEXT,
                    barcode_exact TEXT,barcode_lookup TEXT,listing_status TEXT);
            """)
            connection.executemany("INSERT INTO products VALUES(?,?,?,?,?,?)",[
                (10,"Original Widget",1,1,"OldBrand","Old description"),
                (11,"Selected Widget",1,1,"Acme","Blue replacement widget"),
                (1040,"ACE RSBL CLD PK",1,1,"ACE","Reusable cold compress"),
                (1929,"Bialetti Impact Sauce Pan",1,1,"Bialetti",
                 "Impact textured nonstick surface oil distribution 2 quart sauce pan gray")])
            connection.executemany("INSERT INTO product_barcodes VALUES(?,?,?,1)",[
                (1,11,'987654321'),(2,1040,'051131204010'),(3,1929,'076753075572')])
            connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)",[(1,"BrooksHouse Storefront","store"),(2,"Store Back Room","storage")])
            connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?)",[(1,10,1,"F",5,0,0,"x"),(2,11,2,"B",3,0,0,"x"),(3,1929,2,"ON-THE-TABLE",18,0,0,"x")])
            connection.execute("INSERT INTO sales_channels VALUES(1,'walmart')")
            connection.execute("INSERT INTO channel_listings VALUES(1,1,'1W8WPXMHC4Y8','bp',?,'bp',?,?,?,'PUBLISHED')",
                               ("Bialetti Impact textured nonstick surface, oil distribution, 2 quart sauce pan, gray",
                                "076753075572","076753075572","76753075572"))
            connection.execute("INSERT INTO amazon_listings VALUES(1,'AMZ-SKU','B012TEST','Active','sellable')")
            connection.execute("INSERT INTO amazon_product_links VALUES(1,1,11,'linked')")
            raw = lambda title: json.dumps({"orderLines":{"orderLine":[{
                "lineNumber":"1","item":{"sku":"SKU-X","productName":title}}]}})
            connection.execute("INSERT INTO walmart_orders VALUES('W1','2026-08-20','Created','v1',?)",(raw("Marketplace Widget"),))
            connection.execute("INSERT INTO walmart_order_lines VALUES(101,'W1',10,1,'SKU-X','Marketplace Widget','Created','1','')")
            connection.execute("INSERT INTO walmart_orders VALUES('W2','2026-08-20','Created','v1',?)",(raw("Marketplace Widget"),))
            connection.execute("INSERT INTO walmart_order_lines VALUES(102,'W2',NULL,1,'SKU-X','Marketplace Widget','Created','1','')")
            connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-20',0,NULL,'unfulfilled')")
            connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1',10,1,1,'SHOP','Shop item','v1','matched')")
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_product_search_uses_name_description_brand_barcode_and_id(self):
        for term in ("Selected","replacement","Acme","987654","11"):
            with self.subTest(term=term):
                self.assertEqual(search_products(self.db,term)[0]["product_id"],11)

    def test_product_search_prioritizes_exact_id_barcode_and_walmart_sku(self):
        with closing(sqlite3.connect(self.db)) as connection:
            before = inventory_fingerprint(connection)
        cases = {
            "1929":"Exact product ID",
            "076753075572":"Exact barcode",
            "bp":"Exact marketplace identity",
            "Bialetti Impact textured nonstick surface":"Description",
        }
        for term, reason in cases.items():
            with self.subTest(term=term):
                result = search_products(self.db,term)
                self.assertEqual(result[0]["product_id"],1929)
                self.assertEqual(result[0]["match_reason"],reason)
        self.assertEqual(search_products(self.db,"999999"),[])
        self.assertEqual(search_products(self.db,"no product can match this phrase"),[])
        self.assertEqual(search_products(self.db,"AMZ-SKU")[0]["product_id"],11)
        self.assertEqual(search_products(self.db,"B012TEST")[0]["product_id"],11)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(before,inventory_fingerprint(connection))

    def test_mapping_requires_preview_and_exact_confirmation_without_inventory_change(self):
        preview = mapping_confirmation_preview(self.db,"walmart","W1","101",1040)
        self.assertEqual(preview["selected_product"]["product_id"],1040)
        for phrase in ("", "yes", "1040"):
            with self.subTest(phrase=phrase), self.assertRaisesRegex(ValueError,"Exact mapping confirmation"):
                apply_confirmed_mapping(self.db,preview,confirmation=phrase)
        with closing(sqlite3.connect(self.db)) as connection:
            before = inventory_fingerprint(connection)
            transaction_before = connection.execute("SELECT COUNT(*),COALESCE(MAX(transaction_id),0) FROM inventory_transactions").fetchone()
        result = apply_confirmed_mapping(self.db,preview,confirmation=EXPLICIT_MAPPING_CONFIRMATION)
        with closing(sqlite3.connect(self.db)) as connection:
            after = inventory_fingerprint(connection)
            line_product = connection.execute("SELECT product_id FROM walmart_order_lines WHERE order_line_id=101").fetchone()[0]
            unrelated_product = connection.execute("SELECT product_id FROM walmart_order_lines WHERE order_line_id=102").fetchone()[0]
            link = connection.execute("SELECT product_id,match_status FROM walmart_product_links").fetchone()
            transaction_after = connection.execute("SELECT COUNT(*),COALESCE(MAX(transaction_id),0) FROM inventory_transactions").fetchone()
        self.assertEqual(before,after)
        self.assertEqual(transaction_before,transaction_after)
        self.assertTrue(result["inventory_unchanged"])
        self.assertEqual(result["affected_order_lines"],1)
        self.assertEqual((line_product,unrelated_product,link),(1040,None,(1040,"linked")))

    def test_mapping_transaction_rolls_back_completely_on_inventory_guard_failure(self):
        preview = mapping_confirmation_preview(self.db,"walmart","W1","101",1040)
        with patch("app.services.channel_inventory_review_workflow.inventory_fingerprint",
                   side_effect=[{"fingerprint":"before"},{"fingerprint":"changed"}]):
            with self.assertRaisesRegex(RuntimeError,"unexpectedly changed inventory"):
                apply_confirmed_mapping(self.db,preview,confirmation=EXPLICIT_MAPPING_CONFIRMATION)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT product_id FROM walmart_order_lines WHERE order_line_id=101").fetchone()[0],10)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM walmart_product_links").fetchone()[0],0)

    def test_missing_listing_bootstraps_only_from_consistent_retained_walmart_evidence(self):
        preview = mapping_confirmation_preview(self.db,"walmart","W1","101",1040)
        result = apply_confirmed_mapping(self.db,preview,confirmation=EXPLICIT_MAPPING_CONFIRMATION)
        with closing(sqlite3.connect(self.db)) as connection:
            listing = connection.execute(
                "SELECT seller_sku,item_name FROM walmart_listings").fetchone()
        self.assertEqual((result["affected_order_lines"],listing),(1,("SKU-X","Marketplace Widget")))

    def test_missing_listing_refuses_conflicting_historical_sku_identity(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE walmart_order_lines SET item_name='Different product' WHERE order_line_id=102")
            connection.commit()
        preview = mapping_confirmation_preview(self.db,"walmart","W1","101",1040)
        with self.assertRaisesRegex(RuntimeError,"seller SKU ambiguous"):
            apply_confirmed_mapping(self.db,preview,confirmation=EXPLICIT_MAPPING_CONFIRMATION)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM walmart_listings").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM walmart_product_links").fetchone()[0],0)

    def test_mark_reviewed_writes_metadata_only(self):
        with self.assertRaises(ValueError):
            mark_reviewed(self.db,"shopify","O1","L1","owner",confirmation="")
        with closing(sqlite3.connect(self.db)) as connection:
            before = inventory_fingerprint(connection)
        result = mark_reviewed(self.db,"shopify","O1","L1","owner",confirmation=EXPLICIT_REVIEW_CONFIRMATION)
        with closing(sqlite3.connect(self.db)) as connection:
            after = inventory_fingerprint(connection)
            task = connection.execute("SELECT task_type,status FROM operations_work_queue").fetchone()
        self.assertEqual((before,after),(after,after))
        self.assertEqual((result["inventory_unchanged"],task),(True,("channel_inventory_review","completed")))

    def test_routes_and_template_expose_required_safe_workflow(self):
        app = FastAPI(); install_channel_inventory_admin(app)
        paths = {route.path for route in app.routes}
        self.assertTrue({"/admin/channel-inventory-review","/admin/channel-inventory-review/mapping-preview",
                         "/admin/channel-inventory-review/confirm-mapping","/admin/channel-inventory-review/mark-reviewed"} <= paths)
        template = Path("app/templates/channel_inventory_review.html").read_text(encoding="utf-8")
        for text in ("Safe / Would Deduct","Mapping Required","Inventory Location Review","Historical / Ignore","/inventory/transfer"):
            self.assertIn(text,template)
        self.assertIn('name="selected_product_id"',template)
        self.assertIn('name="confirmation_phrase"',template)
        self.assertIn('value="" autocomplete="off"',template)
        self.assertIn("No matching product.",template)
        Environment(loader=FileSystemLoader("app/templates")).get_template("channel_inventory_review.html")

    def test_incorrect_confirmation_returns_friendly_ui_error_instead_of_500(self):
        app = FastAPI(); install_channel_inventory_admin(app)
        endpoint = next(route.endpoint for route in app.routes
                        if route.path == "/admin/channel-inventory-review/confirm-mapping")
        request = Request({"type":"http","method":"POST","path":"/admin/channel-inventory-review/confirm-mapping",
                           "headers":[],"query_string":b"","server":("test",80),"client":("test",1),"scheme":"http"})
        request.state.auth_user = SimpleNamespace(role="owner_admin")
        preview = mapping_confirmation_preview(self.db,"walmart","W1","101",1040)
        with patch("app.channel_inventory_admin.PRODUCTION_DB",self.db), \
             patch("app.channel_inventory_admin.mapping_confirmation_preview",return_value=preview), \
             patch("app.channel_inventory_admin._review_context",return_value={"request":request,"error":"Exact mapping confirmation is required"}), \
             patch("app.channel_inventory_admin.templates.TemplateResponse",
                   return_value=HTMLResponse("Mapping was not changed. Exact mapping confirmation is required",status_code=400)):
            response = endpoint(request,"walmart","W1","101",1040,"1040",30)
        self.assertEqual(response.status_code,400)
        self.assertIn(b"Mapping was not changed",response.body)

    def test_invalid_preview_product_returns_friendly_error_not_500(self):
        app = FastAPI(); install_channel_inventory_admin(app)
        endpoint = next(route.endpoint for route in app.routes
                        if route.path == "/admin/channel-inventory-review/mapping-preview")
        request = Request({"type":"http","method":"POST","path":"/admin/channel-inventory-review/mapping-preview",
                           "headers":[],"query_string":b"","server":("test",80),"client":("test",1),"scheme":"http"})
        request.state.auth_user = SimpleNamespace(role="owner_admin")
        with patch("app.channel_inventory_admin.PRODUCTION_DB",self.db), \
             patch("app.channel_inventory_admin._review_context",return_value={"request":request,"error":"No matching product."}), \
             patch("app.channel_inventory_admin.templates.TemplateResponse",
                   return_value=HTMLResponse("No matching product.",status_code=400)):
            response = endpoint(request,"walmart","W1","101",999999,30)
        self.assertEqual(response.status_code,400)
        self.assertIn(b"No matching product",response.body)


if __name__ == "__main__":
    unittest.main()
