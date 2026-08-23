import sqlite3
import tempfile
import unittest
import sys
import types
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from amazon_order_history_sync import sync_recent_orders
from app.services.marketplace_order_ingestion import (
    alert_counts, deliver_catchup_summary, deliver_pending_pushes, mark_alert_reviewed,
    prepare_initial_catchup_summary, sync_health,
)
from app.walmart_order_service import sync_orders
from app.services.web_push_notifications import ensure_push_tables, send_notification


OPERATIONS_SCHEMA = """
CREATE TABLE operations_work_queue(
 task_id INTEGER PRIMARY KEY AUTOINCREMENT,task_key TEXT UNIQUE,task_type TEXT,title TEXT,
 details TEXT,priority TEXT,status TEXT,source_channel TEXT,source_reference TEXT,
 product_id INTEGER,requested_quantity INTEGER,created_at TEXT,updated_at TEXT);
CREATE TABLE inventory(inventory_id INTEGER PRIMARY KEY,quantity_on_hand INTEGER,quantity_reserved INTEGER);
CREATE TABLE products(product_id INTEGER PRIMARY KEY,product_name TEXT);
CREATE TABLE amazon_listings(amazon_listing_id INTEGER PRIMARY KEY,seller_sku TEXT,asin TEXT);
CREATE TABLE amazon_product_links(amazon_listing_id INTEGER,product_id INTEGER,match_status TEXT);
"""


def walmart_payload(status="Created"):
    return {"orders": [{
        "purchaseOrderId": "W1", "customerOrderId": "C1", "orderDate": "2026-08-22T12:00:00Z",
        "shippingInfo": {"estimatedShipDate": "2026-08-24T12:00:00Z"},
        "orderLines": {"orderLine": [{"lineNumber": "1", "item": {"sku": "SKU-W", "productName": "Widget"},
            "orderLineQuantity": {"amount": "2"},
            "orderLineStatuses": {"orderLineStatus": [{"status": status}]}}]},
    }]}


class FakeAmazonClient:
    def __init__(self, status="UNSHIPPED", fulfilled_by="MERCHANT"):
        self.status = status
        self.fulfilled_by = fulfilled_by

    def search_orders(self, **kwargs):
        return {"orders": [{"orderId": "A1", "createdTime": "2026-08-22T12:00:00Z",
            "lastUpdatedTime": "2026-08-22T12:01:00Z",
            "fulfillment": {"fulfillmentStatus": self.status, "fulfilledBy": self.fulfilled_by},
            "orderItems": [{"orderItemId": "I1", "sellerSku": "SKU-A", "asin": "ASIN1",
                            "title": "Amazon Widget", "quantityOrdered": 1}]}]}


class MarketplaceOrderIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "fixture.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(OPERATIONS_SCHEMA)
            connection.execute("INSERT INTO inventory VALUES(1,11,2)")

    def tearDown(self):
        self.temp.cleanup()

    def inventory(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT quantity_on_hand,quantity_reserved FROM inventory").fetchall()

    def test_walmart_new_duplicate_update_alert_task_and_no_inventory_mutation(self):
        before = self.inventory()
        first = sync_orders(3, database=self.db, allow_fixture=True, detailed=True,
                            request_function=lambda *a, **k: walmart_payload())
        second = sync_orders(3, database=self.db, allow_fixture=True, detailed=True,
                             request_function=lambda *a, **k: walmart_payload("Acknowledged"))
        with closing(sqlite3.connect(self.db)) as connection:
            alerts = connection.execute("SELECT COUNT(*),MAX(marketplace_status) FROM marketplace_order_alerts").fetchone()
            tasks = connection.execute("SELECT COUNT(*) FROM operations_work_queue").fetchone()[0]
        self.assertEqual((first["new_orders_inserted"], second["new_orders_inserted"]), (1, 0))
        self.assertEqual((alerts[0], tasks), (1, 1))
        self.assertEqual(self.inventory(), before)

    def test_amazon_new_duplicate_alert_task_and_no_inventory_mutation(self):
        before = self.inventory()
        first = sync_recent_orders(database=self.db, allow_fixture=True, client=FakeAmazonClient(), marketplace_id="ATV")
        second = sync_recent_orders(database=self.db, allow_fixture=True, client=FakeAmazonClient(), marketplace_id="ATV")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM marketplace_order_alerts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM operations_work_queue").fetchone()[0], 1)
        self.assertEqual((first["new_orders_inserted"], second["new_orders_inserted"]), (1, 0))
        self.assertEqual(self.inventory(), before)

    def test_dashboard_counts_review_and_push_once(self):
        sync_orders(3, database=self.db, allow_fixture=True,
                    request_function=lambda *a, **k: walmart_payload())
        sent = []
        sender = lambda *args: sent.append(args) or {"delivered": 1, "failed": 0}
        self.assertEqual(deliver_pending_pushes(sender, self.db, allow_fixture=True), 1)
        self.assertEqual(deliver_pending_pushes(sender, self.db, allow_fixture=True), 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(alert_counts(self.db, allow_fixture=True)["total"], 1)
        with closing(sqlite3.connect(self.db)) as connection:
            alert_id = connection.execute("SELECT alert_id FROM marketplace_order_alerts").fetchone()[0]
        self.assertTrue(mark_alert_reviewed(alert_id, "Owner", self.db, allow_fixture=True))
        self.assertEqual(alert_counts(self.db, allow_fixture=True)["total"], 0)

    def test_sync_failure_is_persisted_and_health_becomes_stale(self):
        with self.assertRaisesRegex(RuntimeError, "API down"):
            sync_orders(3, database=self.db, allow_fixture=True,
                        request_function=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API down")))
        health = sync_health(self.db, allow_fixture=True, stale_after_minutes=0)
        self.assertTrue(health["channels"]["walmart"]["stale"])
        self.assertIn("API down", health["channels"]["walmart"]["last_failure"]["error_message"])

    def test_closed_or_marketplace_fulfilled_orders_do_not_alert_or_create_pick(self):
        sync_orders(3, database=self.db, allow_fixture=True,
                    request_function=lambda *a, **k: walmart_payload("Shipped"))
        sync_recent_orders(database=self.db, allow_fixture=True,
                           client=FakeAmazonClient("UNSHIPPED", "AMAZON"), marketplace_id="ATV")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM marketplace_order_alerts").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM operations_work_queue").fetchone()[0], 0)

    def test_expired_push_subscription_is_deactivated(self):
        ensure_push_tables(self.db, allow_fixture=True)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("""INSERT INTO web_push_subscriptions
                (endpoint,p256dh,auth,device_name,active,created_at)
                VALUES('https://push.invalid','key','auth','phone',1,'2026-08-22T00:00:00Z')""")
            connection.commit()

        class Gone(Exception):
            def __init__(self):
                super().__init__("gone")
                self.response = types.SimpleNamespace(status_code=410)

        fake = types.SimpleNamespace(WebPushException=Gone, webpush=lambda **kwargs: (_ for _ in ()).throw(Gone()))
        with patch.dict(sys.modules, {"pywebpush": fake}), \
             patch("app.services.web_push_notifications.ensure_vapid_keys", return_value={"public_key": "x"}):
            result = send_notification("title", "body", database=self.db, allow_fixture=True)
        with closing(sqlite3.connect(self.db)) as connection:
            active = connection.execute("SELECT active FROM web_push_subscriptions").fetchone()[0]
        self.assertEqual((result["failed"], active), (1, 0))

    def test_initial_catchup_uses_one_summary_push(self):
        sync_orders(3, database=self.db, allow_fixture=True,
                    request_function=lambda *a, **k: walmart_payload())
        sync_recent_orders(database=self.db, allow_fixture=True,
                           client=FakeAmazonClient(), marketplace_id="ATV")
        self.assertEqual(prepare_initial_catchup_summary(self.db, allow_fixture=True), 2)
        sent = []
        sender = lambda *args: sent.append(args) or {"delivered": 1, "failed": 0}
        self.assertEqual(deliver_pending_pushes(sender, self.db, allow_fixture=True), 0)
        self.assertEqual(deliver_catchup_summary(sender, self.db, allow_fixture=True), 2)
        self.assertEqual(len(sent), 1)
        self.assertIn("2 marketplace orders need attention", sent[0][0])


if __name__ == "__main__":
    unittest.main()
