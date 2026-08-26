import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from app import marketplace_order_service
from app.services import dashboard_financials


BASELINE = Path("codex-backups/dashboard-marketplace-repair-20260824-195734/brookshouse_store-baseline.db")


class DashboardMarketplaceRepairTests(unittest.TestCase):
    def test_changed_templates_parse_as_utf8(self):
        environment = Environment(loader=FileSystemLoader("app/templates"))
        for name in ("dashboard.html", "marketplace_orders.html", "marketplace_pull_guide.html"):
            source = Path("app/templates", name).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"[ÃÂ]")
            environment.parse(source)

    def test_more_menu_has_viewport_scroll_and_close_controls(self):
        source = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
        for marker in (
            "overflow-y: scroll", "overflow-x: hidden", "overscroll-behavior: contain",
            "touch-action: pan-y", "scrollbar-gutter: stable", "window.innerHeight",
            'event.key === "Escape"', 'document.addEventListener("pointerdown"',
            "grid-template-columns: 1fr", 'tabindex="0"',
        ):
            self.assertIn(marker, source)
        self.assertEqual(source.count('href="/inventory/container-to-shelf"'), 1)

    def test_storefront_retail_uses_store_price_on_database_copy(self):
        with patch.object(dashboard_financials, "DATABASE_PATH", BASELINE.resolve()), patch.object(
            dashboard_financials, "require_application_database_match", return_value=BASELINE.resolve()
        ):
            summary = dashboard_financials.build_financial_summary()
            locations = dashboard_financials.build_location_financial_summary()
        self.assertIsNone(summary["error"])
        self.assertEqual(summary["retail_value"], 6256.46)
        self.assertEqual(summary["missing_price_count"], 0)
        storefront = next(row for row in locations["locations"] if row["location_name"] == "BrooksHouse Storefront")
        self.assertEqual(storefront["retail_value"], 6256.46)

    def test_stale_open_amazon_orders_remain_visible_as_exceptions(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            copied = Path(folder, "orders.db")
            shutil.copy2(BASELINE, copied)
            orders = marketplace_order_service.load_marketplace_orders(copied, allow_fixture=True)
        amazon_open = [order for order in orders if order["channel_key"] == "amazon" and not order["is_terminal"]]
        self.assertEqual(len(amazon_open), 2)
        self.assertTrue(all(order["is_actionable"] for order in amazon_open))
        self.assertTrue(all(order["workflow_state"] in {"ready_to_pick", "exception", "mapping_required"} for order in amazon_open))


if __name__ == "__main__":
    unittest.main()
