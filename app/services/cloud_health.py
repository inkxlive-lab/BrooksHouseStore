from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import APP_ENV, STORAGE_ROOT, database_backend
from app.database.connection import engine


def _storage_check() -> bool:
    marker = Path(STORAGE_ROOT) / ".brookshouse-health-check"
    try:
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def install_cloud_health(app: FastAPI) -> None:
    @app.get("/api/health", include_in_schema=False)
    def cloud_health():
        database_ok = False
        try:
            with engine.connect() as connection:
                database_ok = connection.execute(text("SELECT 1")).scalar() == 1
        except Exception:
            database_ok = False

        storage_ok = _storage_check()
        healthy = database_ok and storage_ok
        payload = {
            "status": "ok" if healthy else "degraded",
            "database": "ok" if database_ok else "unavailable",
            "database_backend": database_backend(),
            "storage": "ok" if storage_ok else "unavailable",
            "environment": APP_ENV,
            "release": os.getenv("BROOKSHOUSE_RELEASE", "local"),
        }
        return JSONResponse(payload, status_code=200 if healthy else 503)
