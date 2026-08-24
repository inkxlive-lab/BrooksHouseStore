from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def inspect(path: Path) -> dict:
    result = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return result
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        result.update({
            "size_bytes": path.stat().st_size,
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "has_auth_schema": "app_users" in tables,
        })
        if "app_users" not in tables:
            return result
        users = []
        for row in connection.execute(
            "SELECT user_id,username,active,role,failed_attempts,locked_until,password_hash,last_login_at "
            "FROM app_users ORDER BY user_id"
        ):
            stored = str(row["password_hash"] or "")
            parts = stored.split("$")
            users.append({
                "user_id": row["user_id"],
                "normalized_username": str(row["username"] or "").strip().lower(),
                "stored_username_is_normalized": row["username"] == str(row["username"] or "").strip().lower(),
                "active": bool(row["active"]), "role": row["role"],
                "failed_attempts": row["failed_attempts"], "locked_until": row["locked_until"],
                "last_login_at": row["last_login_at"], "password_hash_present": bool(stored),
                "password_algorithm": parts[0] if parts else None,
                "password_parameter_metadata": parts[1:4] if len(parts) >= 4 else [],
            })
        result["users"] = users
        result["user_count"] = len(users)
        audits = []
        if "app_access_audit" in tables:
            for row in connection.execute(
                "SELECT audit_id,event_type,path,method,details,ip_address,created_at "
                "FROM app_access_audit WHERE event_type IN ('login_failed','login_success') "
                "ORDER BY audit_id DESC LIMIT 20"
            ):
                item = dict(row)
                item["ip_present"] = bool(item.pop("ip_address", ""))
                audits.append(item)
        result["recent_login_audits"] = audits
        result["login_audit_count"] = connection.execute(
            "SELECT COUNT(*) FROM app_access_audit WHERE event_type IN ('login_failed','login_success')"
        ).fetchone()[0] if "app_access_audit" in tables else 0
        return result
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    output = {
        "configured_database_is_sqlite": database_url.startswith("sqlite:"),
        "configured_database_targets_volume": "/data/app-data/" in database_url,
        "databases": [inspect(path) for path in args.paths],
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
