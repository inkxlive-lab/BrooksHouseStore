import unittest

from app.shopify_operations import _line_identifiers


class ShopifyIdentifierIngestionTests(unittest.TestCase):
    def test_variant_identifiers_are_preferred_and_barcode_normalized(self):
        sku, barcode = _line_identifiers({"sku":"LINE-SKU","variant":{"sku":" VARIANT-SKU ","barcode":"0 123-456"}})
        self.assertEqual((sku,barcode),("VARIANT-SKU","0123456"))

    def test_line_sku_fallback_and_genuinely_missing_identifiers(self):
        self.assertEqual(_line_identifiers({"sku":"LINE-SKU","variant":None}),("LINE-SKU",""))
        self.assertEqual(_line_identifiers({"variant":{}}),("",""))


if __name__ == "__main__":
    unittest.main()
