"""Preview or apply the additive Deal Scanner reference schema.

Default behavior is preview only.  This table stores temporary sourcing
evidence and economics; it does not create inventory or change product prices.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateIndex, CreateTable

from app.database.models import DealScanReference


TABLES = [DealScanReference.__table__]


def _sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("A file-backed SQLite URL is required for SQLite backup.")
    return Path(url.database).expanduser().resolve()


def preview(database_url: str) -> str:
    dialect = make_url(database_url).get_dialect()()
    statements: list[str] = []
    for table in TABLES:
        statements.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        statements.extend(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";" for index in table.indexes)
    return "\n\n".join(statements)


def apply(database_url: str, backup_path: str | None) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        source = _sqlite_path(database_url)
        if not source.is_file():
            raise FileNotFoundError(f"SQLite database does not exist: {source}")
        if not backup_path:
            raise ValueError("SQLite --apply requires --backup-path.")
        destination = Path(backup_path).expanduser().resolve()
        if destination == source:
            raise ValueError("Backup path must differ from the database path.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if destination.stat().st_size != source.stat().st_size:
            raise RuntimeError("SQLite backup verification failed.")
    db_engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with db_engine.begin() as connection:
            DealScanReference.metadata.create_all(bind=connection, tables=TABLES)
    finally:
        db_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--apply", action="store_true", help="Create the additive table.")
    parser.add_argument("--backup-path", help="Required verified backup destination for SQLite apply.")
    args = parser.parse_args()
    if not args.apply:
        print("PREVIEW ONLY: no database connection or change was made.")
        print(preview(args.database_url))
        return 0
    apply(args.database_url, args.backup_path)
    print("Deal Scanner reference table created successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
