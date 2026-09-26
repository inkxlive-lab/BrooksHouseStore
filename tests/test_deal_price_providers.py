import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

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
        self.assertEqual(value["location_type"], "ONLINE")

    def test_zip_does_not_turn_generic_online_price_into_local(self):
        value = normalize_observation({"title": "Widget", "price": "12.50"}, source="Example", context={"postal_code": "90210"})
        self.assertEqual(value["location_type"], "ONLINE")
        self.assertEqual(value["postal_code"], "90210")

    def test_walmart_catalog_match_is_a_market_observation_not_eligibility(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("""CREATE TABLE walmart_catalog_matches (
                barcode_lookup TEXT, match_status TEXT, walmart_item_id TEXT, title TEXT,
                brand TEXT, price_amount REAL, price_currency TEXT, checked_at TEXT,
                updated_at TEXT, error_message TEXT)"""))
            connection.execute(text("""INSERT INTO walmart_catalog_matches VALUES
                ('78742014616','MATCH','12345','Great Value Cheese Wow! Spray Cheese, American Cheese, 8 oz',
                 'Great Value',3.48,'USD','2026-09-26T12:00:00Z','2026-09-26T12:00:00Z',NULL)"""))
        with Session(engine) as database:
            result = build_price_intelligence(None, database=database, identifier="078742014616")
        self.assertEqual(result["observations"][0]["retailer"], "Walmart.com")
        self.assertEqual(result["observations"][0]["location_type"], "ONLINE")
        walmart = next(item for item in result["marketplaces"] if item["channel"] == "Walmart")
        self.assertEqual(walmart["eligibility"], "NOT_CONFIGURED")
        self.assertEqual(result["observations"][0]["price"], 3.48)

    def test_unavailable_retailers_do_not_get_fake_prices(self):
        result = build_price_intelligence(None, location={"postal_code": "90210"})
        self.assertEqual(result["provider_status"], "unavailable")
        self.assertIsNone(result["lowest_observed_price"])
        self.assertTrue(all(item["observations"] == [] for item in result["retailers"]))

    def test_provider_failure_is_explicit_and_nonfatal(self):
        result = build_price_intelligence({"source": "UPCitemdb", "error": "timeout"})
        self.assertEqual(result["provider_states"][0]["status"], "PROVIDER_ERROR")
        self.assertEqual(result["observations"], [])

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
