import sqlite3
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
            connection.executemany("INSERT INTO products VALUES(?,?,?,?,?,?)",[
                (10,"Original Widget",1,1,"OldBrand","Old description"),
                (11,"Selected Widget",1,1,"Acme","Blue replacement widget"),
                (1040,"ACE RSBL CLD PK",1,1,"ACE","Reusable cold compress")])
            connection.executemany("INSERT INTO product_barcodes VALUES(?,?,?,1)",[
                (1,11,'987654321'),(2,1040,'051131204010')])
            connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)",[(1,"BrooksHouse Storefront","store"),(2,"Store Back Room","storage")])
            connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?)",[(1,10,1,"F",5,0,0,"x"),(2,11,2,"B",3,0,0,"x")])
            connection.execute("INSERT INTO walmart_orders VALUES('W1','2026-08-20','Created','v1')")
            connection.execute("INSERT INTO walmart_order_lines VALUES(101,'W1',10,1,'SKU-X','Marketplace Widget','Created')")
            connection.execute("INSERT INTO walmart_orders VALUES('W2','2026-08-20','Created','v1')")
            connection.execute("INSERT INTO walmart_order_lines VALUES(102,'W2',NULL,1,'SKU-X','Another order','Created')")
            connection.execute("INSERT INTO walmart_listings VALUES(1,'SKU-X')")
            connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-20',0,NULL,'unfulfilled')")
            connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1',10,1,1,'SHOP','Shop item','v1','matched')")
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_product_search_uses_name_description_brand_barcode_and_id(self):
        for term in ("Selected","replacement","Acme","987654","11"):
            with self.subTest(term=term):
                self.assertEqual(search_products(self.db,term)[0]["product_id"],11)

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


if __name__ == "__main__":
    unittest.main()
