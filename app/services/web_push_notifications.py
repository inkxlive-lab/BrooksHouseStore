from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.database_resolution import configured_sqlite_path, require_application_database_match


APP_DIRECTORY = Path(__file__).resolve().parents[1]
DATA_DIRECTORY = APP_DIRECTORY / "data"
DB_PATH = configured_sqlite_path()
VAPID_PATH = DATA_DIRECTORY / "web_push_vapid.json"
VAPID_PRIVATE_KEY_PATH = DATA_DIRECTORY / "web_push_private.pem"
_SEND_LOCK = threading.Lock()
CENTRAL_TIME = ZoneInfo("America/Chicago")


def _database_path(database=None, *, allow_fixture=False):
    if database is None:
        return require_application_database_match(DB_PATH)
    path = Path(database).expanduser().resolve()
    return path if allow_fixture else require_application_database_match(path)


def ensure_push_tables(database=None, *, allow_fixture=False) -> None:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_database_path(database, allow_fixture=allow_fixture), timeout=30)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                device_name TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_success_at TEXT,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS web_push_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                target_url TEXT,
                delivered_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS web_push_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        subscription_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(web_push_subscriptions)"
            ).fetchall()
        }
        for column, definition in {
            "notify_new_orders": "INTEGER NOT NULL DEFAULT 1",
            "notify_morning": "INTEGER NOT NULL DEFAULT 1",
            "notify_evening": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if column not in subscription_columns:
                connection.execute(
                    f"ALTER TABLE web_push_subscriptions ADD COLUMN {column} {definition}"
                )
        now = datetime.now(CENTRAL_TIME).isoformat()
        for key, value in {
            "morning_enabled": "1",
            "morning_time": "08:00",
            "evening_enabled": "1",
            "evening_time": "19:00",
            "last_morning_date": "",
            "last_evening_date": "",
        }.items():
            connection.execute(
                "INSERT OR IGNORE INTO web_push_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
        connection.commit()
    finally:
        connection.close()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def ensure_vapid_keys() -> dict[str, str]:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if VAPID_PATH.exists():
        payload = json.loads(VAPID_PATH.read_text(encoding="utf-8"))
        VAPID_PRIVATE_KEY_PATH.write_text(
            payload["private_key"],
            encoding="ascii",
        )
        return payload

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()
    public_key = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    payload = {"public_key": _b64url(public_key), "private_key": private_pem}
    temporary_path = VAPID_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary_path, VAPID_PATH)
    VAPID_PRIVATE_KEY_PATH.write_text(private_pem, encoding="ascii")
    return payload


def public_vapid_key() -> str:
    return ensure_vapid_keys()["public_key"]


def save_subscription(subscription: dict[str, Any], device_name: str = "") -> None:
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise ValueError("The browser returned an incomplete push subscription.")
    ensure_push_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.execute(
            """
            INSERT INTO web_push_subscriptions
                (endpoint, p256dh, auth, device_name, active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh=excluded.p256dh,
                auth=excluded.auth,
                device_name=excluded.device_name,
                active=1,
                last_error=NULL
            """,
            (endpoint, p256dh, auth, device_name.strip(), datetime.now().astimezone().isoformat()),
        )
        connection.commit()
    finally:
        connection.close()


def remove_subscription(endpoint: str) -> None:
    ensure_push_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.execute(
            "UPDATE web_push_subscriptions SET active=0 WHERE endpoint=?",
            (endpoint.strip(),),
        )
        connection.commit()
    finally:
        connection.close()


def subscription_summary() -> dict[str, Any]:
    ensure_push_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        devices = connection.execute(
            """
            SELECT subscription_id, device_name, active, created_at,
                   last_success_at, last_error, notify_new_orders,
                   notify_morning, notify_evening
            FROM web_push_subscriptions
            ORDER BY active DESC, created_at DESC
            """
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM web_push_events ORDER BY event_id DESC LIMIT 15"
        ).fetchall()
        return {
            "devices": [dict(row) for row in devices],
            "events": [dict(row) for row in events],
            "active_count": sum(bool(row["active"]) for row in devices),
            "settings": load_push_settings(connection),
        }
    finally:
        connection.close()


def send_notification(
    title: str,
    body: str,
    target_url: str = "/channels/orders",
    event_type: str = "general",
    *,
    database=None,
    allow_fixture: bool = False,
) -> dict[str, int]:
    from pywebpush import WebPushException, webpush

    ensure_push_tables(database, allow_fixture=allow_fixture)
    vapid = ensure_vapid_keys()
    connection = sqlite3.connect(_database_path(database, allow_fixture=allow_fixture), timeout=30)
    connection.row_factory = sqlite3.Row
    delivered = 0
    failed = 0
    payload = json.dumps({"title": title, "body": body, "url": target_url})

    with _SEND_LOCK:
        try:
            preference_column = {
                "walmart_new_order": "notify_new_orders",
                "amazon_new_order": "notify_new_orders",
                "morning_recap": "notify_morning",
                "evening_recap": "notify_evening",
            }.get(event_type)
            where_clause = "active=1"
            if preference_column:
                where_clause += f" AND {preference_column}=1"
            subscriptions = connection.execute(
                f"SELECT * FROM web_push_subscriptions WHERE {where_clause}"
            ).fetchall()
            for subscription in subscriptions:
                subscription_info = {
                    "endpoint": subscription["endpoint"],
                    "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
                }
                try:
                    webpush(
                        subscription_info=subscription_info,
                        data=payload,
                        vapid_private_key=str(VAPID_PRIVATE_KEY_PATH),
                        vapid_claims={"sub": "mailto:notifications@brookshouse.local"},
                        ttl=3600,
                    )
                    delivered += 1
                    connection.execute(
                        "UPDATE web_push_subscriptions SET last_success_at=?, last_error=NULL WHERE subscription_id=?",
                        (datetime.now().astimezone().isoformat(), subscription["subscription_id"]),
                    )
                except WebPushException as error:
                    failed += 1
                    status_code = getattr(getattr(error, "response", None), "status_code", None)
                    deactivate = status_code in {404, 410}
                    connection.execute(
                        "UPDATE web_push_subscriptions SET active=?, last_error=? WHERE subscription_id=?",
                        (0 if deactivate else 1, str(error)[:500], subscription["subscription_id"]),
                    )

            connection.execute(
                """
                INSERT INTO web_push_events
                    (event_type, title, body, target_url, delivered_count, failed_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    title,
                    body,
                    target_url,
                    delivered,
                    failed,
                    datetime.now().astimezone().isoformat(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
    return {"delivered": delivered, "failed": failed}


def load_push_settings(connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    ensure_push_tables() if connection is None else None
    owns_connection = connection is None
    if connection is None:
        connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        values = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT setting_key, setting_value FROM web_push_settings"
            ).fetchall()
        }
        return {
            "morning_enabled": values.get("morning_enabled", "1") == "1",
            "morning_time": values.get("morning_time", "08:00"),
            "evening_enabled": values.get("evening_enabled", "1") == "1",
            "evening_time": values.get("evening_time", "19:00"),
            "last_morning_date": values.get("last_morning_date", ""),
            "last_evening_date": values.get("last_evening_date", ""),
        }
    finally:
        if owns_connection:
            connection.close()


def save_push_settings(
    morning_enabled: bool,
    morning_time: str,
    evening_enabled: bool,
    evening_time: str,
) -> None:
    ensure_push_tables()
    for value in (morning_time, evening_time):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as error:
            raise ValueError("Recap times must use the HH:MM format.") from error
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        now = datetime.now(CENTRAL_TIME).isoformat()
        for key, value in {
            "morning_enabled": "1" if morning_enabled else "0",
            "morning_time": morning_time,
            "evening_enabled": "1" if evening_enabled else "0",
            "evening_time": evening_time,
        }.items():
            connection.execute(
                """
                INSERT INTO web_push_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value,
                    updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
        connection.commit()
    finally:
        connection.close()


def save_device_preferences(
    subscription_id: int,
    notify_new_orders: bool,
    notify_morning: bool,
    notify_evening: bool,
) -> None:
    ensure_push_tables()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.execute(
            """
            UPDATE web_push_subscriptions
            SET notify_new_orders=?, notify_morning=?, notify_evening=?
            WHERE subscription_id=?
            """,
            (
                int(notify_new_orders),
                int(notify_morning),
                int(notify_evening),
                int(subscription_id),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def build_daily_recap(period: str) -> tuple[str, str]:
    ensure_push_tables()
    today = datetime.now(CENTRAL_TIME).date().isoformat()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        order_count = open_orders = completed_orders = units = 0
        gross_sales = 0.0
        if _table_exists(connection, "walmart_orders"):
            orders = connection.execute(
                """
                SELECT purchase_order_id, local_status, COALESCE(order_total, 0) AS order_total
                FROM walmart_orders
                WHERE substr(COALESCE(order_date, synced_at, ''), 1, 10)=?
                """,
                (today,),
            ).fetchall()
            order_count = len(orders)
            completed_orders = sum(
                str(row["local_status"] or "").casefold() in {"shipped", "cancelled"}
                for row in orders
            )
            open_orders = order_count - completed_orders
            gross_sales = sum(float(row["order_total"] or 0) for row in orders)
            if orders and _table_exists(connection, "walmart_order_lines"):
                placeholders = ",".join("?" for _ in orders)
                units = int(
                    connection.execute(
                        f"SELECT COALESCE(SUM(quantity), 0) FROM walmart_order_lines WHERE purchase_order_id IN ({placeholders})",
                        tuple(row["purchase_order_id"] for row in orders),
                    ).fetchone()[0]
                    or 0
                )

        adjustments = 0
        if _table_exists(connection, "inventory_transactions"):
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(inventory_transactions)"
                ).fetchall()
            }
            date_column = next(
                (name for name in ("created_at", "transaction_date", "transaction_time") if name in columns),
                None,
            )
            if date_column:
                adjustments = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM inventory_transactions WHERE substr(COALESCE({date_column}, ''), 1, 10)=?",
                        (today,),
                    ).fetchone()[0]
                    or 0
                )

        label = "Morning Brief" if period == "morning" else "Closing Recap"
        title = f"BrooksHouse {label}"
        body = (
            f"Today: ${gross_sales:,.2f} sales • {order_count} Walmart orders "
            f"({open_orders} open, {completed_orders} completed) • {units} units • "
            f"{adjustments} inventory transactions."
        )
        return title, body
    finally:
        connection.close()


def maybe_send_daily_recaps() -> list[str]:
    ensure_push_tables()
    now = datetime.now(CENTRAL_TIME)
    today = now.date().isoformat()
    current_minutes = now.hour * 60 + now.minute
    settings = load_push_settings()
    sent: list[str] = []
    for period in ("morning", "evening"):
        if not settings[f"{period}_enabled"]:
            continue
        hour, minute = map(int, settings[f"{period}_time"].split(":"))
        scheduled_minutes = hour * 60 + minute
        if current_minutes < scheduled_minutes:
            continue
        if settings[f"last_{period}_date"] == today:
            continue
        title, body = build_daily_recap(period)
        send_notification(
            title,
            body,
            "/channels/orders",
            f"{period}_recap",
        )
        connection = sqlite3.connect(DB_PATH, timeout=30)
        try:
            connection.execute(
                "UPDATE web_push_settings SET setting_value=?, updated_at=? WHERE setting_key=?",
                (today, now.isoformat(), f"last_{period}_date"),
            )
            connection.commit()
        finally:
            connection.close()
        sent.append(period)
    return sent
