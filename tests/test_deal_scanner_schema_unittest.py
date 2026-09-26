import re
import unittest

from app.migrations.deal_scanner_schema import preview


class DealScannerSchemaTests(unittest.TestCase):
    def test_preview_is_additive_and_index_names_are_unique(self):
        sql = preview("sqlite:///preview-only-deal-scanner.db")
        self.assertIn("CREATE TABLE deal_scan_references", sql)
        names = re.findall(r"CREATE INDEX ([A-Za-z0-9_]+)", sql)
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
