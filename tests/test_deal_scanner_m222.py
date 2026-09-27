import unittest
from unittest.mock import Mock, patch

from app.deal_scanner import DealScanRequest, _mode_summary
from app.integrations.product_lookup import _lookup_upcitemdb
from app.services.deal_price_providers import build_price_intelligence
from app.services.smart_scan_engine import calculate_deal_economics


class DealScannerM222Tests(unittest.TestCase):
    def test_rate_limited_is_not_no_price(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "provider_status": "RATE_LIMITED",
            "error": "daily limit reached", "found": False,
        })
        self.assertEqual(result["provider_states"][0]["status"], "RATE_LIMITED")
        self.assertEqual(result["provider_status"], "RATE_LIMITED")

    def test_identity_only_is_distinct_from_no_match(self):
        identity = build_price_intelligence({
            "source": "UPCitemdb", "provider_status": "AVAILABLE_IDENTITY_ONLY",
            "found": True, "title": "Widget", "offers": [],
        })
        no_match = build_price_intelligence({
            "source": "UPCitemdb", "provider_status": "NO_MATCH", "found": False,
        })
        self.assertEqual(identity["provider_states"][0]["status"], "AVAILABLE_IDENTITY_ONLY")
        self.assertEqual(no_match["provider_states"][0]["status"], "NO_MATCH")

    def test_observation_is_preserved_while_outlier_is_excluded(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "barcode": "079567100706", "title": "Oil",
            "offers": [
                {"merchant": "Walmart.com", "price": "4.68"},
                {"merchant": "Home Depot", "price": "5.35"},
                {"merchant": "Unknown Seller", "price": "49.36"},
            ],
        })
        self.assertEqual(len(result["observations"]), 3)
        outlier = next(row for row in result["observations"] if row["price"] == 49.36)
        self.assertFalse(outlier["reference_eligible"])
        self.assertEqual(outlier["exclusion_reason"], "OUTLIER")

    def test_personal_summary_uses_entered_price_and_not_profit_cards(self):
        request = DealScanRequest(mode="personal", candidate_resale_price=5.99)
        summary = _mode_summary(
            "personal", request=request,
            intelligence={"lowest_observed_price": 3.49},
            economics={},
            reference={"price": 4.68}, decision="INSPECT_FIRST",
        )
        self.assertEqual([card["label"] for card in summary["cards"]], [
            "YOUR PRICE", "LOWEST FOUND", "REFERENCE / TYPICAL", "ABOVE REFERENCE",
        ])
        self.assertEqual(summary["difference_vs_reference"], 1.31)

    def test_missing_quantity_does_not_create_total_potential(self):
        economics = calculate_deal_economics(
            mode="sourcing", acquisition_cost=5, candidate_resale_price=10, quantity=None,
        )
        self.assertIsNone(economics["potential_total_profit"])

    @patch("app.integrations.product_lookup.requests.get")
    def test_upc_rate_limit_is_tagged_at_retrieval_boundary(self, get):
        response = Mock(status_code=429)
        get.return_value = response
        result = _lookup_upcitemdb("079567100706")
        self.assertEqual(result["provider_status"], "RATE_LIMITED")


if __name__ == "__main__":
    unittest.main()
