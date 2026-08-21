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
        connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)", [(1,"BrooksHouse Storefront","store"),(5,"Online Orders / Reserved","reserved"),(7,"Trailer 1","trailer")])
        connection.execute("INSERT INTO products VALUES(10,'Widget')")
        connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?)", [(1,10,5,"",2,0),(2,10,1,"PICK-A",5,1),(3,10,7,"TOTE",100,0)])
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

    def test_reserved_then_pick_slot_and_never_trailer(self):
        with connect_read_only(self.copied) as connection:
            row = reconcile(connection, "2026-01-01")[0]
        self.assertEqual(row.action, "deduct_preview")
        self.assertEqual(row.deduction_location, "BrooksHouse Storefront")
        self.assertEqual((row.quantity_before, row.quantity_after), (5, 2))
        self.assertNotIn("Trailer", row.eligible_quantities)

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
