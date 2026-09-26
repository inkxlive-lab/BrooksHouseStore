import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.deal_scanner import DealScanRequest, _scan_result
from app.services.smart_scan_engine import build_scan_result


class DealScannerM21SafetyTests(unittest.TestCase):
    def test_unknown_scan_does_not_create_product_or_inventory(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE products (product_id INTEGER PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE inventory (inventory_id INTEGER PRIMARY KEY)"))
        with Session(engine) as database:
            unknown = build_scan_result("078742014616")
            unknown["external"] = None
            with patch("app.deal_scanner.identify_barcode", return_value=unknown):
                _scan_result(DealScanRequest(raw_value="078742014616", mode="personal"), database)
            self.assertFalse(database.new)
            self.assertEqual(database.scalar(text("SELECT COUNT(*) FROM products")), 0)
            self.assertEqual(database.scalar(text("SELECT COUNT(*) FROM inventory")), 0)
        engine.dispose()

    def test_zebra_manual_barcode_contract_remains_present(self):
        with open("app/templates/deal_scanner.html", encoding="utf-8") as handle:
            template = handle.read()
        self.assertIn('id="rawValue"', template)
        self.assertIn("scan_type:'barcode'", template)
        self.assertIn("TYPE / PASTE BARCODE", template)
        self.assertIn("BarcodeDetector", template)


if __name__ == "__main__":
    unittest.main()
