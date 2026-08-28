from __future__ import annotations

import json
import logging
import os
import random
import socket
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.database_resolution import configured_application_database, require_application_database_match


SYNC_INTERVAL_SECONDS = 300
STARTUP_DELAY_SECONDS = 60
STALE_AFTER_MINUTES = 15
_WORKER_LOCK = threading.Lock()
_SYNC_CYCLE_LOCK = threading.Lock()
_WORKER_STATE: dict[str, Any] = {
    "running": False, "started_at": None, "last_cycle_at": None, "last_error": None,
}
_WORKER_STOP = threading.Event()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_OWNER: str | None = None
LOGGER = logging.getLogger("brookshouse.marketplace_sync")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path(database: str | Path | None = None, *, allow_fixture: bool = False) -> Path:
    if database is None:
        return require_application_database_match()
    path = Path(database).expanduser().resolve()
    if allow_fixture:
        return path
    return require_application_database_match(path)


def connect(database: str | Path | None = None, *, allow_fixture: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(database, allow_fixture=allow_fixture), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def ensure_marketplace_operations_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS marketplace_sync_runs (
            sync_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            success INTEGER,
            orders_discovered INTEGER NOT NULL DEFAULT 0,
            new_orders_inserted INTEGER NOT NULL DEFAULT 0,
            orders_updated INTEGER NOT NULL DEFAULT 0,
            lines_processed INTEGER NOT NULL DEFAULT 0,
            sanitized_database_target TEXT NOT NULL,
            error_message TEXT,
            worker_identity TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_marketplace_sync_runs_channel_time
            ON marketplace_sync_runs(channel, started_at DESC);
        CREATE TABLE IF NOT EXISTS marketplace_order_alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            marketplace_order_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            marketplace_status TEXT,
            acknowledged_at TEXT,
            acknowledged_by TEXT,
            alert_state TEXT NOT NULL DEFAULT 'new',
            push_notified_at TEXT,
            push_error TEXT,
            UNIQUE(channel, marketplace_order_id)
        );
        CREATE INDEX IF NOT EXISTS ix_marketplace_order_alerts_state
            ON marketplace_order_alerts(alert_state, channel, first_seen_at DESC);
        CREATE TABLE IF NOT EXISTS marketplace_status_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            marketplace_order_id TEXT NOT NULL,
            old_marketplace_status TEXT,
            new_marketplace_status TEXT,
            old_local_status TEXT,
            new_local_status TEXT,
            channel_response TEXT,
            sync_run_id INTEGER,
            synchronized_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_marketplace_status_audit_order
            ON marketplace_status_audit(channel, marketplace_order_id, synchronized_at DESC);
        CREATE TABLE IF NOT EXISTS operations_report_runs (
            report_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT,
            filters_json TEXT NOT NULL,
            freshness_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            totals_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_operations_report_runs_created
            ON operations_report_runs(created_at DESC);
        CREATE TABLE IF NOT EXISTS operations_report_jobs (
            report_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            mode TEXT NOT NULL,
            state TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            requested_by TEXT,
            filters_json TEXT NOT NULL,
            progress_message TEXT,
            result_report_run_id INTEGER,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_operations_report_jobs_state
            ON operations_report_jobs(state, updated_at DESC);
        CREATE TABLE IF NOT EXISTS marketplace_operation_locks (
            lock_name TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            details TEXT
        );
        """
    )
    for table, columns in {
        "walmart_orders": {"channel_closed_at": "TEXT", "last_verified_at": "TEXT", "terminal_reason": "TEXT"},
        "amazon_order_history": {"local_status": "TEXT NOT NULL DEFAULT 'new'", "channel_closed_at": "TEXT", "last_verified_at": "TEXT", "terminal_reason": "TEXT", "raw_json": "TEXT", "ship_by_date": "TEXT"},
    }.items():
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def normalized_channel_status(status: str) -> str:
    return str(status or "").strip().casefold().replace("_", "").replace(" ", "")


def terminal_local_status(status: str) -> str | None:
    values = [normalized_channel_status(part) for part in str(status or "").split(",") if part.strip()]
    if not values:
        return None
    def terminal_kind(value: str) -> str | None:
        if value in {"cancelled", "canceled", "cancel", "voided"}:
            return "cancelled"
        if value in {"refunded", "refund"}:
            return "refunded"
        if value in {"shipped", "completed", "complete", "delivered"}:
            return "shipped"
        if value in {"closed", "rejected", "unfulfillable"}:
            return "closed"
        return None
    kinds = [terminal_kind(value) for value in values]
    if any(kind is None for kind in kinds):
        return None
    if "cancelled" in kinds:
        return "cancelled"
    if "refunded" in kinds:
        return "refunded"
    if "shipped" in kinds:
        return "shipped"
    return "closed"


def reconcile_order_status(connection: sqlite3.Connection, *, channel: str, order_id: str,
                           marketplace_status: str, channel_response: Any,
                           sync_run_id: int | None = None) -> dict[str, Any]:
    """Apply authoritative channel lifecycle state without overwriting open internal stages."""
    channel = channel.casefold()
    now = _now()
    if channel == "walmart":
        table, key, market_column = "walmart_orders", "purchase_order_id", "walmart_status"
    elif channel == "amazon":
        table, key, market_column = "amazon_order_history", "amazon_order_id", "fulfillment_status"
    else:
        raise ValueError(f"Unsupported marketplace channel: {channel}")
    row = connection.execute(f'SELECT {market_column},local_status FROM {table} WHERE {key}=?', (str(order_id),)).fetchone()
    if row is None:
        return {"changed": False, "terminal": False, "reason": "not_found"}
    old_market, old_local = str(row[0] or ""), str(row[1] or "new")
    terminal = terminal_local_status(marketplace_status)
    new_local = terminal or old_local
    connection.execute(
        f'''UPDATE {table} SET {market_column}=?,local_status=?,last_verified_at=?,
               channel_closed_at=CASE WHEN ? IS NOT NULL THEN COALESCE(channel_closed_at,?) ELSE channel_closed_at END,
               terminal_reason=CASE WHEN ? IS NOT NULL THEN ? ELSE terminal_reason END WHERE {key}=?''',
        (marketplace_status, new_local, now, terminal, now, terminal, marketplace_status, str(order_id)),
    )
    changed = old_market != str(marketplace_status or "") or old_local != new_local
    if changed:
        response_text = json.dumps(channel_response, default=str, separators=(",", ":"))
        connection.execute(
            """INSERT INTO marketplace_status_audit
               (channel,marketplace_order_id,old_marketplace_status,new_marketplace_status,
                old_local_status,new_local_status,channel_response,sync_run_id,synchronized_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (channel, str(order_id), old_market, marketplace_status, old_local, new_local,
             response_text, sync_run_id, now),
        )
    if terminal:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations_work_queue'").fetchone():
            connection.execute(
                """UPDATE operations_work_queue SET status='cancelled',updated_at=?
                   WHERE source_channel=? AND (source_reference=? OR source_reference LIKE ?)
                     AND status NOT IN ('completed','cancelled','closed')""",
                (now, channel, str(order_id), f"{order_id}|%"),
            )
        connection.execute("UPDATE marketplace_order_alerts SET alert_state='closed' WHERE channel=? AND marketplace_order_id=?", (channel, str(order_id)))
    return {"changed": changed, "terminal": bool(terminal), "local_status": new_local, "old_local_status": old_local}


def release_cancelled_allocations(database: str | Path, channel: str, order_id: str) -> list[dict]:
    """Release only through the existing idempotent allocation/ledger engine."""
    results: list[dict] = []
    connection = sqlite3.connect(database, timeout=30)
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_inventory_allocations'").fetchone():
            return results
        line_column = "order_line_id"
        rows = connection.execute(
            "SELECT order_line_id FROM channel_inventory_allocations WHERE channel_name=? AND order_id=? AND status NOT IN ('cancelled','restocked')",
            (channel.casefold(), str(order_id)),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return results
    from app.services.channel_inventory_engine import cancel_before_fulfillment_to_copy
    for row in rows:
        results.append(cancel_before_fulfillment_to_copy(database, channel, str(order_id), str(row[0])))
    return results


def _sanitized_target() -> str:
    return configured_application_database().sanitized_target


def begin_sync_run(connection: sqlite3.Connection, channel: str) -> int:
    ensure_marketplace_operations_schema(connection)
    cursor = connection.execute(
        """INSERT INTO marketplace_sync_runs
           (channel,started_at,sanitized_database_target,worker_identity)
           VALUES(?,?,?,?)""",
        (channel.casefold(), _now(), _sanitized_target(), f"{socket.gethostname()}:{os.getpid()}"),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_sync_run(connection: sqlite3.Connection, run_id: int, *, success: bool,
                    orders_discovered: int = 0, new_orders_inserted: int = 0,
                    orders_updated: int = 0, lines_processed: int = 0,
                    error_message: str = "") -> None:
    connection.execute(
        """UPDATE marketplace_sync_runs SET finished_at=?,success=?,orders_discovered=?,
           new_orders_inserted=?,orders_updated=?,lines_processed=?,error_message=?
           WHERE sync_run_id=?""",
        (_now(), int(success), orders_discovered, new_orders_inserted, orders_updated,
         lines_processed, str(error_message or "")[:1000] or None, run_id),
    )
    connection.commit()


def qualify_order(channel: str, marketplace_status: str, fulfilled_by: str = "") -> bool:
    status = str(marketplace_status or "").strip().casefold().replace("_", "")
    if channel.casefold() == "walmart":
        return status in {"created", "acknowledged"}
    return status == "unshipped" and str(fulfilled_by or "").strip().casefold() == "merchant"


def register_order_alert(connection: sqlite3.Connection, channel: str, order_id: str,
                         marketplace_status: str) -> bool:
    ensure_marketplace_operations_schema(connection)
    cursor = connection.execute(
        """INSERT OR IGNORE INTO marketplace_order_alerts
           (channel,marketplace_order_id,first_seen_at,marketplace_status,alert_state)
           VALUES(?,?,?,?, 'new')""",
        (channel.casefold(), str(order_id), _now(), marketplace_status),
    )
    if cursor.rowcount == 0:
        connection.execute(
            """UPDATE marketplace_order_alerts SET marketplace_status=?
               WHERE channel=? AND marketplace_order_id=?""",
            (marketplace_status, channel.casefold(), str(order_id)),
        )
    return cursor.rowcount == 1


def create_picking_task(connection: sqlite3.Connection, *, channel: str, order_id: str,
                        line_id: str, sku: str, quantity: int,
                        product_id: int | None = None) -> None:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations_work_queue'"
    ).fetchone()
    if not table:
        return
    now = _now()
    details = json.dumps({"channel": channel, "order_id": order_id, "line_id": line_id,
                          "sku": sku, "quantity": int(quantity), "inventory_mutation": False},
                         separators=(",", ":"))
    connection.execute(
        """INSERT INTO operations_work_queue
           (task_key,task_type,title,details,priority,status,source_channel,source_reference,
            product_id,requested_quantity,created_at,updated_at)
           VALUES(?,?,?,?,?,'open',?,?,?,?,?,?)
           ON CONFLICT(task_key) DO UPDATE SET details=excluded.details,
             requested_quantity=excluded.requested_quantity,updated_at=excluded.updated_at""",
        (f"marketplace-pick:{channel}:{order_id}:{line_id}", "marketplace_pick",
         f"Pick {channel.title()} order {order_id}", details, "high", channel,
         f"{order_id}|{line_id}", product_id, int(quantity), now, now),
    )


def alert_counts(database: str | Path | None = None, *, allow_fixture: bool = False) -> dict[str, int]:
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        rows = connection.execute(
            """SELECT channel,COUNT(*) count FROM marketplace_order_alerts
               WHERE alert_state='new' AND acknowledged_at IS NULL GROUP BY channel"""
        ).fetchall()
        counts = {"walmart": 0, "amazon": 0}
        counts.update({str(row["channel"]): int(row["count"]) for row in rows})
        counts["total"] = counts["walmart"] + counts["amazon"]
        return counts


def mark_alert_reviewed(alert_id: int, actor: str, database: str | Path | None = None,
                        *, allow_fixture: bool = False) -> bool:
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        cursor = connection.execute(
            """UPDATE marketplace_order_alerts SET acknowledged_at=?,acknowledged_by=?,alert_state='reviewed'
               WHERE alert_id=? AND acknowledged_at IS NULL""", (_now(), actor[:100], int(alert_id)))
        connection.commit()
        return cursor.rowcount == 1


def sync_health(database: str | Path | None = None, *, allow_fixture: bool = False,
                 stale_after_minutes: int = STALE_AFTER_MINUTES) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    central = ZoneInfo("America/Chicago")
    result: dict[str, Any] = {"worker": dict(_WORKER_STATE), "channels": {}}
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        running = connection.execute(
            "SELECT 1 FROM marketplace_operation_locks WHERE lock_name='marketplace_refresh' "
            "AND expires_at > ? LIMIT 1", (now.isoformat(),)
        ).fetchone() is not None
        for channel in ("walmart", "amazon"):
            success = connection.execute(
                """SELECT * FROM marketplace_sync_runs WHERE channel=? AND success=1
                   ORDER BY sync_run_id DESC LIMIT 1""", (channel,)).fetchone()
            failure = connection.execute(
                """SELECT * FROM marketplace_sync_runs WHERE channel=? AND success=0
                   ORDER BY sync_run_id DESC LIMIT 1""", (channel,)).fetchone()
            age = None
            if success and success["finished_at"]:
                age = max(0, int((now - datetime.fromisoformat(success["finished_at"])).total_seconds() // 60))
            latest = max(
                (row for row in (success, failure) if row is not None),
                key=lambda row: int(row["sync_run_id"]),
                default=None,
            )
            stale = age is None or age > stale_after_minutes
            error = str(failure["error_message"] or "") if failure else ""
            if running:
                state = "refresh_running"
            elif latest is None:
                state = "never_synchronized"
            elif not latest["success"]:
                lowered = error.casefold()
                state = "authentication_failure" if any(
                    token in lowered for token in ("401", "403", "auth", "credential", "token")
                ) else "api_failure"
            elif stale:
                state = "stale"
            else:
                state = "last_refresh_succeeded"

            def display(value):
                if not value:
                    return None
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(central).strftime("%a %m/%d/%Y %I:%M:%S %p CT")

            result["channels"][channel] = {
                "last_success": dict(success) if success else None,
                "last_failure": dict(failure) if failure else None,
                "last_success_display": display(success["finished_at"]) if success else None,
                "last_success_utc": success["finished_at"] if success else None,
                "last_failure_display": display(failure["finished_at"]) if failure else None,
                "age_minutes": age,
                "stale": stale,
                "refresh_running": running,
                "state": state,
                "state_label": state.replace("_", " ").title(),
                "latest_error": error or None,
            }
    result["stale"] = any(row["stale"] for row in result["channels"].values())
    return result


def deliver_pending_pushes(send: Callable[..., dict[str, int]], database: str | Path | None = None,
                           *, allow_fixture: bool = False) -> int:
    delivered_alerts = 0
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        alerts = connection.execute(
            """SELECT * FROM marketplace_order_alerts
               WHERE alert_state='new' AND push_notified_at IS NULL
                 AND COALESCE(push_error,'')<>'Initial catch-up summary pending'
               ORDER BY alert_id"""
        ).fetchall()
        for alert in alerts:
            channel = str(alert["channel"]).title()
            try:
                result = send(f"New {channel} order", f"Order {alert['marketplace_order_id']} is ready for review.",
                              "/channels/orders?alert_state=new", f"{alert['channel']}_new_order")
                if int(result.get("delivered", 0)) > 0:
                    connection.execute(
                        "UPDATE marketplace_order_alerts SET push_notified_at=?,push_error=NULL WHERE alert_id=?",
                        (_now(), alert["alert_id"]),
                    )
                    delivered_alerts += 1
                else:
                    connection.execute(
                        "UPDATE marketplace_order_alerts SET push_error=? WHERE alert_id=?",
                        ("No active push subscription accepted the notification", alert["alert_id"]),
                    )
            except Exception as error:
                connection.execute(
                    "UPDATE marketplace_order_alerts SET push_error=? WHERE alert_id=?",
                    (f"{type(error).__name__}: {error}"[:500], alert["alert_id"]),
                )
            connection.commit()
    return delivered_alerts


def prepare_initial_catchup_summary(database: str | Path | None = None, *, allow_fixture: bool = False) -> int:
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        cursor = connection.execute(
            """UPDATE marketplace_order_alerts
               SET push_error='Initial catch-up summary pending'
               WHERE alert_state='new' AND push_notified_at IS NULL"""
        )
        connection.commit()
        return cursor.rowcount


def deliver_catchup_summary(send: Callable[..., dict[str, int]], database: str | Path | None = None,
                            *, allow_fixture: bool = False) -> int:
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        rows = connection.execute(
            """SELECT channel,COUNT(*) count FROM marketplace_order_alerts
               WHERE alert_state='new' AND push_notified_at IS NULL
                 AND push_error='Initial catch-up summary pending' GROUP BY channel"""
        ).fetchall()
        counts = {str(row["channel"]): int(row["count"]) for row in rows}
        total = sum(counts.values())
        if not total:
            return 0
        result = send(
            f"{total} marketplace orders need attention",
            f"{counts.get('walmart', 0)} Walmart, {counts.get('amazon', 0)} Amazon",
            "/channels/orders?alert_state=new", "marketplace_catchup_summary",
        )
        if int(result.get("delivered", 0)) > 0:
            connection.execute(
                """UPDATE marketplace_order_alerts SET push_notified_at=?,push_error=NULL
                   WHERE alert_state='new' AND push_notified_at IS NULL
                     AND push_error='Initial catch-up summary pending'""", (_now(),)
            )
            connection.commit()
            return total
        connection.commit()
        return 0


def _has_completed_sync_run(database: str | Path | None = None, *, allow_fixture: bool = False) -> bool:
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        return connection.execute(
            "SELECT 1 FROM marketplace_sync_runs WHERE finished_at IS NOT NULL LIMIT 1"
        ).fetchone() is not None


def acquire_operation_lock(lock_name: str, owner_token: str, *, ttl_seconds: int = 900,
                           database: str | Path | None = None, allow_fixture: bool = False,
                           details: str = "") -> bool:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(30, int(ttl_seconds)))
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM marketplace_operation_locks WHERE expires_at <= ?", (now.isoformat(),))
        row = connection.execute("SELECT owner_token FROM marketplace_operation_locks WHERE lock_name=?", (lock_name,)).fetchone()
        if row and row["owner_token"] != owner_token:
            connection.rollback()
            return False
        connection.execute(
            "INSERT INTO marketplace_operation_locks(lock_name,owner_token,acquired_at,heartbeat_at,expires_at,details) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(lock_name) DO UPDATE SET owner_token=excluded.owner_token,"
            "heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at,details=excluded.details",
            (lock_name, owner_token, now.isoformat(), now.isoformat(), expires.isoformat(), details[:500]),
        )
        connection.commit()
        return True


def release_operation_lock(lock_name: str, owner_token: str, *, database: str | Path | None = None,
                           allow_fixture: bool = False) -> None:
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        connection.execute("DELETE FROM marketplace_operation_locks WHERE lock_name=? AND owner_token=?",
                           (lock_name, owner_token))
        connection.commit()


def run_locked_daily_recap(callback: Callable[[], Any], *, database: str | Path | None = None,
                           allow_fixture: bool = False) -> bool:
    """Run one recap check under the shared durable operation lock."""
    owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    if not acquire_operation_lock(
        "daily_recap_notifications", owner, ttl_seconds=120,
        database=database, allow_fixture=allow_fixture,
        details="scheduled daily recap check",
    ):
        return False
    try:
        callback()
        return True
    finally:
        release_operation_lock(
            "daily_recap_notifications", owner,
            database=database, allow_fixture=allow_fixture,
        )


def run_sync_cycle(channels: list[str] | tuple[str, ...] | None = None, *, database: str | Path | None = None,
                   allow_fixture: bool = False) -> dict[str, Any]:
    from app.walmart_order_service import sync_orders
    from amazon_order_history_sync import sync_recent_orders
    initial_activation = not _has_completed_sync_run(database, allow_fixture=allow_fixture)
    requested = {str(channel).casefold() for channel in (channels or ("walmart", "amazon"))}
    unsupported = requested - {"walmart", "amazon"}
    if unsupported:
        raise ValueError(f"Unsupported marketplace channels: {sorted(unsupported)}")
    if not _SYNC_CYCLE_LOCK.acquire(blocking=False):
        return {channel: {"success": False, "busy": True, "error": "A refresh cycle is already active in this process"}
                for channel in sorted(requested)}
    owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    try:
        acquired = acquire_operation_lock(
            "marketplace_refresh", owner,
            ttl_seconds=max(300, int(os.getenv("MARKETPLACE_REFRESH_LOCK_SECONDS", "900"))),
            database=database, allow_fixture=allow_fixture, details=",".join(sorted(requested)),
        )
    except Exception:
        _SYNC_CYCLE_LOCK.release()
        raise
    if not acquired:
        _SYNC_CYCLE_LOCK.release()
        return {channel: {"success": False, "busy": True, "error": "Another marketplace refresh is already running"}
                for channel in sorted(requested)}
    try:
        callbacks = {"walmart": lambda: sync_orders(3, database=database, allow_fixture=allow_fixture, detailed=True),
                     "amazon": lambda: sync_recent_orders(days=3, database=database, allow_fixture=allow_fixture)}
        results = {}
        for channel in ("walmart", "amazon"):
            if channel not in requested:
                continue
            try:
                results[channel] = callbacks[channel]()
            except Exception as error:
                results[channel] = {"success": False, "error": f"{type(error).__name__}: {error}"}
        from app.services.web_push_notifications import send_notification
        if initial_activation:
            prepare_initial_catchup_summary(database, allow_fixture=allow_fixture)
        deliver_catchup_summary(send_notification, database, allow_fixture=allow_fixture)
        deliver_pending_pushes(send_notification, database, allow_fixture=allow_fixture)
        _WORKER_STATE["last_cycle_at"] = _now()
        return results
    finally:
        release_operation_lock("marketplace_refresh", owner, database=database, allow_fixture=allow_fixture)
        _SYNC_CYCLE_LOCK.release()


def _channel_due_times(*, database: str | Path | None = None, allow_fixture: bool = False,
                       interval_seconds: int = SYNC_INTERVAL_SECONDS,
                       not_before: datetime | None = None) -> dict[str, datetime]:
    """Return durable per-channel due times with bounded failure backoff and jitter."""
    now = datetime.now(timezone.utc)
    floor = not_before or now
    due: dict[str, datetime] = {}
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
        for channel in ("walmart", "amazon"):
            rows = connection.execute(
                "SELECT success,finished_at FROM marketplace_sync_runs "
                "WHERE channel=? AND finished_at IS NOT NULL ORDER BY sync_run_id DESC LIMIT 8",
                (channel,),
            ).fetchall()
            if not rows:
                due[channel] = floor
                continue
            latest = datetime.fromisoformat(str(rows[0]["finished_at"]).replace("Z", "+00:00"))
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            failures = 0
            for row in rows:
                if bool(row["success"]):
                    break
                failures += 1
            multiplier = min(64, 2 ** failures) if failures else 1
            jitter = random.uniform(0.0, min(30.0, interval_seconds * 0.20)) if failures else 0.0
            scheduled = latest + timedelta(seconds=(interval_seconds * multiplier) + jitter)
            due[channel] = max(floor, scheduled)
    return due


def worker_loop(stop_event: threading.Event | None = None, interval_seconds: int = SYNC_INTERVAL_SECONDS,
                *, database: str | Path | None = None, allow_fixture: bool = False,
                startup_delay_seconds: int = STARTUP_DELAY_SECONDS,
                worker_owner: str | None = None) -> None:
    stop_event = stop_event or threading.Event()
    _WORKER_STATE.update({"running": True, "started_at": _now()})
    startup_floor = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(startup_delay_seconds)))
    singleton_owner = worker_owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    LOGGER.info("Marketplace sync worker started with %s-second interval and %s-second startup delay",
                interval_seconds, startup_delay_seconds)
    try:
        while not stop_event.is_set():
            try:
                if not acquire_operation_lock(
                    "marketplace_worker_singleton", singleton_owner, ttl_seconds=120,
                    database=database, allow_fixture=allow_fixture, details="background marketplace scheduler",
                ):
                    _WORKER_STATE["last_error"] = "Another marketplace worker singleton is active"
                    LOGGER.warning(_WORKER_STATE["last_error"])
                    return
                due_times = _channel_due_times(
                    database=database, allow_fixture=allow_fixture,
                    interval_seconds=interval_seconds, not_before=startup_floor,
                )
                now = datetime.now(timezone.utc)
                due_channels = [channel for channel, due_at in due_times.items() if due_at <= now]
                if due_channels:
                    results = run_sync_cycle(due_channels, database=database, allow_fixture=allow_fixture)
                    _WORKER_STATE["last_error"] = None
                    LOGGER.info("Marketplace sync cycle completed for %s: %s", due_channels, results)
                    startup_floor = now
                    continue
                wait_seconds = min(30.0, max(0.1, min(
                    (due_at - now).total_seconds() for due_at in due_times.values()
                )))
                stop_event.wait(wait_seconds)
            except Exception as error:
                _WORKER_STATE["last_error"] = f"{type(error).__name__}: {error}"[:1000]
                LOGGER.exception("Marketplace sync worker cycle failed")
                stop_event.wait(min(30.0, max(1.0, interval_seconds)))
    finally:
        try:
            release_operation_lock("marketplace_worker_singleton", singleton_owner,
                                   database=database, allow_fixture=allow_fixture)
        except Exception:
            LOGGER.exception("Marketplace worker singleton lock cleanup failed")
        _WORKER_STATE["running"] = False
        LOGGER.info("Marketplace sync worker stopped")


def start_worker(*, database: str | Path | None = None, allow_fixture: bool = False,
                 interval_seconds: int = SYNC_INTERVAL_SECONDS,
                 startup_delay_seconds: int = STARTUP_DELAY_SECONDS) -> bool:
    global _WORKER_THREAD, _WORKER_OWNER
    with _WORKER_LOCK:
        if _WORKER_STATE["running"] or (_WORKER_THREAD is not None and _WORKER_THREAD.is_alive()):
            return False
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        if not acquire_operation_lock(
            "marketplace_worker_singleton", owner, ttl_seconds=120,
            database=database, allow_fixture=allow_fixture, details="background marketplace scheduler startup",
        ):
            LOGGER.warning("Marketplace worker start refused because another singleton is active")
            return False
        _WORKER_STOP.clear()
        _WORKER_STATE.update({"running": True, "started_at": _now()})
        _WORKER_OWNER = owner
        _WORKER_THREAD = threading.Thread(
            target=worker_loop, args=(_WORKER_STOP, interval_seconds),
            kwargs={"database": database, "allow_fixture": allow_fixture,
                    "startup_delay_seconds": startup_delay_seconds, "worker_owner": owner},
            name="brookshouse-marketplace-sync", daemon=True,
        )
        _WORKER_THREAD.start()
        return True


def stop_worker(timeout: float = 10.0) -> bool:
    global _WORKER_THREAD, _WORKER_OWNER
    with _WORKER_LOCK:
        thread = _WORKER_THREAD
        if thread is None:
            return False
        _WORKER_STOP.set()
    thread.join(timeout=timeout)
    if not thread.is_alive():
        _WORKER_THREAD = None
        _WORKER_OWNER = None
        return True
    return False
