from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.database_resolution import configured_application_database, require_application_database_match


SYNC_INTERVAL_SECONDS = 300
STALE_AFTER_MINUTES = 15
_WORKER_LOCK = threading.Lock()
_WORKER_STATE: dict[str, Any] = {
    "running": False, "started_at": None, "last_cycle_at": None, "last_error": None,
}
_WORKER_STOP = threading.Event()
_WORKER_THREAD: threading.Thread | None = None


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
        """
    )


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
    result: dict[str, Any] = {"worker": dict(_WORKER_STATE), "channels": {}}
    with closing(connect(database, allow_fixture=allow_fixture)) as connection:
        ensure_marketplace_operations_schema(connection)
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
            result["channels"][channel] = {
                "last_success": dict(success) if success else None,
                "last_failure": dict(failure) if failure else None,
                "age_minutes": age,
                "stale": age is None or age > stale_after_minutes,
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


def _has_completed_sync_run() -> bool:
    with closing(connect()) as connection:
        ensure_marketplace_operations_schema(connection)
        return connection.execute(
            "SELECT 1 FROM marketplace_sync_runs WHERE finished_at IS NOT NULL LIMIT 1"
        ).fetchone() is not None


def run_sync_cycle() -> dict[str, Any]:
    from app.walmart_order_service import sync_orders
    from amazon_order_history_sync import sync_recent_orders
    initial_activation = not _has_completed_sync_run()
    results = {}
    for channel, callback in (("walmart", lambda: sync_orders(3, detailed=True)),
                              ("amazon", lambda: sync_recent_orders(days=3))):
        try:
            results[channel] = callback()
        except Exception as error:
            results[channel] = {"success": False, "error": f"{type(error).__name__}: {error}"}
    from app.services.web_push_notifications import send_notification
    if initial_activation:
        prepare_initial_catchup_summary()
    deliver_catchup_summary(send_notification)
    deliver_pending_pushes(send_notification)
    _WORKER_STATE["last_cycle_at"] = _now()
    return results


def worker_loop(stop_event: threading.Event | None = None, interval_seconds: int = SYNC_INTERVAL_SECONDS) -> None:
    stop_event = stop_event or threading.Event()
    _WORKER_STATE.update({"running": True, "started_at": _now()})
    try:
        while not stop_event.is_set():
            try:
                run_sync_cycle()
                _WORKER_STATE["last_error"] = None
            except Exception as error:
                _WORKER_STATE["last_error"] = f"{type(error).__name__}: {error}"[:1000]
            stop_event.wait(interval_seconds)
    finally:
        _WORKER_STATE["running"] = False


def start_worker() -> bool:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_STATE["running"]:
            return False
        _WORKER_STOP.clear()
        _WORKER_STATE.update({"running": True, "started_at": _now()})
        _WORKER_THREAD = threading.Thread(
            target=worker_loop, args=(_WORKER_STOP,), name="brookshouse-marketplace-sync", daemon=True,
        )
        _WORKER_THREAD.start()
        return True


def stop_worker(timeout: float = 10.0) -> bool:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        thread = _WORKER_THREAD
        if thread is None:
            return False
        _WORKER_STOP.set()
    thread.join(timeout=timeout)
    if not thread.is_alive():
        _WORKER_THREAD = None
        return True
    return False
