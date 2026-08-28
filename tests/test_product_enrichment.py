from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.database.models import (
    Product, ProductBarcode, ProductEnrichmentAuditEvent, ProductEnrichmentBatch,
    ProductEnrichmentItem, ProductEnrichmentLookupCache, ProductEnrichmentProposal,
    ProductImage,
)
from app.migrations.product_enrichment_phase1 import TABLES, apply as apply_migration, preview
from app.product_enrichment import _owner, _review_fields, install_product_enrichment
from app.services.product_enrichment_lookup import RateLimiter, internet_candidates
from app.services.product_enrichment_workflow import (
    MAX_BATCH_SIZE, StaleProductError, apply_item, create_batch, process_next_item,
    review_proposal, set_batch_status,
)
from app.services.smart_scan_updates import approved_product_update_values


SCHEMA_TABLES = [
    Product.__table__, ProductBarcode.__table__, ProductImage.__table__, *TABLES,
]


class ProductEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database_path = Path(self.temp.name) / "test.db"
        self.engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        Product.metadata.create_all(self.engine, tables=SCHEMA_TABLES)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user = SimpleNamespace(
            user_id=7, username="owner", display_name="Owner Admin", role="owner_admin"
        )

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def add_product(self, name="Unknown Product", barcode="012345678905", **values):
        with self.Session() as database:
            product = Product(product_name=name, active=True, **values)
            if barcode:
                product.barcodes.append(ProductBarcode(barcode=barcode, is_primary=True))
            database.add(product)
            database.commit()
            return product.product_id

    def ready_item(self, lookup_result):
        product_id = self.add_product()
        with self.Session() as database:
            batch = create_batch(database, self.user, 10)
            set_batch_status(database, batch, "running", self.user)
            item = process_next_item(
                database, batch, self.user, internet_lookup=lambda _barcode: lookup_result,
                limiter=RateLimiter(0), include_internet=True,
            )
            return product_id, batch.batch_id, item.item_id

    def test_batch_defaults_are_bounded_and_selection_is_deterministic(self):
        first = self.add_product(name="Unknown Product", barcode="100000000001")
        second = self.add_product(name="Useful name", barcode="100000000002", brand="Brand")
        with self.Session() as database:
            batch = create_batch(database, self.user, 1)
            items = database.scalars(select(ProductEnrichmentItem).where(
                ProductEnrichmentItem.batch_id == batch.batch_id
            )).all()
            self.assertEqual([row.product_id for row in items], [first])
            self.assertEqual(batch.requested_batch_size, 1)
            with self.assertRaises(ValueError):
                create_batch(database, self.user, MAX_BATCH_SIZE + 1)

    def test_lookup_is_persisted_and_resumable(self):
        product_id, batch_id, item_id = self.ready_item({
            "found": True, "source": "Mock UPC", "title": "Better Product",
            "brand": "Better Brand", "description": "Useful description",
            "category": "Household", "weight": "12 oz", "price_low": 4.99,
            "images": ["https://example.invalid/product.jpg"],
        })
        with self.Session() as database:
            item = database.get(ProductEnrichmentItem, item_id)
            proposals = database.scalars(select(ProductEnrichmentProposal).where(
                ProductEnrichmentProposal.item_id == item_id
            )).all()
            self.assertEqual(item.status, "ready")
            self.assertEqual(item.attempt_count, 1)
            self.assertIn("product_name", {row.field_name for row in proposals})
            self.assertIn("product_image", {row.field_name for row in proposals})
            batch = database.get(ProductEnrichmentBatch, batch_id)
            self.assertEqual(batch.processed_count, 1)
            set_batch_status(database, batch, "paused", self.user)
            set_batch_status(database, batch, "running", self.user)
            self.assertIsNone(process_next_item(database, batch, self.user, include_internet=False))
            self.assertEqual(batch.status, "reviewing")

    def test_approval_is_separate_and_apply_preserves_images_and_primary(self):
        product_id = self.add_product(name="Unknown Product")
        with self.Session() as database:
            product = database.get(Product, product_id)
            product.images.append(ProductImage(
                image_url="https://example.invalid/existing.jpg", is_primary=True
            ))
            database.commit()
            batch = create_batch(database, self.user, 10)
            queued = database.scalar(select(ProductEnrichmentItem).where(
                ProductEnrichmentItem.batch_id == batch.batch_id,
                ProductEnrichmentItem.product_id == product_id,
            ))
            fields = json.loads(queued.missing_fields_json)
            fields.append("product_image")
            queued.missing_fields_json = json.dumps(fields)
            database.commit()
            set_batch_status(database, batch, "running", self.user)
            item = process_next_item(database, batch, self.user, internet_lookup=lambda _barcode: {
                "found": True, "source": "Mock", "title": "Approved Name",
                "images": ["https://example.invalid/new.jpg"],
            }, limiter=RateLimiter(0))
            proposals = database.scalars(select(ProductEnrichmentProposal).where(
                ProductEnrichmentProposal.item_id == item.item_id
            )).all()
            name = next(row for row in proposals if row.field_name == "product_name")
            image = next(row for row in proposals if row.field_name == "product_image")
            review_proposal(database, name, "approve", self.user)
            review_proposal(database, image, "approve", self.user)
            self.assertEqual(database.get(Product, product_id).product_name, "Unknown Product")
            self.assertEqual(len(database.get(Product, product_id).images), 1)
            self.assertEqual(apply_item(database, item, self.user), 2)
            product = database.get(Product, product_id)
            database.refresh(product)
            self.assertEqual(product.product_name, "Approved Name")
            self.assertEqual(len(product.images), 2)
            self.assertEqual(sum(1 for row in product.images if row.is_primary), 1)
            self.assertEqual(
                next(row for row in product.images if row.is_primary).image_url,
                "https://example.invalid/existing.jpg",
            )
            events = database.scalars(select(ProductEnrichmentAuditEvent).where(
                ProductEnrichmentAuditEvent.batch_id == batch.batch_id,
                ProductEnrichmentAuditEvent.event_type == "field_applied",
            )).all()
            self.assertEqual(len(events), 2)
            self.assertTrue(all(row.actor_name == "Owner Admin" for row in events))

    def test_apply_refuses_stale_product_field(self):
        product_id, _batch_id, item_id = self.ready_item({
            "found": True, "source": "Mock", "title": "Suggested Name", "images": []
        })
        with self.Session() as database:
            item = database.get(ProductEnrichmentItem, item_id)
            proposal = database.scalar(select(ProductEnrichmentProposal).where(
                ProductEnrichmentProposal.item_id == item_id,
                ProductEnrichmentProposal.field_name == "product_name",
            ))
            review_proposal(database, proposal, "approve", self.user)
            product = database.get(Product, product_id)
            product.product_name = "Manual edit after lookup"
            database.commit()
            with self.assertRaises(StaleProductError):
                apply_item(database, item, self.user)
            self.assertEqual(database.get(Product, product_id).product_name,
                             "Manual edit after lookup")

    def test_rate_limiter_is_mockable(self):
        state = {"now": 10.0}
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            state["now"] += seconds
        limiter = RateLimiter(2.0, clock=lambda: state["now"], sleeper=sleep)
        limiter.wait()
        state["now"] += 0.5
        limiter.wait()
        self.assertEqual(sleeps, [1.5])

    def test_every_provider_request_uses_rate_limit_hook(self):
        calls = []
        def lookup(_barcode, before_request=None):
            before_request()
            calls.append("first")
            before_request()
            calls.append("second")
            return {"found": False, "source": "Mock", "images": []}
        state = {"now": 0.0}
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            state["now"] += seconds
        candidates, error = internet_candidates(
            "123", lookup=lookup,
            limiter=RateLimiter(1.0, clock=lambda: state["now"], sleeper=sleep),
        )
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(candidates, [])
        self.assertIsNone(error)

    def test_successful_internet_lookup_is_cached(self):
        calls = []

        def lookup(_barcode):
            calls.append(_barcode)
            return {
                "found": True,
                "source": "Mock UPC",
                "title": "Cached product",
                "images": [],
            }

        with self.Session() as database:
            first, first_error = internet_candidates(
                "123456789012", lookup=lookup, limiter=RateLimiter(0), database=database
            )
            database.commit()
            second, second_error = internet_candidates(
                "123456789012", lookup=lookup, limiter=RateLimiter(0), database=database
            )
            self.assertEqual(calls, ["123456789012"])
            self.assertEqual(first_error, second_error)
            self.assertEqual(
                [(row.field_name, row.value) for row in first],
                [(row.field_name, row.value) for row in second],
            )
            self.assertEqual(
                database.scalar(select(func.count()).select_from(ProductEnrichmentLookupCache)),
                1,
            )

    def test_internet_errors_are_durable_retryable_and_capped(self):
        self.add_product(name="Unknown Product")
        with self.Session() as database:
            batch = create_batch(database, self.user, 10)
            set_batch_status(database, batch, "running", self.user)
            for expected_attempt in (1, 2, 3):
                item = process_next_item(
                    database, batch, self.user,
                    internet_lookup=lambda _barcode: {
                        "found": False, "source": "Mock", "error": "rate limited"
                    }, limiter=RateLimiter(0),
                )
                self.assertEqual(item.attempt_count, expected_attempt)
                expected_status = "error" if expected_attempt < 3 else "ready"
                self.assertEqual(item.status, expected_status)
            errors = database.scalars(select(ProductEnrichmentProposal).where(
                ProductEnrichmentProposal.item_id == item.item_id,
                ProductEnrichmentProposal.status == "error",
            )).all()
            self.assertEqual(len(errors), 3)
            self.assertTrue(all(row.error_message == "rate limited" for row in errors))

    def test_migration_preview_does_not_require_a_database_file(self):
        sql = preview("sqlite:///this-file-is-not-opened.db")
        self.assertIn("CREATE TABLE product_enrichment_batches", sql)
        self.assertIn("CREATE TABLE product_enrichment_audit_events", sql)
        postgres_sql = preview("postgresql+psycopg://localhost/preview_only")
        self.assertIn("CREATE TABLE product_enrichment_batches", postgres_sql)

    def test_migration_apply_requires_and_creates_verified_sqlite_backup(self):
        source = Path(self.temp.name) / "migration-source.db"
        backup = Path(self.temp.name) / "migration-backup.db"
        source_engine = create_engine(f"sqlite:///{source.as_posix()}")
        with source_engine.begin() as connection:
            connection.execute(text("CREATE TABLE existing_marker (id INTEGER PRIMARY KEY)"))
        source_engine.dispose()

        apply_migration(f"sqlite:///{source.as_posix()}", str(backup))

        self.assertTrue(backup.is_file())
        backup_engine = create_engine(f"sqlite:///{backup.as_posix()}")
        self.assertIn("existing_marker", inspect(backup_engine).get_table_names())
        self.assertNotIn(
            "product_enrichment_batches", inspect(backup_engine).get_table_names()
        )
        backup_engine.dispose()
        migrated_engine = create_engine(f"sqlite:///{source.as_posix()}")
        self.assertIn(
            "product_enrichment_lookup_cache", inspect(migrated_engine).get_table_names()
        )
        migrated_engine.dispose()

    def test_owner_guard_rejects_every_other_role(self):
        owner_request = SimpleNamespace(state=SimpleNamespace(auth_user=self.user))
        self.assertIs(_owner(owner_request), self.user)
        for role in ("manager", "adult_staff", "view_only", ""):
            request = SimpleNamespace(
                state=SimpleNamespace(
                    auth_user=SimpleNamespace(role=role)
                )
            )
            with self.assertRaises(HTTPException) as raised:
                _owner(request)
            self.assertEqual(raised.exception.status_code, 403)

    def test_review_page_receives_all_supported_fields(self):
        product = Product(product_name="Example")
        self.assertEqual(
            [field for field, _label, _value in _review_fields(product)],
            [
                "product_name", "brand", "description", "category",
                "size_value", "size_unit", "suggested_retail_price", "product_image",
            ],
        )

    def test_smart_scan_keeps_product_name_and_description_distinct(self):
        values = approved_product_update_values(
            "Selected title", "Long description", "Brand", "Category"
        )
        self.assertEqual(values[0], "Selected title")
        self.assertEqual(values[1], "Long description")
        preserve_description = approved_product_update_values(
            "Selected title", None, None, None
        )
        self.assertIsNone(preserve_description[1])

    def test_enrichment_templates_parse(self):
        environment = Environment(loader=FileSystemLoader("app/templates"))
        for name in (
            "product_enrichment_batches.html", "product_enrichment_batch.html",
            "product_enrichment_review.html", "product_enrichment_audit.html",
        ):
            environment.get_template(name)

    def test_registered_enrichment_routes_and_legacy_redirect(self):
        app = FastAPI()

        @app.middleware("http")
        async def owner_session(request, call_next):
            request.state.auth_user = self.user
            return await call_next(request)

        install_product_enrichment(app)
        with patch("app.product_enrichment.SessionLocal", self.Session):
            with TestClient(app) as client:
                response = client.get("/product-enrichment", follow_redirects=False)
                registered = client.get("/admin/product-enrichment", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/admin/product-enrichment")
        self.assertEqual(registered.status_code, 200)
        self.assertIn("Product Enrichment", registered.text)


if __name__ == "__main__":
    unittest.main()
