import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import image_studio


class FakeProvider(image_studio.ImageProvider):
    name = "fake"
    model = "fake-image-v1"
    configured = True

    def edit(self, source, filename, content_type, prompt):
        assert source == b"source-image"
        assert "Do not invent" in prompt
        return image_studio.ProviderResult(b"generated-image")


class FailingProvider(FakeProvider):
    def edit(self, source, filename, content_type, prompt):
        raise RuntimeError("provider test failure")


def create_database(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
        CREATE TABLE products (product_id INTEGER PRIMARY KEY, product_name TEXT NOT NULL);
        CREATE TABLE product_barcodes (barcode_id INTEGER PRIMARY KEY, product_id INTEGER, barcode TEXT, is_primary INTEGER);
        CREATE TABLE product_images (
            image_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
            image_path TEXT, image_url TEXT, image_type TEXT NOT NULL DEFAULT 'front',
            is_primary INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        INSERT INTO products VALUES (1, 'Test Coffee Maker');
        INSERT INTO products VALUES (2, 'No Image Product');
        INSERT INTO product_barcodes VALUES (1, 1, '012345678905', 1);
        INSERT INTO product_images VALUES (1, 1, '/static/product-images/source.png', NULL, 'front', 1, '2026-01-01');
        """)


class ImageStudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "test.db"
        create_database(self.database)
        self.static = self.root / "static"
        self.source = self.static / "product-images" / "source.png"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"source-image")
        self.patchers = [
            patch.object(image_studio, "DB_PATH", self.database),
            patch.object(image_studio, "APP_DIR", self.root),
            patch.object(image_studio, "PENDING_DIR", self.static / "generated-images" / "pending"),
            patch.object(image_studio, "APPROVED_DIR", self.static / "product-images" / "ai-studio"),
            patch.object(image_studio, "SOURCE_DIR", self.root / "studio-sources"),
            patch.object(image_studio, "PRODUCT_IMAGE_ROOT", self.static / "product_images"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def client(self):
        app = FastAPI()
        image_studio.install_image_studio(app)
        return TestClient(app)

    def test_configuration_needed_page(self):
        self.assertEqual(
            image_studio._display_reference(str(self.source)),
            "/static/product-images/source.png",
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            response = self.client().get("/images/studio?product_id=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Configuration needed", response.text)
        self.assertIn("Test Coffee Maker", response.text)

    def test_generate_approve_discard_and_preserve_original(self):
        with patch.object(image_studio, "_provider_factory", FakeProvider):
            client = self.client()
            generated = client.post("/images/studio/generate", data={
                "product_id": 1, "source_image_id": 1, "preset": "clean_marketplace", "instruction": "Center it",
            }, follow_redirects=False)
            self.assertEqual(generated.status_code, 303)
            self.assertEqual(self.source.read_bytes(), b"source-image")
            with closing(sqlite3.connect(self.database)) as connection:
                generation_id, status = connection.execute(
                    "SELECT generation_id,status FROM image_studio_generations"
                ).fetchone()
                self.assertEqual(status, "pending")

            approved = client.post(
                f"/images/studio/{generation_id}/approve", data={"save_as_primary": "true"},
                follow_redirects=False,
            )
            self.assertEqual(approved.status_code, 303)
            self.assertEqual(self.source.read_bytes(), b"source-image")
            with closing(sqlite3.connect(self.database)) as connection:
                rows = connection.execute(
                    "SELECT image_id,image_path,is_primary FROM product_images ORDER BY image_id"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0], (1, "/static/product-images/source.png", 0))
                self.assertEqual(rows[1][2], 1)
                self.assertEqual(
                    connection.execute("SELECT status FROM image_studio_generations").fetchone()[0], "approved"
                )

            client.post("/images/studio/generate", data={
                "product_id": 1, "source_image_id": 1, "preset": "clean_marketplace", "instruction": "",
            })
            with closing(sqlite3.connect(self.database)) as connection:
                second = connection.execute("SELECT MAX(generation_id) FROM image_studio_generations").fetchone()[0]
            discarded = client.post(f"/images/studio/{second}/discard", follow_redirects=False)
            self.assertEqual(discarded.status_code, 303)
            self.assertEqual(self.source.read_bytes(), b"source-image")
            with closing(sqlite3.connect(self.database)) as connection:
                row = connection.execute(
                    "SELECT status,generated_image_path FROM image_studio_generations WHERE generation_id=?", (second,)
                ).fetchone()
                self.assertEqual(row, ("discarded", None))

    def test_approval_without_primary_preserves_existing_primary(self):
        with patch.object(image_studio, "_provider_factory", FakeProvider):
            client = self.client()
            client.post("/images/studio/generate", data={
                "product_id": 1, "source_image_id": 1, "preset": "clean_marketplace",
                "instruction": "", "variations": 1,
            })
            with closing(sqlite3.connect(self.database)) as connection:
                generation_id = connection.execute(
                    "SELECT generation_id FROM image_studio_generations"
                ).fetchone()[0]
            response = client.post(
                f"/images/studio/{generation_id}/approve", data={}, follow_redirects=False
            )
            self.assertEqual(response.status_code, 303)
            with closing(sqlite3.connect(self.database)) as connection:
                rows = connection.execute(
                    "SELECT image_id,is_primary FROM product_images ORDER BY image_id"
                ).fetchall()
            self.assertEqual(rows, [(1, 1), (2, 0)])
            self.assertEqual(self.source.read_bytes(), b"source-image")

    def test_multiple_variations_create_independent_pending_records(self):
        with patch.object(image_studio, "_provider_factory", FakeProvider):
            response = self.client().post("/images/studio/generate", data={
                "product_id": 1, "source_image_id": 1, "preset": "lifestyle",
                "instruction": "Use a kitchen", "variations": 3,
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT status,preset_name FROM image_studio_generations ORDER BY generation_id"
            ).fetchall()
        self.assertEqual(rows, [("pending", "lifestyle")] * 3)
        self.assertEqual(self.source.read_bytes(), b"source-image")

    def test_upload_source_is_persistent_and_not_primary(self):
        client = self.client()
        png = b"\x89PNG\r\n\x1a\n" + b"safe-original"
        response = client.post("/images/studio/source/upload", data={
            "product_id": 1, "save_to_gallery": "true",
        }, files={"photo": ("../../camera.png", png, "image/png")}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        uploads = list((self.root / "studio-sources").glob("*.png"))
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0].read_bytes(), png)
        with closing(sqlite3.connect(self.database)) as connection:
            images = connection.execute(
                "SELECT image_type,is_primary FROM product_images ORDER BY image_id"
            ).fetchall()
            original_name = connection.execute(
                "SELECT original_filename FROM image_studio_sources"
            ).fetchone()[0]
        self.assertEqual(images, [("front", 1), ("original_upload", 0)])
        self.assertEqual(original_name, "camera.png")

    def test_provider_failure_keeps_failed_generation_record(self):
        with patch.object(image_studio, "_provider_factory", FailingProvider):
            response = self.client().post("/images/studio/generate", data={
                "product_id": 1, "source_image_id": 1, "preset": "enhance_only",
                "instruction": "", "variations": 1,
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT status,generated_image_path,error_message FROM image_studio_generations"
            ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIsNone(row[1])
        self.assertIn("provider test failure", row[2])
        self.assertEqual(self.source.read_bytes(), b"source-image")

    def test_persistent_and_legacy_product_references_stay_confined(self):
        persistent = self.static / "product_images" / "shopify" / "safe.jpg"
        persistent.parent.mkdir(parents=True)
        persistent.write_bytes(b"persistent-image")
        url_reference = "/static/product_images/shopify/safe.jpg"
        windows_reference = r"C:\BrooksHouseStore\app\static\product_images\shopify\safe.jpg"
        self.assertEqual(image_studio._source_local_path(url_reference), persistent.resolve())
        self.assertEqual(image_studio._source_local_path(windows_reference), persistent.resolve())
        self.assertEqual(
            image_studio._display_reference(windows_reference),
            "/images/studio/product-images/shopify/safe.jpg",
        )
        self.assertIsNone(
            image_studio._persistent_product_image_path("/static/product_images/../../outside.jpg")
        )
        self.assertIsNone(
            image_studio._persistent_product_image_path(r"C:\Windows\System32\outside.jpg")
        )

    def test_all_with_and_without_image_filters(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            all_ids = {row["product_id"] for row in image_studio._load_products(connection, None, "", "all")}
            with_ids = {row["product_id"] for row in image_studio._load_products(connection, None, "", "with_images")}
            without_ids = {row["product_id"] for row in image_studio._load_products(connection, None, "", "without_images")}
        self.assertEqual(all_ids, {1, 2})
        self.assertEqual(with_ids, {1})
        self.assertEqual(without_ids, {2})

    def test_product_without_source_cannot_generate_but_can_upload(self):
        client = self.client()
        blocked = client.post("/images/studio/generate", data={
            "product_id": 2, "preset": "clean_marketplace", "variations": 1,
        }, follow_redirects=False)
        self.assertEqual(blocked.status_code, 303)
        self.assertIn("Select%20a%20valid%20source", blocked.headers["location"])
        png = b"\x89PNG\r\n\x1a\n" + b"new-original"
        uploaded = client.post("/images/studio/source/upload", data={"product_id": 2},
                               files={"photo": ("photo.png", png, "image/png")}, follow_redirects=False)
        self.assertEqual(uploaded.status_code, 303)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM image_studio_sources WHERE product_id=2"
            ).fetchone()[0], 1)

    def test_product_44_style_relative_image_path_resolves_safely(self):
        product_file = self.static / "product-images" / "internet" / "44-test.jpg"
        product_file.parent.mkdir(parents=True)
        product_file.write_bytes(b"source-image")
        self.assertEqual(
            image_studio._source_local_path("product-images/internet/44-test.jpg"),
            product_file.resolve(),
        )
        self.assertEqual(
            image_studio._read_source("product-images/internet/44-test.jpg")[0], b"source-image"
        )
        self.assertIsNone(image_studio._source_local_path("../../outside.jpg"))


if __name__ == "__main__":
    unittest.main()
