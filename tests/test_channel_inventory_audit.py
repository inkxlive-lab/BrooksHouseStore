import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.services.channel_inventory_audit import reconcile_copy
from app.services.channel_inventory_engine import apply_sale_to_copy, cancel_before_fulfillment_to_copy
from tests.test_channel_inventory_engine import make_copy


class ChannelInventoryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = make_copy(Path(self.temp.name),quantity=2,storefront=5)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_trace_reconciles_sale_and_cancellation(self):
        apply_sale_to_copy(self.db,"shopify","O1","L1")
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE shopify_sales_orders SET cancelled_at='c1'")
            connection.commit()
        cancel_before_fulfillment_to_copy(self.db,"shopify","O1","L1")
        report = reconcile_copy(self.db)
        row = report["rows"][0]
        self.assertEqual(report["mismatch_count"],0)
        self.assertEqual((row["marketplace"],row["order_id"],row["order_line_id"]),("shopify","O1","L1"))
        self.assertEqual((row["deducted_quantity"],row["restored_quantity"],row["current_outstanding_allocation"]),(2,2,0))
        self.assertEqual(row["physical_inventory_rows_used"],[1])
        self.assertEqual(len(row["inventory_transaction_ids"]),2)

    def test_corrupt_link_is_flagged(self):
        apply_sale_to_copy(self.db,"shopify","O1","L1")
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE channel_inventory_event_transactions SET quantity_change=-1")
            connection.commit()
        report = reconcile_copy(self.db)
        self.assertEqual(report["mismatch_count"],1)
        self.assertIn("linked_transaction_quantity_mismatch",report["rows"][0]["mismatch_error_state"])


if __name__ == "__main__":
    unittest.main()
