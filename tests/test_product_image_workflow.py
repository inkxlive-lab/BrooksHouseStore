import sqlite3
import unittest

from app.services import image_studio
from app.services.smart_lookup_images import save_lookup_image
from app.services.workflow_navigation import safe_return_to


class ProductImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("""CREATE TABLE product_images(
            image_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER,
            image_path TEXT, image_url TEXT, image_type TEXT, is_primary INTEGER, created_at TEXT)""")
        self.db.execute(
            "INSERT INTO product_images VALUES(1,1,'/static/original.jpg',NULL,'original',1,'before')"
        )

    def tearDown(self):
        self.db.close()

    def test_lookup_save_preserves_primary_unless_explicit(self):
        image_id = save_lookup_image(
            self.db, product_id=1, image_url="https://images.example.com/item.jpg",
            make_primary=False, created_at="now",
        )
        self.assertEqual(image_id, 2)
        self.assertEqual(
            self.db.execute("SELECT image_type,is_primary FROM product_images WHERE image_id=2").fetchone(),
            ("internet_lookup", 0),
        )
        self.assertEqual(self.db.execute("SELECT is_primary FROM product_images WHERE image_id=1").fetchone()[0], 1)

    def test_lookup_image_is_available_without_duplicate(self):
        first = save_lookup_image(
            self.db, product_id=1, image_url="https://images.example.com/item.jpg",
            created_at="now",
        )
        second = save_lookup_image(
            self.db, product_id=1, image_url="https://images.example.com/item.jpg",
            created_at="later",
        )
        self.assertEqual(first, second)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM product_images").fetchone()[0], 2)
        self.db.row_factory = sqlite3.Row
        images = image_studio._load_images(self.db, 1)
        self.assertEqual(images[-1]["display_url"], "https://images.example.com/item.jpg")

    def test_unsafe_lookup_image_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            save_lookup_image(self.db, product_id=1, image_url="file:///etc/passwd", created_at="now")

    def test_return_to_is_local_and_allowlisted(self):
        self.assertEqual(
            safe_return_to("/channels/publish?product_id=44"),
            "/channels/publish?product_id=44",
        )
        self.assertEqual(safe_return_to("https://evil.example/path"), "/channels/publish")
        self.assertEqual(safe_return_to("//evil.example/path"), "/channels/publish")
        self.assertEqual(safe_return_to("/unrelated/admin"), "/channels/publish")


if __name__ == "__main__":
    unittest.main()
