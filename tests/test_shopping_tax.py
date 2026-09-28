import os
import unittest
from unittest.mock import patch

from app.services.shopping_tax import resolve_tax


class ShoppingTaxTests(unittest.TestCase):
    def test_manual_override_is_percentage_not_fraction(self):
        result = resolve_tax({}, manual_rate="9.75")
        self.assertEqual(result["rate"], "9.75")
        self.assertEqual(result["status"], "MANUAL OVERRIDE")

    def test_configured_zip_rate_is_transparent(self):
        with patch.dict(os.environ, {"BROOKSHOUSE_TAX_RATES_JSON": '{"38671": 9.75}'}, clear=False):
            result = resolve_tax({"postal_code": "38671"})
        self.assertEqual(result["rate"], "9.75")
        self.assertEqual(result["method"], "CONFIGURED_JURISDICTION_ESTIMATE")

    def test_unconfigured_location_does_not_fabricate_rate(self):
        with patch.dict(os.environ, {"BROOKSHOUSE_TAX_RATES_JSON": ""}, clear=False):
            result = resolve_tax({"postal_code": "00000"})
        self.assertEqual(result["rate"], "0")
        self.assertEqual(result["status"], "LOCATION RATE NEEDED")
        self.assertEqual(result["source"], "unavailable")


if __name__ == "__main__":
    unittest.main()
