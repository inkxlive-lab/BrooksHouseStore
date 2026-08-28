import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates

from app.marketplace_publish import (
    _product,
    channel_state,
    publish_page_data,
    refresh_publish,
    save_draft,
    submit_publish,
    validate_draft,
    install_marketplace_publish,
    walmart_candidate_products,
    walmart_economics,
    walmart_opportunities,
)
from app.migrations.marketplace_publish_schema import initialize_schema, schema_installed


BASE_SCHEMA = """
CREATE TABLE products(product_id INTEGER PRIMARY KEY,product_name TEXT,brand TEXT,description TEXT,category TEXT,store_price NUMERIC,average_cost NUMERIC,active INTEGER);
CREATE TABLE product_barcodes(barcode_id INTEGER PRIMARY KEY,product_id INTEGER,barcode TEXT,is_primary INTEGER);
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,quantity_on_hand INTEGER,quantity_reserved INTEGER);
CREATE TABLE product_images(image_id INTEGER PRIMARY KEY,product_id INTEGER,image_url TEXT,image_path TEXT,image_type TEXT,is_primary INTEGER);
CREATE TABLE walmart_catalog_matches(match_id INTEGER PRIMARY KEY,barcode_lookup TEXT,barcode_exact TEXT,query_value TEXT,item_id TEXT,walmart_item_id TEXT,title TEXT,brand TEXT,description TEXT,product_type TEXT,image_url TEXT,price_amount NUMERIC,price_currency TEXT,checked_at TEXT,error_message TEXT,standard_upc TEXT,match_status TEXT);
CREATE TABLE walmart_listings(walmart_listing_id INTEGER PRIMARY KEY,seller_sku TEXT UNIQUE,walmart_item_id TEXT,walmart_price NUMERIC,walmart_quantity INTEGER);
CREATE TABLE walmart_product_links(walmart_product_link_id INTEGER PRIMARY KEY,walmart_listing_id INTEGER,product_id INTEGER,match_status TEXT);
CREATE TABLE amazon_listings(amazon_listing_id INTEGER PRIMARY KEY,seller_sku TEXT UNIQUE,asin TEXT,amazon_price NUMERIC,amazon_quantity INTEGER,approval_status TEXT,inventory_status TEXT);
CREATE TABLE amazon_product_links(amazon_product_link_id INTEGER PRIMARY KEY,amazon_listing_id INTEGER,product_id INTEGER,match_status TEXT);
CREATE TABLE amazon_catalog_match_audit(amazon_listing_id INTEGER PRIMARY KEY,asin TEXT,seller_sku TEXT,identifiers_json TEXT,result_status TEXT,matched_product_id INTEGER,checked_at TEXT);
"""


class SuccessAdapter:
    def __init__(self, response=None):
        self.calls = 0
        self.response = response or {"status": "SUBMITTED", "external_submission_id": "FEED-1"}

    def submit(self, payload):
        self.calls += 1
        return dict(self.response)

    def refresh(self, row):
        return {"status": "PUBLISHED", "external_submission_id": row.get("external_submission_id")}


class FailureAdapter(SuccessAdapter):
    def submit(self, payload):
        self.calls += 1
        raise RuntimeError("mock submission failure")


class MarketplacePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "publish.db"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.route_connections = []
        self.db.executescript(BASE_SCHEMA)
        initialize_schema(self.db)
        self.db.execute("INSERT INTO products VALUES(1,'Widget','Acme','Useful widget','Home',12.50,4.00,1)")
        self.db.execute("INSERT INTO product_barcodes VALUES(1,1,'012345678905',1)")
        self.db.execute("INSERT INTO inventory VALUES(1,1,14,0)")
        self.db.execute("INSERT INTO product_images VALUES(1,1,'/static/widget.jpg',NULL,'original',1)")
        self.db.execute("""INSERT INTO walmart_catalog_matches
            (match_id,barcode_lookup,barcode_exact,query_value,walmart_item_id,title,brand,description,
             image_url,price_amount,price_currency,checked_at,standard_upc,match_status)
            VALUES(1,'12345678905','012345678905','012345678905','WM1','Walmart Widget','Acme',
                   'Useful','https://walmart.example/w.jpg',7.99,'USD','2026-08-01T00:00:00+00:00',
                   '012345678905','MATCH')""")
        self.db.execute("INSERT INTO amazon_catalog_match_audit VALUES(1,'B000TEST','OLD',?, 'unique',1,'2026-08-27')",
                        (json.dumps([["UPC", "012345678905"]]),))
        self.db.commit()

    def tearDown(self):
        for connection in self.route_connections:
            connection.close()
        self.db.close()
        self.temp.cleanup()

    def product(self):
        return _product(self.db, 1)

    def draft(self, channel="walmart", price="12.50", quantity="4", sku="", image=1,
              weight="1.00", shipping="6.00", fee_rate=""):
        return save_draft(self.db, channel=channel, product_id=1, seller_sku=sku,
                          proposed_price=price, proposed_quantity=quantity, selected_image_id=image,
                          shipping_weight_lb=weight, estimated_shipping_cost=shipping,
                          marketplace_fee_rate=fee_rate)

    def add_walmart_opportunity(self, product_id, price, quantity=0, average_cost=4.00):
        barcode = f"0123456789{product_id:02d}"
        lookup = barcode.lstrip("0")
        self.db.execute(
            "INSERT INTO products VALUES(?,?,?,?,?,?,?,1)",
            (product_id, f"Opportunity {product_id}", "Acme", "Trailer item", "Home", price, average_cost),
        )
        self.db.execute(
            "INSERT INTO product_barcodes VALUES(?,?,?,1)", (product_id, product_id, barcode)
        )
        self.db.execute(
            "INSERT INTO inventory VALUES(?,?,?,0)", (product_id, product_id, quantity)
        )
        self.db.execute(
            """INSERT INTO walmart_catalog_matches
               (match_id,barcode_lookup,barcode_exact,query_value,walmart_item_id,title,brand,description,
                image_url,price_amount,price_currency,checked_at,standard_upc,match_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (product_id, lookup, barcode, barcode, f"WM{product_id}", f"Opportunity {product_id}",
             "Acme", "Trailer item", None, price, "USD", "2026-08-01T00:00:00+00:00", barcode, "MATCH"),
        )

    def route_connection(self):
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        self.route_connections.append(connection)
        return connection

    def test_schema_is_explicit_and_additive(self):
        initialize_schema(self.db)
        self.assertTrue(schema_installed(self.db))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM marketplace_publish_queue").fetchone()[0], 0)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(marketplace_publish_queue)")}
        self.assertTrue({"shipping_weight_lb", "estimated_shipping_cost", "marketplace_fee_rate"} <= columns)

    def test_walmart_catalog_match_uses_offer_path(self):
        state = channel_state(self.db, self.product(), "walmart")
        self.assertEqual((state["submission_type"], state["external_catalog_id"]), ("offer", "WM1"))
        self.assertEqual(state["catalog_result"]["title"], "Walmart Widget")
        self.assertEqual(state["catalog_result"]["price_amount"], 7.99)
        self.assertIn("Stale Walmart price snapshot", state["walmart_price_note"])

    def test_default_candidates_only_include_walmart_match_or_listing(self):
        self.db.execute("INSERT INTO products VALUES(2,'Review Me','Acme','Review','Home',5,2,1)")
        self.db.execute("INSERT INTO product_barcodes VALUES(2,2,'099999999999',1)")
        self.db.execute("""INSERT INTO walmart_catalog_matches
            (match_id,barcode_lookup,barcode_exact,query_value,match_status)
            VALUES(2,'99999999999','099999999999','099999999999','NOT_FOUND')""")
        eligible = walmart_candidate_products(self.db, "walmart_eligible")
        review = walmart_candidate_products(self.db, "walmart_review")
        self.assertEqual([item["product_id"] for item in eligible], [1])
        self.assertEqual([item["product_id"] for item in review], [2])

    def test_amazon_eligibility_is_independent_of_walmart_rejection(self):
        self.db.execute("UPDATE walmart_catalog_matches SET match_status='INVALID_BARCODE',walmart_item_id=NULL")
        product = self.product()
        self.assertFalse(channel_state(self.db, product, "walmart")["eligible"])
        self.assertEqual(channel_state(self.db, product, "amazon")["external_catalog_id"], "B000TEST")

    def test_walmart_weight_is_required_and_planning_fields_persist(self):
        self.assertEqual(self.draft(weight="")["status"], "NEEDS ATTENTION")
        self.assertEqual(self.draft(fee_rate="101")["status"], "NEEDS ATTENTION")
        row = self.draft(weight="2.25", shipping="6.75", fee_rate="15")
        self.assertEqual(row["status"], "READY")
        self.assertEqual(
            (float(row["shipping_weight_lb"]), float(row["estimated_shipping_cost"]), float(row["marketplace_fee_rate"])),
            (2.25, 6.75, 15.0),
        )
        self.assertEqual(self.db.execute("SELECT store_price FROM products WHERE product_id=1").fetchone()[0], 12.5)

    def test_low_price_shipping_economics_are_flagged(self):
        state = channel_state(self.db, self.product(), "walmart")
        state.update({"proposed_price": "7.99", "estimated_shipping_cost": "6.00", "marketplace_fee_rate": None})
        result = walmart_economics(self.product(), state)
        self.assertTrue(result["poor"])
        self.assertEqual(str(result["before_fee_profit"]), "-2.01")

    def test_walmart_not_found_is_review_only_not_ready(self):
        self.db.execute("UPDATE walmart_catalog_matches SET match_status='NOT_FOUND',walmart_item_id=NULL")
        state = channel_state(self.db, self.product(), "walmart")
        self.assertFalse(state["eligible"])
        self.assertEqual(state["catalog_status"], "NOT_FOUND")
        self.assertEqual(self.draft()["status"], "NEEDS ATTENTION")

    def test_amazon_asin_match_uses_offer_path(self):
        state = channel_state(self.db, self.product(), "amazon")
        self.assertEqual((state["submission_type"], state["external_catalog_id"]), ("offer", "B000TEST"))

    def test_amazon_not_found_new_product_when_complete(self):
        self.db.execute("DELETE FROM amazon_catalog_match_audit")
        state = channel_state(self.db, self.product(), "amazon")
        self.assertEqual(state["submission_type"], "new_product")
        errors = validate_draft(self.db, self.product(), state, "9.99", 1, "BH-AMZ-1", 1)
        self.assertTrue(any("product-type attributes" in item for item in errors))

    def test_invalid_gtin_is_blocked(self):
        self.db.execute("UPDATE product_barcodes SET barcode='123' WHERE product_id=1")
        row = self.draft()
        self.assertEqual(row["status"], "NEEDS ATTENTION")
        self.assertIn("GTIN", row["validation_json"])

    def test_missing_price_is_blocked(self):
        self.assertEqual(self.draft(price="")["status"], "NEEDS ATTENTION")

    def test_missing_image_is_blocked(self):
        self.assertEqual(self.draft(image=None)["status"], "NEEDS ATTENTION")

    def test_channel_quantity_cannot_exceed_availability(self):
        self.assertEqual(self.draft(quantity="15")["status"], "NEEDS ATTENTION")

    def test_combined_channel_quantities_are_validated(self):
        self.assertEqual(self.draft("walmart", quantity="10")["status"], "READY")
        self.assertEqual(self.draft("amazon", quantity="10")["status"], "NEEDS ATTENTION")

    def test_publishing_does_not_deduct_inventory(self):
        before = tuple(self.db.execute("SELECT quantity_on_hand,quantity_reserved FROM inventory").fetchone())
        row = self.draft()
        submit_publish(self.db, row["publish_id"], SuccessAdapter())
        after = tuple(self.db.execute("SELECT quantity_on_hand,quantity_reserved FROM inventory").fetchone())
        self.assertEqual(before, after)

    def test_existing_walmart_listing_is_already_listed(self):
        self.db.execute("INSERT INTO walmart_listings VALUES(1,'EXISTING-WM','WM1',10,2)")
        self.db.execute("INSERT INTO walmart_product_links VALUES(1,1,1,'linked')")
        row = self.draft(sku="DIFFERENT")
        self.assertEqual((row["status"], row["seller_sku"]), ("ALREADY LISTED", "EXISTING-WM"))
        self.assertEqual(channel_state(self.db, self.product(), "walmart")["submission_type"], "offer")

    def test_existing_amazon_listing_is_already_listed(self):
        self.db.execute("INSERT INTO amazon_listings VALUES(1,'EXISTING-AMZ','B001',10,2,'approved','in_stock')")
        self.db.execute("INSERT INTO amazon_product_links VALUES(1,1,1,'linked')")
        row = self.draft("amazon", sku="DIFFERENT")
        self.assertEqual((row["status"], row["seller_sku"]), ("ALREADY LISTED", "EXISTING-AMZ"))

    def test_existing_seller_sku_is_preserved(self):
        self.db.execute("INSERT INTO amazon_listings VALUES(1,'KEEP-ME','B001',10,2,'approved','in_stock')")
        self.db.execute("INSERT INTO amazon_product_links VALUES(1,1,1,'linked')")
        self.assertEqual(self.draft("amazon", sku="REPLACE-ME")["seller_sku"], "KEEP-ME")

    def test_sku_cannot_move_to_another_product(self):
        self.db.execute("INSERT INTO products VALUES(2,'Other','Acme','Other','Home',5,2,1)")
        self.db.execute("INSERT INTO product_barcodes VALUES(2,2,'099999999999',1)")
        self.db.execute("INSERT INTO inventory VALUES(2,2,2,0)")
        self.db.execute("INSERT INTO product_images VALUES(2,2,'/static/other.jpg',NULL,'original',1)")
        self.draft(sku="COLLIDE")
        other = _product(self.db, 2); state = channel_state(self.db, other, "walmart")
        errors = validate_draft(self.db, other, state, "5", 1, "COLLIDE", 2)
        self.assertTrue(any("different" in item for item in errors))

    def test_double_submission_is_prevented(self):
        row = self.draft(); adapter = SuccessAdapter()
        submit_publish(self.db, row["publish_id"], adapter)
        submit_publish(self.db, row["publish_id"], adapter)
        self.assertEqual(adapter.calls, 1)

    def test_failed_walmart_submission_is_consistent(self):
        row = self.draft(); result = submit_publish(self.db, row["publish_id"], FailureAdapter())
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("mock submission failure", result["error_message"])

    def test_failed_amazon_submission_is_consistent(self):
        row = self.draft("amazon"); result = submit_publish(self.db, row["publish_id"], FailureAdapter())
        self.assertEqual(result["status"], "FAILED")

    def test_successful_walmart_submission_stores_feed_id(self):
        row = self.draft(); result = submit_publish(self.db, row["publish_id"], SuccessAdapter())
        self.assertEqual(result["external_submission_id"], "FEED-1")

    def test_successful_amazon_submission_stores_identifiers(self):
        row = self.draft("amazon")
        adapter = SuccessAdapter({"status": "SUBMITTED", "submission_id": "AMZ-1", "asin": "BNEW"})
        result = submit_publish(self.db, row["publish_id"], adapter)
        self.assertEqual((result["external_submission_id"], result["external_catalog_id"]), ("AMZ-1", "BNEW"))

    def test_status_refresh_does_not_duplicate_submission(self):
        row = self.draft(); adapter = SuccessAdapter(); submitted = submit_publish(self.db, row["publish_id"], adapter)
        refreshed = refresh_publish(self.db, submitted["publish_id"], adapter)
        self.assertEqual((adapter.calls, refreshed["status"]), (1, "PUBLISHED"))

    def test_walmart_usable_when_amazon_unconfigured(self):
        with patch.dict(os.environ, {"WALMART_CLIENT_ID": "x", "WALMART_CLIENT_SECRET": "y"}, clear=True):
            data = publish_page_data(self.db, 1)
        self.assertTrue(data["states"]["walmart"]["configured"])
        self.assertFalse(data["states"]["amazon"]["configured"])

    def test_no_credentials_are_stored_in_queue_or_events(self):
        with patch.dict(os.environ, {"WALMART_CLIENT_ID": "secret-id", "WALMART_CLIENT_SECRET": "secret-value"}):
            self.draft()
        dumped = " ".join(str(value) for row in self.db.execute("SELECT * FROM marketplace_publish_queue") for value in row)
        dumped += " ".join(str(value) for row in self.db.execute("SELECT * FROM marketplace_publish_events") for value in row)
        self.assertNotIn("secret-value", dumped)

    def test_audit_events_are_immutable(self):
        self.draft()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE marketplace_publish_events SET result='changed'")

    def test_publish_center_shows_image_and_workflow_links(self):
        data = publish_page_data(self.db, 1)
        self.assertEqual(data["product"]["primary_image"]["display_url"], "/static/widget.jpg")
        app = FastAPI()
        templates = Jinja2Templates(directory=Path(__file__).parents[1] / "app" / "templates")
        install_marketplace_publish(app, templates)
        with patch("app.marketplace_publish.connect", side_effect=self.route_connection):
            with TestClient(app) as client:
                response = client.get("/channels/publish?product_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn('/smart-scan?product_id=1', response.text)
        self.assertIn('/images/studio?product_id=1', response.text)
        self.assertIn('/static/widget.jpg', response.text)

    def test_publish_center_accepts_empty_optional_product_id(self):
        app = FastAPI()
        templates = Jinja2Templates(directory=Path(__file__).parents[1] / "app" / "templates")
        install_marketplace_publish(app, templates)
        with patch("app.marketplace_publish.connect", side_effect=self.route_connection):
            with TestClient(app) as client:
                base = client.get("/channels/publish?candidate_filter=walmart_eligible")
                empty = client.get("/channels/publish?candidate_filter=walmart_eligible&product_id=")
                invalid = client.get("/channels/publish?product_id=not-a-product")
        self.assertEqual(base.status_code, 200)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(invalid.status_code, 422)

    def test_publish_center_reports_no_saved_image(self):
        self.db.execute("DELETE FROM product_images WHERE product_id=1")
        app = FastAPI()
        templates = Jinja2Templates(directory=Path(__file__).parents[1] / "app" / "templates")
        install_marketplace_publish(app, templates)
        self.db.commit()
        with patch("app.marketplace_publish.connect", side_effect=self.route_connection):
            with TestClient(app) as client:
                response = client.get("/channels/publish?product_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No saved image", response.text)

    def test_approved_ai_studio_image_is_available_to_publish_center(self):
        self.db.execute("UPDATE product_images SET is_primary=0 WHERE product_id=1")
        self.db.execute(
            "INSERT INTO product_images VALUES(2,1,NULL,'/images/studio/files/approved/product-1.png','ai_studio',1)"
        )
        product = publish_page_data(self.db, 1)["product"]
        self.assertEqual(
            product["primary_image"]["display_url"],
            "/images/studio/files/approved/product-1.png",
        )

    def test_search_covers_exact_barcode_description_marketplace_ids_and_skus(self):
        self.db.execute("INSERT INTO walmart_listings VALUES(1,'WM-SKU-ONE','WM1',7.99,3)")
        self.db.execute("INSERT INTO walmart_product_links VALUES(1,1,1,'linked')")
        self.db.execute("INSERT INTO amazon_listings VALUES(1,'AMZ-SKU-ONE','B000TEST',12.50,3,'approved','active')")
        self.db.execute("INSERT INTO amazon_product_links VALUES(1,1,1,'linked')")
        for query in ("012345678905", "Useful widget", "WM1", "WM-SKU-ONE", "AMZ-SKU-ONE", "B000TEST", "1"):
            with self.subTest(query=query):
                rows = walmart_candidate_products(self.db, "all", query)
                self.assertEqual(rows[0]["product_id"], 1)

    def test_draft_route_reports_missing_schema_instead_of_silently_failing(self):
        self.db.execute("DROP TRIGGER marketplace_publish_events_immutable_update")
        self.db.execute("DROP TRIGGER marketplace_publish_events_immutable_delete")
        self.db.execute("DROP TABLE marketplace_publish_events")
        self.db.execute("DROP TABLE marketplace_publish_queue")
        self.db.commit()
        app = FastAPI()
        templates = Jinja2Templates(directory=Path(__file__).parents[1] / "app" / "templates")
        install_marketplace_publish(app, templates)
        with patch("app.marketplace_publish.connect", side_effect=self.route_connection):
            with TestClient(app) as client:
                response = client.post("/channels/publish/draft", data={
                    "product_id": 1, "channel": "walmart", "seller_sku": "WM-1",
                    "proposed_price": "12.50", "proposed_quantity": "1", "selected_image_id": "1",
                })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Draft saving unavailable", response.text)
        self.assertIn("Publish Center setup required", response.text)

    def test_real_submission_route_remains_fail_closed(self):
        app = FastAPI()
        templates = Jinja2Templates(directory=Path(__file__).parents[1] / "app" / "templates")
        install_marketplace_publish(app, templates)
        with TestClient(app) as client:
            response = client.post("/channels/publish/1/submit")
        self.assertEqual(response.status_code, 503)
        self.assertIn("submissions are disabled", response.json()["detail"])

    def test_opportunities_require_match_stock_and_saved_price(self):
        rows = walmart_opportunities(self.db)
        self.assertEqual([row["product_id"] for row in rows], [1])
        self.db.execute("UPDATE inventory SET quantity_reserved=quantity_on_hand")
        row = walmart_opportunities(self.db)[0]
        self.assertFalse(row["confirmed_stock"])
        self.assertEqual(row["stock_status"], "Stock not confirmed")
        self.assertIsNone(row["total_opportunity"])
        self.assertEqual(walmart_opportunities(self.db, opportunity_filter="in_stock"), [])
        self.assertEqual(
            walmart_opportunities(self.db, opportunity_filter="inventory_hunt")[0]["product_id"], 1,
        )

    def test_zero_quantity_is_inventory_hunt_not_known_out_of_stock(self):
        self.db.execute("UPDATE inventory SET quantity_on_hand=0,quantity_reserved=0")
        self.draft(price="29.88", quantity="0", weight="1", shipping="6", fee_rate="10")
        row = walmart_opportunities(self.db)[0]
        self.assertIn("stock not confirmed", row["opportunity_label"].lower())
        self.assertNotIn("out of stock", row["opportunity_label"].lower())
        self.assertEqual(row["available_quantity"], 0)
        self.assertIsNone(row["total_opportunity"])
        self.assertFalse(row["location_known"])

    def test_inventory_hunt_defaults_to_highest_walmart_selling_price(self):
        self.db.execute("UPDATE inventory SET quantity_on_hand=0,quantity_reserved=0 WHERE product_id=1")
        self.db.execute("UPDATE walmart_catalog_matches SET price_amount=5 WHERE match_id=1")
        self.add_walmart_opportunity(2, 100, quantity=0)
        rows = walmart_opportunities(self.db, opportunity_filter="inventory_hunt")
        self.assertEqual([row["product_id"] for row in rows[:2]], [2, 1])
        data = publish_page_data(self.db, opportunity_filter="inventory_hunt")
        self.assertEqual(data["opportunity_sort"], "price")

    def test_inventory_hunt_template_explains_physical_workflow_and_sort_options(self):
        app = FastAPI()
        templates = Jinja2Templates(directory=Path(__file__).parents[1] / "app" / "templates")
        install_marketplace_publish(app, templates)
        self.db.execute("UPDATE inventory SET quantity_on_hand=0,quantity_reserved=0")
        self.db.commit()
        with patch("app.marketplace_publish.connect", side_effect=self.route_connection):
            with TestClient(app) as client:
                response = client.get(
                    "/channels/publish?candidate_filter=walmart_eligible&opportunity_filter=inventory_hunt"
                )
        self.assertEqual(response.status_code, 200)
        for expected in (
            "Highest Walmart selling price", "Highest estimated profit per found unit",
            "Highest confirmed total opportunity", "Highest margin",
            "Walmart price: $7.99", "BrooksHouse qty: 0 — Stock not confirmed",
            "Location: Not located", "Action:", "Find / Scan Item", "Count / Assign Location",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, response.text)

    def test_opportunity_economics_are_before_fee_until_verified(self):
        row = self.draft(price="7.99", quantity="2", weight="1", shipping="6", fee_rate="")
        self.assertEqual(row["status"], "READY")
        opportunity = walmart_opportunities(self.db)[0]
        self.assertIsNone(opportunity["profit"])
        self.assertEqual(str(opportunity["before_fee_profit"]), "-2.01")
        self.assertIsNone(opportunity["total_opportunity"])
        self.draft(price="7.99", quantity="2", weight="1", shipping="6", fee_rate="10")
        opportunity = walmart_opportunities(self.db)[0]
        self.assertEqual(str(opportunity["profit"]), "-2.81")
        self.assertEqual(str(opportunity["total_opportunity"]), "-39.34")

    def test_opportunity_location_breakdown_does_not_change_inventory(self):
        before = list(self.db.execute("SELECT * FROM inventory"))
        self.db.execute("ALTER TABLE inventory ADD COLUMN location_id INTEGER")
        self.db.execute("ALTER TABLE inventory ADD COLUMN container_id TEXT")
        self.db.execute("UPDATE inventory SET location_id=7,container_id='BIN-A'")
        self.db.execute("CREATE TABLE inventory_locations(location_id INTEGER PRIMARY KEY,location_name TEXT)")
        self.db.execute("INSERT INTO inventory_locations VALUES(7,'Storage Yard')")
        self.db.execute("CREATE TABLE product_pick_slots(product_id INTEGER,location_id INTEGER,container_id TEXT)")
        self.db.execute("INSERT INTO product_pick_slots VALUES(1,7,'PICK-1')")
        location = walmart_opportunities(self.db)[0]["locations"][0]
        self.assertEqual((location["location_name"], location["container_label"], location["pick_slot"]),
                         ("Storage Yard", "BIN-A", "PICK-1"))
        self.assertEqual(location["available_quantity"], 14)
        opportunity = walmart_opportunities(self.db)[0]
        self.assertEqual(opportunity["location_precision"], "precise")
        self.assertFalse(opportunity["needs_locating"])
        after_values = [(row["inventory_id"], row["product_id"], row["quantity_on_hand"], row["quantity_reserved"])
                        for row in self.db.execute("SELECT * FROM inventory")]
        before_values = [(row["inventory_id"], row["product_id"], row["quantity_on_hand"], row["quantity_reserved"])
                         for row in before]
        self.assertEqual(after_values, before_values)

    def test_confirmed_stock_without_mapping_remains_ranked_and_needs_locating(self):
        self.db.execute("UPDATE walmart_catalog_matches SET price_amount=29.88")
        self.draft(price="29.88", quantity="2", weight="1", shipping="6", fee_rate="10")
        row = walmart_opportunities(self.db)[0]
        self.assertTrue(row["confirmed_stock"])
        self.assertEqual(row["location_precision"], "unmapped")
        self.assertEqual(row["location_status"], "Location unknown — needs location scan")
        self.assertTrue(row["needs_locating"])
        self.assertEqual(row["opportunity_label"], "Strong Walmart opportunity — needs locating")
        self.assertEqual(walmart_opportunities(self.db, opportunity_filter="needs_locating")[0]["product_id"], 1)
        self.assertEqual(walmart_opportunities(self.db, opportunity_filter="location_known"), [])

    def test_general_location_is_distinct_from_precise_location(self):
        self.db.execute("ALTER TABLE inventory ADD COLUMN location_id INTEGER")
        self.db.execute("ALTER TABLE inventory ADD COLUMN container_id TEXT")
        self.db.execute("UPDATE inventory SET location_id=8,container_id=NULL")
        self.db.execute("CREATE TABLE inventory_locations(location_id INTEGER PRIMARY KEY,location_name TEXT)")
        self.db.execute("INSERT INTO inventory_locations VALUES(8,'Reserve Trailer Area')")
        row = walmart_opportunities(self.db)[0]
        self.assertEqual(row["location_precision"], "general")
        self.assertTrue(row["location_known"])
        self.assertTrue(row["needs_locating"])
        self.assertIn("General location known", row["location_status"])


if __name__ == "__main__":
    unittest.main()
