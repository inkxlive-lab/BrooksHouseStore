"""Single sanitized database target resolver for application and inventory code."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import DATABASE_URL, PROJECT_ROOT


MISMATCH_ERROR = "Inventory engine database target does not match application database."
SQLITE_ONLY_ERROR = "Channel inventory tooling is not compatible with the configured non-SQLite database backend."


class DatabaseResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseTarget:
    backend: str
    sanitized_target: str
    sqlite_path: Path | None

    def as_dict(self) -> dict:
        value = asdict(self)
        value["sqlite_path"] = str(self.sqlite_path) if self.sqlite_path else None
        return value


def resolve_database_url(database_url: str) -> DatabaseTarget:
    url = make_url(str(database_url or "").strip())
    backend = url.get_backend_name().casefold()
    if backend == "sqlite":
        database = str(url.database or "").strip()
        if not database or database == ":memory:":
            return DatabaseTarget("sqlite", "sqlite:///:memory:", None)
        path = Path(database).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path = path.resolve()
        return DatabaseTarget("sqlite", f"sqlite:///{path.as_posix()}", path)
    host = str(url.host or "").strip()
    port = f":{url.port}" if url.port else ""
    database = f"/{url.database}" if url.database else ""
    return DatabaseTarget(backend or "other", f"{backend or 'other'}://{host}{port}{database}", None)


def configured_application_database() -> DatabaseTarget:
    return resolve_database_url(DATABASE_URL)


def configured_sqlite_path() -> Path:
    target = configured_application_database()
    if target.backend != "sqlite" or target.sqlite_path is None:
        raise DatabaseResolutionError(SQLITE_ONLY_ERROR)
    return target.sqlite_path


def resolve_sqlite_path(database: str | Path) -> Path:
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def database_alignment(engine_database: str | Path | None = None) -> dict:
    application = configured_application_database()
    engine_path = resolve_sqlite_path(engine_database) if engine_database is not None else application.sqlite_path
    matches = application.backend == "sqlite" and application.sqlite_path is not None and engine_path == application.sqlite_path
    return {"application":application.as_dict(),
            "inventory_engine":{"backend":"sqlite","sanitized_target":
                                f"sqlite:///{engine_path.as_posix()}" if engine_path else None,
                                "sqlite_path":str(engine_path) if engine_path else None},
            "matches":matches,"error":"" if matches else MISMATCH_ERROR}


def require_application_database_match(engine_database: str | Path | None = None) -> Path:
    application = configured_application_database()
    if application.backend != "sqlite" or application.sqlite_path is None:
        raise DatabaseResolutionError(SQLITE_ONLY_ERROR)
    engine_path = resolve_sqlite_path(engine_database) if engine_database is not None else application.sqlite_path
    if engine_path != application.sqlite_path:
        raise DatabaseResolutionError(MISMATCH_ERROR)
    return engine_path


def connect_sqlite_read_only(database: str | Path, *, require_application_match: bool = False) -> sqlite3.Connection:
    path = (require_application_database_match(database) if require_application_match
            else resolve_sqlite_path(database))
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection
