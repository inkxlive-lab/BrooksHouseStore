import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services.deal_price_providers import (
    build_price_intelligence,
    ebay_browse_observations,
    normalize_observation,
    select_reference_price,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DealScannerM22QualityTests(unittest.TestCase):
    def _observation(self, retailer, price, *, days_old=0, exact=True):
        observed = datetime.now(timezone.utc) - timedelta(days=days_old)
        return normalize_observation(
            {
                "retailer": retailer,
                "price": price,
                "currency": "USD",
                "availability": "IN_STOCK",
                "location_type": "ONLINE",
                "observed_at": observed.isoformat(),
                "identifier": "079567100706",
            },
            source="test-provider",
            exact_match=exact,
            context={"postal_code": "38127", "store_name": "Requested store"},
        )

    def test_trusted_retailers_are_prioritized_and_outliers_explained(self):
        observations = [
            self._observation("Walmart.com", 4.68),
            self._observation("Home Depot", 5.35),
            self._observation("Ace Hardware", 4.99),
            self._observation("True Value", 3.49),
            self._observation("Unknown Seller", 49.36),
        ]

        result = select_reference_price(observations)

        self.assertIn(result["price"], {4.68, 4.99})
        self.assertIn("current", result["explanation"].lower())
        self.assertIn("excluded 1", result["explanation"].lower())
        outlier = next(row for row in result["ranked_observations"] if row["retailer"] == "Unknown Seller")
        self.assertEqual(outlier["quality_state"], "OUTLIER")
        self.assertIn("trusted price cluster", outlier["outlier_reason"].lower())

    def test_stale_observation_is_not_selected_over_current_evidence(self):
        result = select_reference_price([
            self._observation("Walmart.com", 5.00, days_old=45),
            self._observation("Ace Hardware", 4.99),
        ])

        self.assertEqual(result["price"], 4.99)
        stale = next(row for row in result["ranked_observations"] if row["retailer"] == "Walmart.com")
        self.assertEqual(stale["freshness_state"], "STALE")
        self.assertEqual(stale["quality_state"], "LOW_CONFIDENCE")

    def test_postal_context_does_not_turn_online_result_into_local(self):
        observation = self._observation("Walmart.com", 4.68)
        self.assertEqual(observation["location_type"], "ONLINE")
        self.assertEqual(observation["postal_code"], "38127")
        self.assertIsNone(observation["store_id"])

    def test_ebay_active_listing_is_market_observation(self):
        payload = {
            "itemSummaries": [{
                "title": "3-IN-ONE Oil 4 oz",
                "price": {"value": "12.00", "currency": "USD"},
                "shippingOptions": [{"shippingCost": {"value": "3.00"}}],
                "condition": "USED",
                "seller": {"username": "seller-1"},
                "itemWebUrl": "https://www.ebay.com/itm/123",
            }]
        }
        with patch.dict(os.environ, {"EBAY_OAUTH_TOKEN": "configured"}, clear=False), \
                patch("app.services.deal_price_providers.urlopen", return_value=_Response(payload)):
            observations, status = ebay_browse_observations("079567100706")

        self.assertEqual(status, "AVAILABLE")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["location_type"], "MARKETPLACE")
        self.assertEqual(observations[0]["shipping_price"], 3.0)
        self.assertEqual(observations[0]["condition"], "USED")
        self.assertNotIn("sold", observations[0])

    def test_provider_failure_returns_partial_structured_state(self):
        intelligence = build_price_intelligence(
            {"source": "UPCitemdb", "error": "timeout"},
            identifier="079567100706",
        )

        states = {row["provider"]: row["status"] for row in intelligence["provider_states"]}
        self.assertEqual(states["UPCitemdb"], "PROVIDER_ERROR")
        self.assertEqual(states["eBay Browse"], "AUTH_REQUIRED")
        self.assertEqual(intelligence["observations"], [])


if __name__ == "__main__":
    unittest.main()
