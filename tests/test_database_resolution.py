import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database_resolution import (
    DatabaseResolutionError, MISMATCH_ERROR, SQLITE_ONLY_ERROR, configured_sqlite_path, database_alignment,
    require_application_database_match, resolve_database_url,
)
from app.services.channel_inventory_engine import PRODUCTION_DB
from app.walmart_order_service import DB_PATH as WALMART_ORDER_DB_PATH


class DatabaseResolutionTests(unittest.TestCase):
    def test_application_and_engine_resolve_same_current_database(self):
        self.assertEqual(PRODUCTION_DB,configured_sqlite_path())
        self.assertEqual(WALMART_ORDER_DB_PATH,configured_sqlite_path())
        self.assertTrue(database_alignment(PRODUCTION_DB)["matches"])

    def test_local_and_railway_sqlite_urls_resolve_without_credentials(self):
        local = resolve_database_url("sqlite:///C:/BrooksHouseStore/app/data/brookshouse_store.db")
        railway = resolve_database_url("sqlite:////data/app-data/brookshouse_store.db")
        self.assertEqual((local.backend,local.sqlite_path.name),("sqlite","brookshouse_store.db"))
        self.assertEqual(railway.backend,"sqlite")
        self.assertTrue(railway.sqlite_path.as_posix().endswith("/data/app-data/brookshouse_store.db"))

    def test_non_sqlite_target_is_sanitized_and_refused(self):
        target = resolve_database_url("postgresql://secret-user:secret-password@db.example:5432/brookshouse")
        self.assertEqual(target.backend,"postgresql")
        self.assertNotIn("secret",target.sanitized_target)
        with patch("app.database_resolution.DATABASE_URL",
                   "postgresql://secret-user:secret-password@db.example:5432/brookshouse"):
            with self.assertRaisesRegex(DatabaseResolutionError,SQLITE_ONLY_ERROR):
                require_application_database_match()

    def test_mismatch_fails_closed_and_matching_target_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Path(directory)/"authoritative.db"
            stale = Path(directory)/"stale.db"
            url = f"sqlite:///{application.as_posix()}"
            with patch("app.database_resolution.DATABASE_URL",url):
                self.assertEqual(require_application_database_match(application),application.resolve())
                self.assertTrue(database_alignment(application)["matches"])
                self.assertFalse(database_alignment(stale)["matches"])
                with self.assertRaisesRegex(DatabaseResolutionError,MISMATCH_ERROR):
                    require_application_database_match(stale)

    def test_explicit_fixture_url_resolves_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)/"fixture.db"
            target = resolve_database_url(f"sqlite:///{fixture.as_posix()}")
            self.assertEqual(target.sqlite_path,fixture.resolve())


if __name__ == "__main__":
    unittest.main()
