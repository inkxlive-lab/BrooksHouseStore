import unittest
from decimal import Decimal

from app.services.smart_scan_engine import (
    IDENTIFIED_EXTERNAL, MATCHED_CATALOG, PARTIAL_IDENTIFICATION, UNKNOWN,
    build_scan_result, calculate_deal_economics, resolve_acquisition_cost, variants_match,
)
from app.services.smart_scan_updates import approved_product_update_values


class DealScannerEngineTests(unittest.TestCase):
    def test_identification_statuses_and_evidence(self):
        self.assertEqual(build_scan_result("1", catalog={"product_id": 4, "product_name": "Known", "match_method": "exact"})["status"], MATCHED_CATALOG)
        unknown_upc = build_scan_result("012345678905")
        self.assertEqual(unknown_upc["status"], UNKNOWN)
        self.assertIsNone(unknown_upc["catalog_product_id"])
        self.assertEqual(build_scan_result("1", external={"title": "External"})["status"], IDENTIFIED_EXTERNAL)
        partial = build_scan_result("", ocr_text="MODEL ZX-42", identifiers={"model_number": "ZX-42"})
        self.assertEqual(partial["status"], PARTIAL_IDENTIFICATION)
        self.assertIn("ocr", partial["identification_sources"])
        self.assertEqual(build_scan_result("")["status"], UNKNOWN)

    def test_bin_price_and_size_count_safeguards(self):
        self.assertEqual(resolve_acquisition_cost("bin", "8", "99"), Decimal("8"))
        economics = calculate_deal_economics(mode="bin", bin_price="8", candidate_resale_price="20", quantity=3)
        self.assertEqual(economics["potential_total_profit"], "36")
        self.assertFalse(variants_match({"size": "12 oz"}, {"size": "24 oz"}))
        self.assertFalse(variants_match({"quantity_or_pack_count": "2"}, {"quantity_or_pack_count": "4"}))

    def test_existing_smart_scan_update_contract_remains_unchanged(self):
        self.assertEqual(
            approved_product_update_values("Title", "Description", "Brand", "Category"),
            ("Title", "Description", "Brand", "Category"),
        )


if __name__ == "__main__":
    unittest.main()
