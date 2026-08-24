import unittest

from app.access_control import normalize_username, verify_password


class AccessControlAuthenticationTests(unittest.TestCase):
    def test_username_normalization_strips_whitespace_and_ignores_case(self):
        self.assertEqual(normalize_username("  AdMiN  "), "admin")
        self.assertEqual(normalize_username(" Helping Hands "), "helping hands")

    def test_scrypt_parser_fails_closed_for_unsupported_or_malformed_metadata(self):
        self.assertFalse(verify_password("example", "bcrypt$metadata-not-supported"))
        self.assertFalse(verify_password("example", "not-a-password-hash"))


if __name__ == "__main__":
    unittest.main()
