import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.services.channel_inventory_production_review import run_production_review
from tests.test_channel_inventory_engine import SCHEMA


class ProductionReviewTests(unittest.TestCase):
    def test_review_runs_without_engine_tables_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/"production-like.db"
            with closing(sqlite3.connect(db)) as connection:
                connection.executescript(SCHEMA)
                connection.execute("ALTER TABLE shopify_sales_orders ADD COLUMN financial_status TEXT")
                connection.execute("ALTER TABLE shopify_sales_lines ADD COLUMN barcode TEXT")
                connection.execute("INSERT INTO products VALUES(10,'Widget',1,1)")
                connection.executemany("INSERT INTO inventory_locations VALUES(?,?,?,1)",[(1,"BrooksHouse Storefront","store"),(2,"Store Back Room","storage")])
                connection.executemany("INSERT INTO inventory VALUES(?,?,?,?,?,?,?,?)",[(1,10,1,"F",1,0,0,"x"),(2,10,2,"B",2,0,0,"x")])
                connection.execute("""INSERT INTO shopify_sales_orders
                    (shopify_order_id,processed_at,test_order,cancelled_at,fulfillment_status,financial_status)
                    VALUES('O1','2026-08-20',0,NULL,'unfulfilled','paid')""")
                connection.execute("""INSERT INTO shopify_sales_lines
                    (shopify_line_id,shopify_order_id,product_id,quantity,current_quantity,sku,title,updated_at,match_status,barcode)
                    VALUES('L1','O1',10,3,3,'SKU','Widget','v1','matched','123')""")
                connection.commit()
            before = db.read_bytes()
            report = run_production_review(db,"2026-01-01")
            self.assertEqual(before,db.read_bytes())
            self.assertFalse(report["controls_confirmation"]["infrastructure_installed"])
            self.assertEqual(report["controls_confirmation"]["effective_state"],"disabled_uninstalled")
            self.assertTrue(report["zero_mutation_verified"])
            self.assertTrue(report["prioritized_review_queue"][0]["current_open_candidate"])
            self.assertEqual(report["prioritized_review_queue"][0]["affected_lines_for_sku"],1)


if __name__ == "__main__":
    unittest.main()
