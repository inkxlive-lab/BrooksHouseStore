from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIRECTORY = PROJECT_ROOT / "app"
DATA_DIRECTORY = APP_DIRECTORY / "data"
LOCAL_ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(LOCAL_ENV_FILE, override=False)

DEFAULT_SQLITE_PATH = DATA_DIRECTORY / "brookshouse_store.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
_storage_value = Path(
    os.getenv("BROOKSHOUSE_STORAGE_ROOT", str(DATA_DIRECTORY))
).expanduser()
STORAGE_ROOT = (
    _storage_value
    if _storage_value.is_absolute()
    else PROJECT_ROOT / _storage_value
)
APP_ENV = os.getenv("BROOKSHOUSE_APP_ENV", "local").strip().lower()
PROCESS_ROLE = os.getenv("BROOKSHOUSE_PROCESS_ROLE", "all").strip().lower()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


BACKGROUND_JOBS_ENABLED = env_flag(
    "BROOKSHOUSE_BACKGROUND_JOBS_ENABLED",
    default=False,
)
POSTGRES_MIGRATION_READY = env_flag(
    "BROOKSHOUSE_POSTGRES_MIGRATION_READY",
    default=False,
)


def database_backend() -> str:
    value = DATABASE_URL.lower()
    if value.startswith("sqlite:"):
        return "sqlite"
    if value.startswith("postgresql:") or value.startswith("postgres:"):
        return "postgresql"
    return "other"


def should_run_background_jobs() -> bool:
    return BACKGROUND_JOBS_ENABLED and PROCESS_ROLE in {"all", "worker"}


def validate_cloud_safety() -> None:
    if database_backend() == "postgresql" and not POSTGRES_MIGRATION_READY:
        raise RuntimeError(
            "PostgreSQL was configured before the BrooksHouse raw-SQL "
            "conversion was marked ready. Keep SQLite active until the "
            "database compatibility phase is completed."
        )


DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
validate_cloud_safety()
