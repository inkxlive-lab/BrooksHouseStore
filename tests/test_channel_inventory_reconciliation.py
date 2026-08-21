import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.services.channel_inventory_reconciliation import LEDGER_DDL, connect_read_only, reconcile


SCHEMA = """
CREATE TABLE products(product_id INTEGER PRIMARY KEY, product_name TEXT);
CREATE TABLE product_barcodes(barcode_id INTEGER PRIMARY KEY,product_id INTEGER,barcode TEXT);
CREATE TABLE inventory_locations(location_id INTEGER PRIMARY KEY,location_name TEXT,location_type TEXT,active INTEGER);
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,product_id INTEGER,location_id INTEGER,container_id TEXT,quantity_on_hand INTEGER,quantity_reserved INTEGER);
CREATE TABLE product_pick_slots(product_id INTEGER PRIMARY KEY,location_id INTEGER,container_id TEXT,updated_at TEXT);
CREATE TABLE shopify_sales_orders(shopify_order_id TEXT PRIMARY KEY,processed_at TEXT,test_order INTEGER,cancelled_at TEXT,financial_status TEXT,fulfillment_status TEXT,refund_amount REAL,last_imported_at TEXT);
CREATE TABLE shopify_sales_lines(shopify_line_id TEXT PRIMARY KEY,shopify_order_id TEXT,shopify_variant_id TEXT,product_id INTEGER,sku TEXT,barcode TEXT,quantity INTEGER,current_quantity INTEGER,match_status TEXT,match_method TEXT,inventory_applied INTEGER);
"""


def make_copy(tmp_path):
    source = tmp_path / "fixture-source.db"
    copied = tmp_path / "fixture-copy.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)", [(1,"BrooksHouse Storefront","store"),(2,"Store Back Room","storage"),(5,"Online Orders / Reserved","reserved"),(7,"Trailer 1","trailer")])
        connection.execute("INSERT INTO products VALUES(10,'Widget')")
        connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?)", [(1,10,5,"",20,0),(2,10,1,"PICK-A",5,1),(3,10,7,"TOTE",100,0),(4,10,2,"BACK",6,0)])
        connection.execute("INSERT INTO product_pick_slots VALUES(10,1,'PICK-A','now')")
        connection.execute("INSERT INTO shopify_sales_orders VALUES('O1','2026-08-20',0,NULL,'paid','fulfilled',0,'now')")
        connection.execute("INSERT INTO shopify_sales_lines VALUES('L1','O1','V1',10,'SKU','123',3,3,'matched','barcode',0)")
        connection.commit()
    shutil.copy2(source, copied)
    return copied


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.copied = make_copy(Path(self.temporary_directory.name))

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_storefront_is_first_and_reserved_is_not_eligible(self):
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.action, "deduct_preview")
        self.assertEqual(row.deduction_location, "BrooksHouse Storefront")
        self.assertEqual((row.quantity_before, row.quantity_after), (5, 2))
        self.assertNotIn("Online Orders / Reserved", row.eligible_quantities)
        self.assertIn("Trailer 1", row.replenishment_sources)

    def test_back_room_is_second_priority(self):
        with closing(sqlite3.connect(self.copied)) as connection:
            connection.execute("UPDATE inventory SET quantity_on_hand=2 WHERE inventory_id=2")
            connection.commit()
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.fulfillment_category, "immediately_fulfillable_back_room")
        self.assertEqual(row.deduction_location, "Store Back Room")

    def test_unfulfilled_line_stays_owed_and_proposes_replenishment(self):
        with closing(sqlite3.connect(self.copied)) as connection:
            connection.execute("UPDATE inventory SET quantity_on_hand=0 WHERE location_id IN (1,2)")
            connection.commit()
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.fulfillment_category, "reserved_owed_replenishment_available")
        self.assertEqual(row.commitment_delta_required, 3)
        self.assertEqual(row.proposed_work_item, "propose_replenishment_to_store_fulfillment")
        self.assertEqual(row.deduction_location, "")

    def test_companywide_unavailable_remains_owed(self):
        with closing(sqlite3.connect(self.copied)) as connection:
            connection.execute("UPDATE inventory SET quantity_on_hand=0 WHERE location_id IN (1,2,5)")
            connection.execute("UPDATE inventory SET quantity_on_hand=2 WHERE location_id=7")
            connection.commit()
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.fulfillment_category, "reserved_owed_unavailable_companywide")
        self.assertEqual(row.commitment_delta_required, 3)

    def test_staged_reserved_pool_requires_allocation_review(self):
        with closing(sqlite3.connect(self.copied)) as connection:
            connection.execute("UPDATE inventory SET quantity_on_hand=0 WHERE location_id IN (1,2,7)")
            connection.commit()
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.fulfillment_category, "reserved_owed_staged_pool_review")
        self.assertEqual(row.proposed_work_item, "reconcile_reserved_staging_allocation_ownership")

    def test_unique_ledger_marks_line_applied_on_copied_database(self):
        with closing(sqlite3.connect(self.copied)) as connection:
            connection.executescript(LEDGER_DDL)
            connection.execute("CREATE TABLE inventory_transactions(transaction_id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO inventory_transactions VALUES(99)")
            connection.execute("""INSERT INTO channel_inventory_ledger(channel_name,order_id,order_line_id,event_type,product_id,quantity_change,inventory_id,inventory_transaction_id,applied_at) VALUES('shopify','O1','L1','sale',10,-3,2,99,'now')""")
            connection.commit()
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
            self.assertTrue(row.already_applied)
            self.assertEqual(row.action, "review")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE inventory SET quantity_on_hand=0")

    def test_refund_is_review_not_restock(self):
        with closing(sqlite3.connect(self.copied)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET refund_amount=5 WHERE shopify_order_id='O1'")
            connection.commit()
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.lifecycle_event, "refund_or_return")
        self.assertEqual(row.action, "review")
        self.assertIn("restock confirmation", row.review_reason)


if __name__ == "__main__":
    unittest.main()
