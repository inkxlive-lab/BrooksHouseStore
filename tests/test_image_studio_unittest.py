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


if __name__ == "__main__":
    unittest.main()
