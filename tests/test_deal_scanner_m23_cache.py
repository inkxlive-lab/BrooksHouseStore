import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.integrations.product_lookup as lookup
from app.integrations.product_lookup import _lookup_upcitemdb, lookup_upc_online
from app.migrations.price_intelligence_cache_schema import INDEX_SQL, TABLE_SQL
from app.services.price_intelligence_cache import get_cached, put_cached
from app.services.deal_price_providers import build_price_intelligence


UPC = "079567100706"


def broad_payload():
    return {
        "found": True, "source": "UPCitemdb", "barcode": UPC,
        "title": "3-IN-ONE Oil", "images": ["https://img.example/oil.jpg"],
        "offers": [{"merchant": "Walmart.com", "price": "4.68", "currency": "USD"}],
        "provider_status": "AVAILABLE_WITH_PRICES",
    }


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class DealScannerM23CacheTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text(TABLE_SQL))
            connection.execute(text(INDEX_SQL))
        self.engine = engine
        lookup._LOOKUP_CACHE.clear()
        lookup._RATE_STATE.clear()
        lookup._INFLIGHT_LOCKS.clear()

    def tearDown(self):
        self.engine.dispose()

    def test_first_lookup_persists_and_second_lookup_avoids_network(self):
        with Session(self.engine) as database, patch("app.integrations.product_lookup.requests.get", return_value=Response(payload={"items": [broad_payload()]})) as get:
            first = lookup_upc_online(UPC, database=database)
            second = lookup_upc_online(UPC, database=database)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(first["offers"], second["offers"])
            self.assertEqual(second["cache_state"], "FRESH")
            self.assertIsNotNone(get_cached(database, UPC))

    def test_rate_limit_classification_and_safe_headers(self):
        for code, expected in (("TOO_FAST", "RATE_LIMITED_BURST"), ("EXCEED_LIMIT", "RATE_LIMITED_DAILY"), ("HTTP_TOO_MANY_REQUESTS", "RATE_LIMITED_PROVIDER")):
            lookup._RATE_STATE.clear()
            response = Response(429, {"code": code}, {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000", "Retry-After": "10"})
            with patch("app.integrations.product_lookup.requests.get", return_value=response):
                result = _lookup_upcitemdb(UPC)
            self.assertEqual(result["provider_status"], expected)
            self.assertEqual(result["rate_limit"]["x-ratelimit-limit"], "100")
            self.assertNotIn("Authorization", json.dumps(result))

    def test_rate_limited_with_cache_returns_cached_observations(self):
        with Session(self.engine) as database:
            put_cached(database, UPC, broad_payload())
            old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            database.execute(text("UPDATE deal_price_intelligence_cache SET retrieved_at=:old, updated_at=:old"), {"old": old})
            database.commit()
            lookup._LOOKUP_CACHE.clear()
            response = Response(429, {"code": "TOO_FAST"}, {"Retry-After": "60"})
            with patch("app.integrations.product_lookup.requests.get", return_value=response):
                result = lookup_upc_online(UPC, database=database)
            self.assertEqual(result["provider_status"], "CACHED_FALLBACK")
            self.assertEqual(result["live_provider_status"], "RATE_LIMITED_BURST")
            self.assertEqual(len(result["offers"]), 1)

    def test_rate_limited_without_cache_has_no_fake_prices(self):
        response = Response(429, {"code": "EXCEED_LIMIT"}, {"Retry-After": "60"})
        with Session(self.engine) as database, patch("app.integrations.product_lookup.requests.get", return_value=response):
            result = lookup_upc_online(UPC, database=database)
            self.assertNotIn("offers", result)
            self.assertEqual(result["provider_status"], "RATE_LIMITED_DAILY")

    def test_stale_cache_is_visible_but_not_reference_eligible(self):
        with Session(self.engine) as database:
            put_cached(database, UPC, broad_payload())
            old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            database.execute(text("UPDATE deal_price_intelligence_cache SET retrieved_at=:old, updated_at=:old"), {"old": old})
            database.commit()
            lookup._LOOKUP_CACHE.clear()
            cached = get_cached(database, UPC)
            self.assertEqual(cached["cache_state"], "STALE")
            intelligence = build_price_intelligence(cached, identifier=UPC)
            self.assertEqual(intelligence["observations"][0]["freshness_state"], "STALE")
            self.assertIsNone(intelligence["reference_price"]["price"])


if __name__ == "__main__":
    unittest.main()
