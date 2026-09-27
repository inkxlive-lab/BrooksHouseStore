"""Preview/apply the additive Deal Scanner price-intelligence cache schema.

Default behavior is preview only.  Apply requires a disposable or explicitly
approved database and a verified SQLite backup path.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


TABLE_SQL = """CREATE TABLE IF NOT EXISTS deal_price_intelligence_cache (
    cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT NOT NULL,
    provider TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    provider_observation_at TEXT,
    last_status TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(identifier, provider)
);"""
INDEX_SQL = "CREATE INDEX IF NOT EXISTS ix_deal_price_cache_identifier ON deal_price_intelligence_cache(identifier);"


def preview(_database_url: str) -> str:
    return TABLE_SQL + "\n\n" + INDEX_SQL


def _sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("A file-backed SQLite URL is required for SQLite backup.")
    return Path(url.database).expanduser().resolve()


def apply(database_url: str, backup_path: str | None) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        source = _sqlite_path(database_url)
        if not source.is_file() or not backup_path:
            raise ValueError("SQLite --apply requires an existing database and --backup-path.")
        destination = Path(backup_path).expanduser().resolve()
        if destination == source:
            raise ValueError("Backup path must differ from the database path.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if destination.stat().st_size != source.stat().st_size:
            raise RuntimeError("SQLite backup verification failed.")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text(TABLE_SQL))
            connection.execute(text(INDEX_SQL))
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-path")
    args = parser.parse_args()
    if not args.apply:
        print("PREVIEW ONLY: no database connection or change was made.")
        print(preview(args.database_url))
        return 0
    apply(args.database_url, args.backup_path)
    print("Price-intelligence cache table created successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
