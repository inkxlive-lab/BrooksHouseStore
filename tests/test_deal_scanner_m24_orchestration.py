import unittest

from app.services.deal_price_providers import build_price_intelligence


FIXTURES = {
    "079567100706": "3-IN-ONE Multi-Purpose Oil",
    "078742014616": "Great Value Cheese Wow! Spray Cheese",
    "858380002097": "Known comparison fixture",
}


class DealScannerM24OrchestrationTests(unittest.TestCase):
    def _broad(self, barcode, *, status="RATE_LIMITED_PROVIDER"):
        return {"source": "UPCitemdb", "barcode": barcode, "title": FIXTURES[barcode], "provider_status": status}

    def test_independent_provider_survives_broad_rate_limit(self):
        result = build_price_intelligence(
            self._broad("079567100706"), identifier="079567100706",
            provider_adapters={"Independent Retail Search": lambda identifier, location: ([
                {"retailer": "Target", "price": 4.68, "identifier": identifier,
                 "location_type": "ONLINE", "observed_at": "2026-09-27T12:00:00Z"},
            ], "AVAILABLE_WITH_PRICES")},
        )
        self.assertEqual(result["provider_states"][0]["status"], "RATE_LIMITED_PROVIDER")
        independent = next(item for item in result["provider_states"] if item["provider"] == "Independent Retail Search")
        self.assertEqual(independent["status"], "AVAILABLE_WITH_PRICES")
        self.assertEqual(result["observations"][0]["retailer"], "Target")
        self.assertEqual(result["observations"][0]["price"], 4.68)

    def test_multiple_sources_dedupe_and_preserve_sources(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "barcode": "078742014616", "title": FIXTURES["078742014616"],
            "offers": [{"merchant": "Walmart", "price": 3.48, "identifier": "078742014616"}],
        }, identifier="078742014616", provider_adapters={
            "Independent Retail Search": lambda identifier, location: ([
                {"retailer": "Walmart", "price": 3.48, "identifier": identifier,
                 "location_type": "ONLINE", "observed_at": "2026-09-27T12:00:00Z"},
            ], "AVAILABLE_WITH_PRICES")
        })
        walmart = [x for x in result["observations"] if x["retailer"] in {"Walmart", "Walmart.com"}]
        self.assertEqual(len(walmart), 1)
        self.assertEqual(len(walmart[0]["sources"]), 2)

    def test_delivered_price_does_not_discard_item_price(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "barcode": "858380002097", "title": FIXTURES["858380002097"],
            "offers": [{"merchant": "Home Depot", "price": 3.50, "shipping_price": 10.00,
                         "identifier": "858380002097"}],
        }, identifier="858380002097")
        observation = result["observations"][0]
        self.assertEqual(observation["price"], 3.50)
        self.assertEqual(observation["delivered_price"], 13.50)
        self.assertEqual(result["lowest_delivered_price"], 13.50)
        self.assertEqual(result["price_comparison_basis"], "DELIVERED_PRICE_WHEN_KNOWN_ELSE_ITEM_PRICE")

    def test_stale_or_outlier_observations_remain_visible(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "barcode": "079567100706", "title": FIXTURES["079567100706"],
            "offers": [
                {"merchant": "Walmart", "price": 4.00, "identifier": "079567100706"},
                {"merchant": "Unknown Seller", "price": 99.00, "identifier": "079567100706"},
            ],
        }, identifier="079567100706")
        self.assertEqual(len(result["observations"]), 2)
        self.assertTrue(any(not item["reference_eligible"] for item in result["observations"]))


if __name__ == "__main__":
    unittest.main()
