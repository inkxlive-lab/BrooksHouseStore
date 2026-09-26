import unittest

from app.services.deal_price_providers import (
    build_price_intelligence,
    marketplace_statuses,
    normalize_observation,
)


class DealPriceProviderTests(unittest.TestCase):
    def test_normalizes_exact_online_observation(self):
        value = normalize_observation({"title": "Widget", "price": "12.50", "link": "https://example.test"}, source="Example", exact_match=True)
        self.assertEqual(value["price"], 12.50)
        self.assertTrue(value["exact_match"])
        self.assertFalse(value["local"])

    def test_unavailable_retailers_do_not_get_fake_prices(self):
        result = build_price_intelligence(None, location={"postal_code": "90210"})
        self.assertEqual(result["provider_status"], "unavailable")
        self.assertIsNone(result["lowest_observed_price"])
        self.assertTrue(all(item["observations"] == [] for item in result["retailers"]))

    def test_multiple_observations_and_marketplace_status_are_separate(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "barcode": "012345678905", "title": "Widget",
            "offers": [{"merchant": "A", "price": "9.99"}, {"merchant": "B", "price": "14.00"}],
        })
        self.assertEqual(result["lowest_observed_price"], 9.99)
        self.assertEqual(len(result["online_observations"]), 2)
        self.assertEqual(result["marketplaces"][0]["eligibility"], "NOT_CONFIGURED")

    def test_mismatched_variant_is_rejected(self):
        result = build_price_intelligence({
            "source": "UPCitemdb", "barcode": "012345678905", "title": "Widget", "size": "12 oz",
            "offers": [{"merchant": "A", "price": "9.99", "size": "24 oz"}],
        })
        self.assertEqual(result["observations"], [])


if __name__ == "__main__":
    unittest.main()
