import sqlite3
from pathlib import Path

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


def create_test_database(path: Path):
    with sqlite3.connect(path) as connection:
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


def test_configuration_needed_page(tmp_path, monkeypatch):
    database = tmp_path / "test.db"
    create_test_database(database)
    monkeypatch.setattr(image_studio, "DB_PATH", database)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    app = FastAPI()
    image_studio.install_image_studio(app)
    response = TestClient(app).get("/images/studio?product_id=1")
    assert response.status_code == 200
    assert "Configuration needed" in response.text
    assert "Test Coffee Maker" in response.text


def test_generate_approve_and_discard_preserve_original(tmp_path, monkeypatch):
    database = tmp_path / "test.db"
    create_test_database(database)
    static = tmp_path / "static"
    source = static / "product-images" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source-image")
    monkeypatch.setattr(image_studio, "DB_PATH", database)
    monkeypatch.setattr(image_studio, "APP_DIR", tmp_path)
    monkeypatch.setattr(image_studio, "PENDING_DIR", static / "generated-images" / "pending")
    monkeypatch.setattr(image_studio, "APPROVED_DIR", static / "product-images" / "ai-studio")
    monkeypatch.setattr(image_studio, "_provider_factory", FakeProvider)
    app = FastAPI()
    image_studio.install_image_studio(app)
    client = TestClient(app)

    generated = client.post("/images/studio/generate", data={
        "product_id": 1, "source_image_id": 1, "preset": "clean_marketplace", "instruction": "Center it",
    }, follow_redirects=False)
    assert generated.status_code == 303
    assert source.read_bytes() == b"source-image"
    with sqlite3.connect(database) as connection:
        generation_id, status = connection.execute(
            "SELECT generation_id,status FROM image_studio_generations"
        ).fetchone()
        assert status == "pending"

    approved = client.post(
        f"/images/studio/{generation_id}/approve", data={"save_as_primary": "true"}, follow_redirects=False
    )
    assert approved.status_code == 303
    assert source.read_bytes() == b"source-image"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT image_id,image_path,is_primary FROM product_images ORDER BY image_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == (1, "/static/product-images/source.png", 0)
        assert rows[1][2] == 1
        status = connection.execute("SELECT status FROM image_studio_generations").fetchone()[0]
        assert status == "approved"

    client.post("/images/studio/generate", data={
        "product_id": 1, "source_image_id": 1, "preset": "clean_marketplace", "instruction": "",
    })
    with sqlite3.connect(database) as connection:
        second = connection.execute("SELECT MAX(generation_id) FROM image_studio_generations").fetchone()[0]
    discarded = client.post(f"/images/studio/{second}/discard", follow_redirects=False)
    assert discarded.status_code == 303
    assert source.read_bytes() == b"source-image"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,generated_image_path FROM image_studio_generations WHERE generation_id=?", (second,)
        ).fetchone() == ("discarded", None)
