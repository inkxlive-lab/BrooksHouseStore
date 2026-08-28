import unittest
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.screen_directory import install_screen_directory
from app.screen_registry import SCREEN_REGISTRY, directory_context, validate_registry


class ScreenDirectoryTests(unittest.TestCase):
    ACTIVE_ROUTE_MODULES = (
        "app/main.py", "app/access_control.py", "app/kids_helper.py",
        "app/channel_performance.py", "app/channel_inventory_admin.py",
        "app/marketplace_publish.py", "app/operations_reports.py",
        "app/product_enrichment.py", "app/product_matching_queue.py",
        "app/shopify_operations.py", "app/store_map.py",
        "app/screen_directory.py", "app/services/image_studio.py",
        "app/services/offline_mode.py", "app/services/sales_dashboard.py",
        "app/services/shopify_cost_rules.py", "app/services/inventory_activity.py",
    )

    def test_registry_routes_are_unique_and_have_metadata(self):
        routes = [screen.route for screen in SCREEN_REGISTRY]
        self.assertEqual(len(routes), len(set(routes)))
        for screen in SCREEN_REGISTRY:
            self.assertTrue(screen.name)
            self.assertTrue(screen.route.startswith("/"))
            self.assertTrue(screen.description)
            self.assertTrue(screen.category)
            self.assertTrue(screen.navigation)

    def test_route_validation_reports_only_missing_routes(self):
        paths = [screen.route for screen in SCREEN_REGISTRY if screen.route != "/scan"]
        validation = validate_registry(paths)
        self.assertEqual(validation["missing_routes"], ["/scan"])

    def test_active_route_sources_cover_registry(self):
        routes = set()
        pattern = re.compile(r'@(app|router)\.get\(\s*["\']([^"\']*)["\']')
        for file_name in self.ACTIVE_ROUTE_MODULES:
            source = Path(file_name).read_text(encoding="utf-8")
            prefix_match = re.search(r'APIRouter\(prefix=["\']([^"\']+)["\']', source)
            prefix = prefix_match.group(1) if prefix_match else ""
            for owner, route in pattern.findall(source):
                routes.add((prefix if owner == "router" else "") + route)
        self.assertEqual(validate_registry(routes)["missing_routes"], [])

    def test_counts_are_registry_derived(self):
        context = directory_context(screen.route for screen in SCREEN_REGISTRY)
        self.assertEqual(context["counts"]["total"], len(SCREEN_REGISTRY))
        self.assertEqual(context["counts"]["missing_routes"], 0)
        self.assertEqual(context["counts"]["unlinked"], sum("Unlinked" in screen.badges for screen in SCREEN_REGISTRY))

    def test_template_parses_and_contains_filters(self):
        template = Environment(loader=FileSystemLoader("app/templates")).get_template("screen_directory.html")
        self.assertIsNotNone(template)
        source = Path("app/templates/screen_directory.html").read_text(encoding="utf-8")
        self.assertIn('id="screen-search"', source)
        self.assertIn('id="category-filter"', source)
        self.assertIn('id="unlinked-filter"', source)

    def test_registered_templates_exist(self):
        missing = [screen.template for screen in SCREEN_REGISTRY
                   if screen.template and not (Path("app/templates") / screen.template).is_file()]
        self.assertEqual(missing, [])

    def test_directory_route_renders_without_database_access(self):
        app = FastAPI()
        install_screen_directory(app, Jinja2Templates(directory="app/templates"))
        response = TestClient(app).get("/screen-directory")
        self.assertEqual(response.status_code, 200)
        self.assertIn("BrooksHouse Screen Directory", response.text)
        self.assertIn("Show Unlinked Screens only", response.text)


if __name__ == "__main__":
    unittest.main()
