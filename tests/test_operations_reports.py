import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from amazon_order_history_sync import AmazonClient, AmazonRateLimitError, _AMAZON_CIRCUIT
from app.operations_reports import (_run_report_job, create_report_snapshot, enqueue_report_job, load_report_job,
                                   load_snapshot, parse_marketplace_datetime, recover_stale_report_jobs)
from app.services.marketplace_order_ingestion import (acquire_operation_lock, ensure_marketplace_operations_schema,
                                                      reconcile_order_status, release_operation_lock, run_sync_cycle)
from app.walmart_order_service import is_acknowledgment_reconciliation_signal


def line(product_id=10, *, quantity=3, picked=0, available=2, mapped=True):
    return {"product_id": product_id if mapped else None, "mapped_product_id": product_id if mapped else None,
            "confirmed_product_id": product_id if mapped else None, "item_name": "Widget", "sku": "SKU1",
            "product_barcode": "0123", "quantity": quantity, "pulled_quantity": picked,
            "inventory_options": ([{"site_name": "Storefront", "location_name": "A1", "container_id": "TOTE-1",
                                    "available_quantity": available}] if available is not None else [])}


def order(order_id="W1", *, ship_by="2099-01-01T12:00:00Z", stage="new", terminal=False, order_line=None):
    return {"channel": "Walmart", "channel_key": "walmart", "purchase_order_id": order_id,
            "is_actionable": not terminal, "is_terminal": terminal, "verification_stale": False,
            "local_status": stage, "marketplace_status": "Shipped" if terminal else "Created",
            "walmart_status": "Shipped" if terminal else "Created", "unit_count": int((order_line or line())["quantity"]),
            "order_date": "2026-08-23T12:00:00Z", "ship_by_date": ship_by,
            "lines": [order_line or line()], "site_names": ["Storefront"], "inventory_state": "ready", "is_overdue": False}


class OperationsReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "copy.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript("""
            CREATE TABLE walmart_orders(purchase_order_id TEXT PRIMARY KEY,walmart_status TEXT,local_status TEXT NOT NULL DEFAULT 'new');
            CREATE TABLE amazon_order_history(amazon_order_id TEXT PRIMARY KEY,fulfillment_status TEXT,local_status TEXT NOT NULL DEFAULT 'new');
            CREATE TABLE operations_work_queue(task_id INTEGER PRIMARY KEY,task_key TEXT UNIQUE,status TEXT,source_channel TEXT,source_reference TEXT,updated_at TEXT);
            """)
            ensure_marketplace_operations_schema(connection)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, report_type, orders, filters=None, today=date(2026, 8, 24), warnings=None):
        filters = filters or {"channel": "all", "include_staged": True}
        with patch("app.operations_reports.load_marketplace_orders", return_value=orders):
            run_id = create_report_snapshot(report_type=report_type, filters=filters,
                freshness={"channels": {}}, warnings=warnings or [], database=self.db,
                allow_fixture=True, today_central=today)
        return load_snapshot(run_id, self.db, allow_fixture=True)[1]

    def test_epoch_seconds_milliseconds_and_iso_parse_to_same_instant(self):
        expected = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)
        seconds = int(expected.timestamp())
        self.assertEqual(parse_marketplace_datetime(seconds), expected)
        self.assertEqual(parse_marketplace_datetime(seconds * 1000), expected)
        self.assertEqual(parse_marketplace_datetime("2026-08-24T15:00:00Z"), expected)
        self.assertIsNone(parse_marketplace_datetime("malformed"))

    def test_operations_report_templates_parse(self):
        environment = Environment(loader=FileSystemLoader("app/templates"))
        for name in ("operations_reports.html", "operations_report_job.html", "operations_report_snapshot.html"):
            environment.get_template(name)

    def test_due_today_uses_entire_central_day_and_excludes_tomorrow_and_terminal(self):
        # 05:00 UTC is midnight Central during daylight time; 04:59:59 next day is 11:59:59 PM Central.
        seconds = int(datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc).timestamp())
        orders = [order("SECONDS", ship_by=str(seconds)), order("MILLIS", ship_by=str((seconds + 86399) * 1000)),
                  order("ISO", ship_by="2026-08-24T18:30:00Z"), order("TOMORROW", ship_by="2026-08-25T05:00:00Z"),
                  order("SHIPPED", ship_by="2026-08-24T16:00:00Z", terminal=True)]
        snapshot = self.snapshot("due_today", orders)
        self.assertEqual({row["order_id"] for row in snapshot["order_rows"]}, {"SECONDS", "MILLIS", "ISO"})

    def test_include_staged_and_terminal_semantics(self):
        orders = [order("NEW"), order("PACKED", stage="packed"), order("STAGED", stage="staged"), order("DONE", terminal=True)]
        included = self.snapshot("active", orders)
        excluded = self.snapshot("active", orders, {"channel": "all", "include_staged": False})
        staged = self.snapshot("staged", orders)
        self.assertEqual({row["order_id"] for row in included["order_rows"]}, {"NEW", "PACKED", "STAGED"})
        self.assertEqual({row["order_id"] for row in excluded["order_rows"]}, {"NEW", "PACKED"})
        self.assertEqual({row["order_id"] for row in staged["order_rows"]}, {"PACKED", "STAGED"})

    def test_master_pull_remaining_and_exact_location_quantity_not_multiplied(self):
        orders = [order("W1", order_line=line(quantity=3, picked=1, available=5)),
                  order("W2", order_line=line(quantity=2, picked=0, available=5))]
        snapshot = self.snapshot("master_pull", orders)
        row = snapshot["pull_rows"][0]
        self.assertEqual((row["units_required"], row["units_picked_staged"], row["remaining_to_pull"]), (5, 1, 4))
        self.assertEqual(row["available_units"], 5)
        self.assertEqual(row["locations"][0]["available"], 5)

    def test_fully_picked_product_is_not_on_master_pull(self):
        snapshot = self.snapshot("master_pull", [order(order_line=line(quantity=3, picked=3, available=5))])
        self.assertEqual(snapshot["pull_rows"], [])
        self.assertEqual(snapshot["totals"]["remaining_units_to_pull"], 0)

    def test_mapping_and_candidate_inventory_are_separate(self):
        candidate = line(mapped=False, available=7)
        snapshot = self.snapshot("exceptions", [order(order_line=candidate)])
        row = snapshot["order_rows"][0]
        self.assertEqual(row["mapping_status"], "unmatched_candidate")
        self.assertEqual(row["inventory_readiness"], "ready")
        self.assertEqual(row["exception"], "Unmatched — candidate inventory found")

    def test_snapshot_is_immutable_and_warning_is_preserved(self):
        warning = ["Amazon STALE AS OF 08/24/2026 08:00 AM CT"]
        snapshot = self.snapshot("active", [order()], warnings=warning)
        self.assertEqual(snapshot["warnings"], warning)
        with closing(sqlite3.connect(self.db)) as connection:
            before = connection.execute("SELECT snapshot_json,snapshot_sha256 FROM operations_report_runs").fetchone()
            connection.execute("INSERT INTO walmart_orders(purchase_order_id,walmart_status,local_status) VALUES('W9','Shipped','shipped')")
            after = connection.execute("SELECT snapshot_json,snapshot_sha256 FROM operations_report_runs").fetchone()
        self.assertEqual(before, after)

    def job_filters(self):
        return {"channel": "all", "ship_start": "", "ship_end": "", "physical_site": "all",
                "stage": "all", "include_staged": True, "exclude_channels": [], "allow_stale_channels": []}

    def test_concurrent_submissions_return_one_database_backed_job(self):
        first, created_first = enqueue_report_job(report_type="active", mode="current", filters=self.job_filters(),
                                                  actor="tester", database=self.db, allow_fixture=True, start=False)
        second, created_second = enqueue_report_job(report_type="master_pull", mode="refresh", filters=self.job_filters(),
                                                    actor="tester", database=self.db, allow_fixture=True, start=False)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first, second)

    @patch("app.operations_reports.start_report_job")
    def test_enqueue_starts_background_job_for_single_worker_responsiveness(self, start_job):
        job_id, created = enqueue_report_job(report_type="active", mode="current", filters=self.job_filters(),
                                             actor="tester", database=self.db, allow_fixture=True)
        self.assertTrue(created)
        start_job.assert_called_once_with(job_id, database=self.db, allow_fixture=True)

    def test_stale_incomplete_job_is_recovered_as_failed(self):
        job_id, _ = enqueue_report_job(report_type="active", mode="current", filters=self.job_filters(),
                                       actor="tester", database=self.db, allow_fixture=True, start=False)
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE operations_report_jobs SET state='refreshing',updated_at=? WHERE report_job_id=?", (old, job_id))
            connection.commit()
        with patch.dict("os.environ", {"OPERATIONS_REPORT_JOB_STALE_SECONDS": "60"}):
            self.assertEqual(recover_stale_report_jobs(self.db, allow_fixture=True), 1)
        self.assertEqual(load_report_job(job_id, self.db, allow_fixture=True)["state"], "failed")

    @patch("app.operations_reports.run_sync_cycle")
    @patch("app.operations_reports.load_marketplace_orders", return_value=[order()])
    @patch("app.operations_reports.sync_health")
    def test_fresh_data_generation_never_refreshes_channels(self, health, _orders, sync):
        health.return_value = {"channels": {name: {"last_success": {"finished_at": datetime.now(timezone.utc).isoformat()}}
                                            for name in ("walmart", "amazon")}}
        job_id, _ = enqueue_report_job(report_type="active", mode="current", filters=self.job_filters(),
                                       actor="tester", database=self.db, allow_fixture=True, start=False)
        _run_report_job(job_id, database=self.db, allow_fixture=True)
        job = load_report_job(job_id, self.db, allow_fixture=True)
        self.assertEqual(job["state"], "complete")
        self.assertIsNotNone(job["result_report_run_id"])
        sync.assert_not_called()

    @patch("app.operations_reports._bounded_call", side_effect=FutureTimeout())
    @patch("app.operations_reports.sync_health")
    def test_generation_timeout_records_friendly_failed_state(self, health, _bounded):
        health.return_value = {"channels": {name: {"last_success": {"finished_at": datetime.now(timezone.utc).isoformat()}}
                                            for name in ("walmart", "amazon")}}
        job_id, _ = enqueue_report_job(report_type="active", mode="current", filters=self.job_filters(),
                                       actor="tester", database=self.db, allow_fixture=True, start=False)
        _run_report_job(job_id, database=self.db, allow_fixture=True)
        job = load_report_job(job_id, self.db, allow_fixture=True)
        self.assertEqual(job["state"], "failed")
        self.assertIn("timeout", job["error_message"].casefold())

    def test_walmart_and_amazon_terminal_reconciliation_is_idempotent(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("INSERT INTO walmart_orders VALUES('W1','Created','new',NULL,NULL,NULL)")
            connection.execute("INSERT INTO amazon_order_history(amazon_order_id,fulfillment_status,local_status) VALUES('A1','Unshipped','packed')")
            connection.execute("INSERT INTO operations_work_queue VALUES(1,'marketplace-pick:walmart:W1:1','open','walmart','W1|1','old')")
            first = reconcile_order_status(connection, channel="walmart", order_id="W1", marketplace_status="Cancelled", channel_response={"status":"Cancelled"})
            second = reconcile_order_status(connection, channel="walmart", order_id="W1", marketplace_status="Cancelled", channel_response={"status":"Cancelled"})
            reconcile_order_status(connection, channel="amazon", order_id="A1", marketplace_status="Shipped", channel_response={"status":"Shipped"})
            connection.commit()
            self.assertTrue(first["changed"]); self.assertFalse(second["changed"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM marketplace_status_audit").fetchone()[0], 2)

    @patch("app.services.marketplace_order_ingestion.deliver_pending_pushes")
    @patch("app.services.marketplace_order_ingestion.deliver_catchup_summary")
    @patch("app.services.marketplace_order_ingestion._has_completed_sync_run", return_value=True)
    @patch("app.walmart_order_service.sync_orders", return_value={"success": True})
    @patch("amazon_order_history_sync.sync_recent_orders", return_value={"success": True})
    def test_channel_specific_sync_never_calls_unselected_channel(self, amazon_sync, walmart_sync, *_):
        result = run_sync_cycle(channels=["walmart"], database=self.db, allow_fixture=True)
        walmart_sync.assert_called_once(); amazon_sync.assert_not_called(); self.assertEqual(set(result), {"walmart"})

    @patch("app.walmart_order_service.sync_orders")
    @patch("amazon_order_history_sync.sync_recent_orders")
    def test_database_refresh_lock_prevents_duplicate_marketplace_calls(self, amazon_sync, walmart_sync):
        self.assertTrue(acquire_operation_lock("marketplace_refresh", "first", database=self.db, allow_fixture=True))
        try:
            result = run_sync_cycle(channels=["walmart", "amazon"], database=self.db, allow_fixture=True)
            self.assertTrue(all(item.get("busy") for item in result.values()))
            walmart_sync.assert_not_called(); amazon_sync.assert_not_called()
        finally:
            release_operation_lock("marketplace_refresh", "first", database=self.db, allow_fixture=True)

    def test_acknowledgment_400_is_reconciliation_signal(self):
        message = "Acknowledgment is not required. Purchase order lines are already in shipped or cancelled state."
        self.assertTrue(is_acknowledgment_reconciliation_signal(message))


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload or {}, headers or {}
    def json(self): return self._payload
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses): self.responses, self.calls = list(responses), 0
    def request(self, *_args, **_kwargs): self.calls += 1; return self.responses.pop(0)


class AmazonThrottleTests(unittest.TestCase):
    def setUp(self): _AMAZON_CIRCUIT.update({"failures": 0, "open_until": 0.0})

    @patch("amazon_order_history_sync.time.sleep")
    def test_429_respects_retry_after_and_succeeds_with_bounded_retry(self, sleep):
        client = AmazonClient("id", "secret", "refresh"); client.access_token = "token"; client.expires_at = datetime.max.replace(tzinfo=timezone.utc)
        client.min_request_interval = 0; client.max_retries = 2
        client.session = FakeSession([FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(200, {"orders": []})])
        self.assertEqual(client.search_orders(marketplace_id="ATV", created_after="2026-08-24T00:00:00Z"), {"orders": []})
        sleep.assert_called_with(2.0); self.assertEqual(client.session.calls, 2)

    @patch("amazon_order_history_sync.time.sleep")
    def test_429_retries_are_bounded_and_circuit_breaker_opens(self, _sleep):
        client = AmazonClient("id", "secret", "refresh"); client.access_token = "token"; client.expires_at = datetime.max.replace(tzinfo=timezone.utc)
        client.min_request_interval = 0; client.max_retries = 1; client.circuit_failures = 1
        client.session = FakeSession([FakeResponse(429), FakeResponse(429)])
        with self.assertRaises(AmazonRateLimitError): client.search_orders(marketplace_id="ATV", created_after="2026-08-24T00:00:00Z")
        self.assertGreater(_AMAZON_CIRCUIT["open_until"], 0); self.assertEqual(client.session.calls, 2)

    def test_get_order_reuses_same_run_cache(self):
        client = AmazonClient("id", "secret", "refresh"); client.access_token = "token"; client.expires_at = datetime.max.replace(tzinfo=timezone.utc)
        client.min_request_interval = 0; client.session = FakeSession([FakeResponse(200, {"orderId": "A1"})])
        self.assertEqual(client.get_order("A1"), client.get_order("A1")); self.assertEqual(client.session.calls, 1)


if __name__ == "__main__": unittest.main()
