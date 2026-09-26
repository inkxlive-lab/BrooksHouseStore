import unittest
from unittest.mock import patch

from app.deal_scanner import DealScanRequest, _scan_result
from app.services.deal_price_providers import build_price_intelligence


UPC = "079567100706"


def fixture_result(offers):
    return {
        "source": "UPCitemdb Public Page",
        "barcode": UPC,
        "title": "3-IN-ONE Multi-Purpose Oil with Marksman Spout, 4 fl oz",
        "offers": offers,
    }


class DealScannerM221RegressionTests(unittest.TestCase):
    def test_external_observations_survive_and_provider_stays_available(self):
        result = build_price_intelligence(fixture_result([
            {"merchant": "Walmart.com", "price": "4.68"},
            {"merchant": "Home Depot", "price": "5.35"},
            {"merchant": "Ace Hardware", "price": "4.99"},
            {"merchant": "True Value", "price": "3.49"},
            {"merchant": "Unknown Seller", "price": "49.36"},
        ]), identifier=UPC)

        self.assertEqual(len(result["observations"]), 5)
        self.assertEqual(result["provider_states"][0], {
            "provider": "UPCitemdb Public Page", "status": "AVAILABLE"
        })
        self.assertEqual({row["retailer"] for row in result["observations"]}, {
            "Walmart.com", "Home Depot", "Ace Hardware", "True Value", "Unknown Seller"
        })

    def test_trusted_and_weak_observations_are_separate_without_deleting_evidence(self):
        result = build_price_intelligence(fixture_result([
            {"merchant": "Walmart.com", "price": "4.68"},
            {"merchant": "Home Depot", "price": "5.35"},
            {"merchant": "Unknown Seller", "price": "49.36"},
        ]), identifier=UPC)

        trusted = [row for row in result["observations"] if row["reference_eligible"]]
        weak = [row for row in result["observations"] if not row["reference_eligible"]]
        self.assertEqual({row["retailer"] for row in trusted}, {"Walmart.com", "Home Depot"})
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["retailer"], "Unknown Seller")
        self.assertEqual(weak[0]["quality_state"], "OUTLIER")
        self.assertEqual(weak[0]["exclusion_reason"], "OUTLIER")

    def test_outlier_does_not_drive_reference_price(self):
        result = build_price_intelligence(fixture_result([
            {"merchant": "Walmart.com", "price": "4.68"},
            {"merchant": "Home Depot", "price": "5.35"},
            {"merchant": "Ace Hardware", "price": "4.99"},
            {"merchant": "True Value", "price": "3.49"},
            {"merchant": "Unknown Seller", "price": "49.36"},
        ]), identifier=UPC)

        reference = result["reference_price"]
        self.assertIn(reference["price"], {4.68, 4.99})
        self.assertIn("excluded 1", reference["explanation"].lower())
        self.assertEqual(reference["quality_state"], "TRUSTED")

    def test_only_weak_observations_remain_visible_without_reference(self):
        result = build_price_intelligence(fixture_result([
            {"merchant": "Unknown Seller", "price": "8.00"},
        ]), identifier=UPC)

        self.assertEqual(len(result["observations"]), 1)
        self.assertIsNone(result["reference_price"]["price"])
        self.assertIn("no current exact-match", result["reference_price"]["explanation"].lower())
        self.assertEqual(result["provider_states"][0]["status"], "AVAILABLE")

    def test_zero_observations_and_provider_error_remain_distinct(self):
        empty = build_price_intelligence({"source": "UPCitemdb Public Page", "offers": []}, identifier=UPC)
        error = build_price_intelligence({"source": "UPCitemdb Public Page", "error": "timeout"}, identifier=UPC)
        self.assertEqual(empty["observations"], [])
        self.assertEqual(empty["provider_states"][0]["status"], "NO_PRICE")
        self.assertEqual(error["observations"], [])
        self.assertEqual(error["provider_states"][0]["status"], "PROVIDER_ERROR")

    def test_weak_reference_evidence_cannot_return_grab(self):
        request = DealScanRequest(mode="bin", raw_value=UPC, bin_price=5)
        scan = {"status": "IDENTIFIED_EXTERNAL", "catalog_product_id": None,
                "raw_value": UPC, "scan_type": "barcode", "identification_sources": []}
        intelligence = {
            "observations": [{"price": 12.0, "reference_eligible": False}],
            "reference_price": {"price": 12.0, "quality_state": "UNVERIFIED", "confidence": 0.4},
            "provider_status": "connected", "provider_states": [], "marketplaces": [],
            "location": None,
        }
        with patch("app.deal_scanner.identify_barcode", return_value={"catalog_product_id": None, "external": {}}), \
                patch("app.deal_scanner.build_scan_result", return_value=scan), \
                patch("app.deal_scanner.build_price_intelligence", return_value=intelligence):
            result = _scan_result(request, object())

        self.assertEqual(result["decision"], "INSPECT_FIRST")


if __name__ == "__main__":
    unittest.main()
