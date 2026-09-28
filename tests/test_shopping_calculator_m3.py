import unittest
from decimal import Decimal

from app.deal_scanner import DealScanRequest, _mode_summary
from app.services.shopping_calculator import calculate_cart


class ShoppingCalculatorM3Tests(unittest.TestCase):
    def test_fourth_mode_has_calculator_summary(self):
        result = _mode_summary(
            "shopping_calculator", request=DealScanRequest(mode="shopping_calculator"),
            intelligence={}, economics={}, reference={}, decision="INSPECT_FIRST",
        )
        self.assertEqual(result["mode"], "shopping_calculator")
        self.assertEqual(result["cards"], [])

    def test_single_taxable_item(self):
        result = calculate_cart([{"name": "Milk", "unit_price": "3.98", "quantity": 1}], "9.75")
        self.assertEqual(result["subtotal"], Decimal("3.98"))
        self.assertEqual(result["taxable_subtotal"], Decimal("3.98"))
        self.assertEqual(result["estimated_tax"], Decimal("0.39"))
        self.assertEqual(result["estimated_total"], Decimal("4.37"))

    def test_mixed_taxability_quantity_and_item_discount(self):
        result = calculate_cart([
            {"name": "Soda", "unit_price": "7.98", "quantity": 2, "discount": "1.00", "taxable": True},
            {"name": "Produce", "unit_price": "4.00", "quantity": 1, "taxable": False},
        ], "10")
        self.assertEqual(result["gross_total"], Decimal("19.96"))
        self.assertEqual(result["item_discount_total"], Decimal("2.00"))
        self.assertEqual(result["taxable_subtotal"], Decimal("13.96"))
        self.assertEqual(result["non_taxable_subtotal"], Decimal("4.00"))
        self.assertEqual(result["estimated_tax"], Decimal("1.40"))
        self.assertEqual(result["estimated_total"], Decimal("19.36"))

    def test_cart_discount_is_capped_and_tax_rate_change_is_deterministic(self):
        result = calculate_cart([{"unit_price": "10.005", "quantity": 1}], "0", "50")
        self.assertEqual(result["gross_total"], Decimal("10.01"))
        self.assertEqual(result["cart_discount"], Decimal("10.01"))
        self.assertEqual(result["estimated_total"], Decimal("0.00"))

    def test_empty_cart_and_zero_tax(self):
        result = calculate_cart([], "0")
        self.assertEqual(result["item_count"], 0)
        self.assertEqual(result["estimated_tax"], Decimal("0.00"))
        self.assertEqual(result["estimated_total"], Decimal("0.00"))

    def test_template_keeps_existing_modes_and_adds_fast_calculator_workflow(self):
        with open("app/templates/deal_scanner.html", encoding="utf-8") as handle:
            template = handle.read()
        for mode in ("personal", "sourcing", "bin", "shopping_calculator"):
            self.assertIn(f'data-mode="{mode}"', template)
        for marker in ("START SHOPPING", "ADD TO CART", "ESTIMATED TOTAL", "BarcodeDetector", "TYPE / PASTE BARCODE"):
            self.assertIn(marker, template)

    def test_mobile_cart_is_collapsible_without_changing_calculation_logic(self):
        with open("app/templates/deal_scanner.html", encoding="utf-8") as handle:
            template = handle.read()
        self.assertIn('id="cartToggle"', template)
        self.assertIn('aria-controls="cartDetails"', template)
        self.assertIn("aria-expanded", template)
        self.assertIn("max-width:620px", template)
        self.assertIn("cart-details.collapsed", template)
        self.assertIn("sessionStorage", template)
        self.assertIn("cartHeaderCount", template)
        self.assertIn("cartHeaderTotal", template)
        self.assertIn("setShoppingCartExpanded(!shoppingCartExpanded)", template)
        self.assertIn("shoppingItems.length===1", template)

    def test_field_test_camera_wedge_quick_add_and_resume_contracts(self):
        with open("app/templates/deal_scanner.html", encoding="utf-8") as handle:
            template = handle.read()
        for marker in (
            "/static/vendor/zxing-browser.min.js", "decodeFromConstraints",
            "CAMERA READY", "SCANNING...", "BARCODE FOUND", "LOOKING UP...",
            "CAMERA PERMISSION REQUIRED", "CAMERA UNAVAILABLE", "TRY AGAIN",
            "e.key==='Enter'", "requestSubmit()", "scanInFlight",
            "focusScanner()", "+ QUICK ADD", "UNKNOWN ITEM", "RESUME SHOPPING",
            "brookshouse-shopping-session-active",
        ):
            self.assertIn(marker, template)


if __name__ == "__main__":
    unittest.main()
