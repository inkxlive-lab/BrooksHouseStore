from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


APP_ROOT = Path(__file__).resolve().parent
DB_PATH = APP_ROOT / "data" / "brookshouse_store.db"
ENV_PATH = APP_ROOT.parent / ".env"
PROFILE_PHOTO_DIRECTORY = APP_ROOT / "data" / "kids-profile-photos"
PROFILE_PHOTO_DIRECTORY.mkdir(parents=True, exist_ok=True)
TASK_PROOF_DIRECTORY = APP_ROOT / "data" / "kids-task-proofs"
TASK_PROOF_DIRECTORY.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
router = APIRouter()


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()
COOKIE_SECRET = os.getenv("KIDS_HELPER_COOKIE_SECRET", "")
PIN_HASH = os.getenv("KIDS_HELPER_PIN_HASH", "")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _initialize() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS kids_helpers (
                helper_id INTEGER PRIMARY KEY AUTOINCREMENT,
                helper_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS kids_helper_tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                helper_id INTEGER NOT NULL,
                barcode TEXT NOT NULL,
                product_name TEXT,
                image_url TEXT,
                location_name TEXT,
                container_id TEXT,
                requested_quantity INTEGER NOT NULL DEFAULT 1,
                counted_quantity INTEGER,
                status TEXT NOT NULL DEFAULT 'assigned',
                approval_status TEXT NOT NULL DEFAULT 'pending',
                assigned_by TEXT,
                notes TEXT,
                assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                approved_at TEXT,
                approved_by TEXT,
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id)
            );

            CREATE TABLE IF NOT EXISTS kids_helper_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                helper_id INTEGER,
                event_type TEXT NOT NULL,
                scanned_barcode TEXT,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES kids_helper_tasks(task_id),
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id)
            );

            CREATE TABLE IF NOT EXISTS kids_task_checklist_items (
                checklist_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                item_text TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                checked INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT,
                FOREIGN KEY(task_id) REFERENCES kids_helper_tasks(task_id)
            );

            CREATE TABLE IF NOT EXISTS kids_points_ledger (
                points_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                helper_id INTEGER NOT NULL,
                task_id INTEGER,
                redemption_id INTEGER,
                points_change INTEGER NOT NULL,
                entry_type TEXT NOT NULL,
                description TEXT,
                entered_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id),
                FOREIGN KEY(task_id) REFERENCES kids_helper_tasks(task_id)
            );

            CREATE TABLE IF NOT EXISTS kids_rewards (
                reward_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reward_name TEXT NOT NULL,
                points_cost INTEGER NOT NULL,
                description TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS kids_reward_redemptions (
                redemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
                helper_id INTEGER NOT NULL,
                reward_id INTEGER NOT NULL,
                reward_name TEXT NOT NULL,
                points_cost INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewed_by TEXT,
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id),
                FOREIGN KEY(reward_id) REFERENCES kids_rewards(reward_id)
            );

            CREATE TABLE IF NOT EXISTS kids_point_rules (
                rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_key TEXT NOT NULL UNIQUE,
                rule_name TEXT NOT NULL,
                rule_type TEXT NOT NULL,
                activity_key TEXT,
                task_type TEXT,
                task_title TEXT,
                units_required INTEGER NOT NULL DEFAULT 1,
                points_awarded INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kids_activity_events (
                activity_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                helper_id INTEGER NOT NULL,
                rule_id INTEGER NOT NULL,
                source_event_key TEXT NOT NULL UNIQUE,
                activity_key TEXT NOT NULL,
                barcode TEXT,
                product_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id),
                FOREIGN KEY(rule_id) REFERENCES kids_point_rules(rule_id)
            );
            CREATE TABLE IF NOT EXISTS kids_activity_progress (
                helper_id INTEGER NOT NULL,
                rule_id INTEGER NOT NULL,
                progress_count INTEGER NOT NULL DEFAULT 0,
                lifetime_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(helper_id, rule_id),
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id),
                FOREIGN KEY(rule_id) REFERENCES kids_point_rules(rule_id)
            );
            CREATE TABLE IF NOT EXISTS kids_activity_awards (
                activity_award_id INTEGER PRIMARY KEY AUTOINCREMENT,
                helper_id INTEGER NOT NULL,
                rule_id INTEGER NOT NULL,
                trigger_event_id INTEGER NOT NULL UNIQUE,
                points_awarded INTEGER NOT NULL,
                units_completed INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(helper_id) REFERENCES kids_helpers(helper_id),
                FOREIGN KEY(rule_id) REFERENCES kids_point_rules(rule_id),
                FOREIGN KEY(trigger_event_id) REFERENCES kids_activity_events(activity_event_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_kids_points_task_award
              ON kids_points_ledger(task_id) WHERE task_id IS NOT NULL AND entry_type = 'task_award';
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(kids_helpers)")}
        additions = {
            "app_user_id": "INTEGER",
            "pin_hash": "TEXT",
            "avatar": "TEXT NOT NULL DEFAULT '🧒'",
            "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
            "locked_until": "INTEGER",
            "last_login_at": "TEXT",
            "profile_image_name": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE kids_helpers ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_kids_helpers_app_user_id "
            "ON kids_helpers(app_user_id) WHERE app_user_id IS NOT NULL"
        )
        task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(kids_helper_tasks)")}
        task_additions = {
            "points_value": "INTEGER NOT NULL DEFAULT 0",
            "points_awarded": "INTEGER NOT NULL DEFAULT 0",
            "task_type": "TEXT NOT NULL DEFAULT 'pull_product'",
            "task_title": "TEXT",
            "priority": "TEXT NOT NULL DEFAULT 'normal'",
            "due_at": "TEXT",
            "requires_barcode": "INTEGER NOT NULL DEFAULT 1",
            "requires_photo": "INTEGER NOT NULL DEFAULT 0",
            "completion_notes": "TEXT",
            "completion_photo_name": "TEXT",
        }
        for name, definition in task_additions.items():
            if name not in task_columns:
                connection.execute(f"ALTER TABLE kids_helper_tasks ADD COLUMN {name} {definition}")
        ledger_columns = {row["name"] for row in connection.execute("PRAGMA table_info(kids_points_ledger)")}
        if "activity_award_id" not in ledger_columns:
            connection.execute("ALTER TABLE kids_points_ledger ADD COLUMN activity_award_id INTEGER")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_kids_points_activity_award "
            "ON kids_points_ledger(activity_award_id) WHERE activity_award_id IS NOT NULL"
        )
        connection.execute(
            """INSERT OR IGNORE INTO kids_point_rules
               (rule_key, rule_name, rule_type, activity_key, units_required, points_awarded)
               VALUES ('batch_valid_scan', 'Batch barcode scanning', 'activity',
                       'batch_valid_scan', 5, 1)"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO kids_point_rules
               (rule_key, rule_name, rule_type, activity_key, units_required, points_awarded)
               VALUES ('batch_pieces_processed', 'Batch pieces processed', 'activity',
                       'batch_pieces_processed', 25, 1)"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO kids_point_rules
               (rule_key, rule_name, rule_type, task_type, task_title, units_required, points_awarded)
               VALUES ('take_out_trash', 'Take out the trash', 'task_preset',
                       'clean_organize', 'Take out the trash', 1, 10)"""
        )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(COOKIE_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return body + "." + signature


def _unsign(value: Optional[str]) -> Optional[dict]:
    if not value or not COOKIE_SECRET or "." not in value:
        return None
    body, signature = value.rsplit(".", 1)
    expected = _b64(hmac.new(COOKIE_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if int(payload.get("expires", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _verify_pin(pin: str) -> bool:
    try:
        algorithm, rounds, salt, wanted = PIN_HASH.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, wanted)
    except Exception:
        return False


def _hash_child_pin(pin: str) -> str:
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise ValueError("Child PIN must contain 4 to 6 numbers.")
    salt = secrets.token_bytes(16)
    rounds = 180000
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest}"


def _verify_child_pin(pin: str, stored: Optional[str]) -> bool:
    try:
        algorithm, rounds, salt, wanted = (stored or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, wanted)
    except Exception:
        hashlib.pbkdf2_hmac("sha256", pin.encode(), b"kids-helper-dummy", 180000)
        return False


def _clean_avatar(value: str) -> str:
    return (value or "🧒").strip()[:32] or "🧒"


def _profile_photo_path(file_name: Optional[str]) -> Optional[Path]:
    if not file_name:
        return None
    clean_name = Path(file_name).name
    candidate = PROFILE_PHOTO_DIRECTORY / clean_name
    return candidate if candidate.parent == PROFILE_PHOTO_DIRECTORY else None


def _task_proof_path(file_name: Optional[str]) -> Optional[Path]:
    if not file_name:
        return None
    clean_name = Path(file_name).name
    candidate = TASK_PROOF_DIRECTORY / clean_name
    return candidate if candidate.parent == TASK_PROOF_DIRECTORY else None


def _picture_kind(data: bytes) -> Optional[tuple[str, str]]:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _adult(request: Request) -> bool:
    payload = _unsign(request.cookies.get("brookshouse_adult"))
    return bool(payload and payload.get("role") == "adult")


def _cookie_helper(request: Request) -> Optional[dict]:
    payload = _unsign(request.cookies.get("brookshouse_kids_mode"))
    return payload if payload and payload.get("role") == "kids_helper" else None


def _helper(request: Request) -> Optional[dict]:
    payload = _cookie_helper(request)
    if payload:
        payload["account_linked"] = False
        return payload
    auth_user = getattr(request.state, "auth_user", None)
    if not auth_user or getattr(auth_user, "role", "") != "store_worker":
        return None
    with _connect() as connection:
        helper = connection.execute(
            "SELECT * FROM kids_helpers WHERE app_user_id=? AND active=1",
            (auth_user.user_id,),
        ).fetchone()
    if not helper:
        return None
    return {
        "role": "kids_helper",
        "helper_id": helper["helper_id"],
        "helper_name": helper["helper_name"],
        "avatar": helper["avatar"],
        "profile_image_name": helper["profile_image_name"],
        "account_linked": True,
    }


def _barcode_equal(left: str, right: str) -> bool:
    a = "".join(character for character in (left or "") if character.isdigit())
    b = "".join(character for character in (right or "") if character.isdigit())
    return bool(a and b and (a == b or a.lstrip("0") == b.lstrip("0")))


def _lookup_product(connection: sqlite3.Connection, barcode: str) -> dict:
    clean = "".join(character for character in barcode if character.isdigit()) or barcode.strip()
    row = connection.execute(
        """
        SELECT p.product_id, p.product_name, pb.barcode,
               COALESCE(pi.image_url, pi.image_path, pe.image_url) AS image_url
        FROM product_barcodes pb
        JOIN products p ON p.product_id = pb.product_id
        LEFT JOIN product_images pi
          ON pi.product_id = p.product_id AND pi.is_primary = 1
        LEFT JOIN product_enrichment pe ON pe.barcode = pb.barcode
        WHERE pb.barcode = ? OR LTRIM(pb.barcode, '0') = LTRIM(?, '0')
        ORDER BY pb.is_primary DESC, pi.is_primary DESC
        LIMIT 1
        """,
        (clean, clean),
    ).fetchone()
    result = dict(row) if row else {"product_name": "Unknown product", "barcode": clean, "image_url": None}
    if row:
        inventory = connection.execute(
            """
            SELECT l.location_name, i.container_id, i.quantity_on_hand
            FROM inventory i
            JOIN inventory_locations l ON l.location_id = i.location_id
            WHERE i.product_id = ? AND i.quantity_on_hand > 0
            ORDER BY i.quantity_on_hand DESC
            """,
            (row["product_id"],),
        ).fetchall()
        result["inventory"] = [dict(item) for item in inventory]
    else:
        result["inventory"] = []
    return result


def _page(request: Request, **extra):
    helper = _helper(request)
    with _connect() as connection:
        helpers = connection.execute("SELECT * FROM kids_helpers WHERE active = 1 ORDER BY helper_name").fetchall()
        tasks = connection.execute(
            """
            SELECT t.*, h.helper_name
            FROM kids_helper_tasks t JOIN kids_helpers h ON h.helper_id = t.helper_id
            ORDER BY CASE t.status WHEN 'assigned' THEN 0 ELSE 1 END, t.assigned_at, t.task_id
            """
        ).fetchall()
        balances = {
            row["helper_id"]: row["balance"]
            for row in connection.execute(
                """SELECT h.helper_id, COALESCE(SUM(p.points_change), 0) balance
                   FROM kids_helpers h LEFT JOIN kids_points_ledger p ON p.helper_id=h.helper_id
                   GROUP BY h.helper_id"""
            ).fetchall()
        }
        point_history = connection.execute(
            """SELECT p.*, h.helper_name FROM kids_points_ledger p
               JOIN kids_helpers h ON h.helper_id=p.helper_id
               ORDER BY p.points_entry_id DESC LIMIT 100"""
        ).fetchall()
        rewards = connection.execute(
            "SELECT * FROM kids_rewards WHERE active=1 ORDER BY points_cost, reward_name"
        ).fetchall()
        redemptions = connection.execute(
            """SELECT r.*, h.helper_name FROM kids_reward_redemptions r
               JOIN kids_helpers h ON h.helper_id=r.helper_id
               ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.redemption_id DESC LIMIT 100"""
        ).fetchall()
        checklist_rows = connection.execute(
            "SELECT * FROM kids_task_checklist_items ORDER BY task_id, sort_order, checklist_item_id"
        ).fetchall()
        checklists = {}
        for row in checklist_rows:
            checklists.setdefault(row["task_id"], []).append(row)
        point_rules = connection.execute(
            "SELECT * FROM kids_point_rules ORDER BY rule_type, rule_name"
        ).fetchall()
        activity_progress = connection.execute(
            """SELECT p.*, r.rule_name, r.units_required, r.points_awarded
               FROM kids_activity_progress p
               JOIN kids_point_rules r ON r.rule_id=p.rule_id
               WHERE r.active=1"""
        ).fetchall()
        progress_by_helper = {}
        for row in activity_progress:
            progress_by_helper.setdefault(row["helper_id"], []).append(row)
    return templates.TemplateResponse(
        request=request,
        name="kids_helper.html",
        context={"request": request, "adult": _adult(request), "helper": helper,
                 "helpers": helpers, "tasks": tasks, "balances": balances,
                 "point_history": point_history, "rewards": rewards,
                 "redemptions": redemptions, "checklists": checklists,
                 "point_rules": point_rules, "progress_by_helper": progress_by_helper, **extra},
    )


def _valid_source_key(value: str) -> bool:
    clean = (value or "").strip()
    return 12 <= len(clean) <= 100 and all(character.isalnum() or character in "-_" for character in clean)


def award_committed_batch_scans(
    app_user_id: int,
    scan_events: list[dict],
    pieces_processed: int = 0,
    batch_key: str = "",
) -> dict:
    """Award activity points only for scans included in a committed batch.

    Client event keys are unique, so retrying a response or reconciliation is
    harmless.  This function intentionally runs after the inventory database
    commit: an abandoned browser batch can never earn points.
    """
    result = {
        "events_added": 0, "points_awarded": 0, "progress": 0,
        "required": 0, "piece_points_awarded": 0, "piece_progress": 0,
        "piece_required": 0, "balance": 0,
    }
    if not app_user_id or not scan_events:
        return result

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        helper = connection.execute(
            "SELECT * FROM kids_helpers WHERE app_user_id=? AND active=1",
            (app_user_id,),
        ).fetchone()
        if not helper:
            return result
        rule = connection.execute(
            """SELECT * FROM kids_point_rules
               WHERE rule_type='activity' AND activity_key='batch_valid_scan' AND active=1"""
        ).fetchone()
        if not rule:
            return result

        helper_id = helper["helper_id"]
        units_required = max(1, int(rule["units_required"] or 1))
        result["required"] = units_required
        points_per_milestone = max(0, int(rule["points_awarded"] or 0))

        for submitted in scan_events:
            if not isinstance(submitted, dict):
                continue
            raw_key = str(submitted.get("event_key") or "").strip()
            source_key = f"committed-{raw_key}"
            barcode = "".join(
                character for character in str(submitted.get("barcode") or "")
                if character.isdigit()
            )
            try:
                product_id = int(submitted.get("product_id"))
            except (TypeError, ValueError):
                continue
            if not _valid_source_key(source_key) or not barcode or product_id < 1:
                continue
            existing = connection.execute(
                "SELECT 1 FROM kids_activity_events WHERE source_event_key=?",
                (source_key,),
            ).fetchone()
            if existing:
                continue
            product = _lookup_product(connection, barcode)
            if int(product.get("product_id") or 0) != product_id:
                continue

            cursor = connection.execute(
                """INSERT INTO kids_activity_events
                   (helper_id, rule_id, source_event_key, activity_key, barcode, product_id, details)
                   VALUES (?, ?, ?, 'batch_valid_scan', ?, ?, ?)""",
                (helper_id, rule["rule_id"], source_key, barcode, product_id,
                 str(product.get("product_name") or "")[:200]),
            )
            event_id = cursor.lastrowid
            progress = connection.execute(
                "SELECT progress_count FROM kids_activity_progress WHERE helper_id=? AND rule_id=?",
                (helper_id, rule["rule_id"]),
            ).fetchone()
            new_progress = int(progress["progress_count"] if progress else 0) + 1
            milestones = new_progress // units_required
            remainder = new_progress % units_required
            points = milestones * points_per_milestone
            connection.execute(
                """INSERT INTO kids_activity_progress
                   (helper_id, rule_id, progress_count, lifetime_count)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(helper_id, rule_id) DO UPDATE SET
                     progress_count=excluded.progress_count,
                     lifetime_count=kids_activity_progress.lifetime_count+1,
                     updated_at=CURRENT_TIMESTAMP""",
                (helper_id, rule["rule_id"], remainder),
            )
            result["events_added"] += 1
            result["progress"] = remainder
            if points:
                award = connection.execute(
                    """INSERT INTO kids_activity_awards
                       (helper_id, rule_id, trigger_event_id, points_awarded, units_completed)
                       VALUES (?, ?, ?, ?, ?)""",
                    (helper_id, rule["rule_id"], event_id, points,
                     milestones * units_required),
                )
                connection.execute(
                    """INSERT INTO kids_points_ledger
                       (helper_id, activity_award_id, points_change, entry_type, description, entered_by)
                       VALUES (?, ?, ?, 'activity_award', ?, 'Committed batch rule')""",
                    (helper_id, award.lastrowid, points,
                     f"{rule['rule_name']}: {milestones * units_required} saved scans"),
                )
                result["points_awarded"] += points

        piece_rule = connection.execute(
            """SELECT * FROM kids_point_rules
               WHERE rule_type='activity'
                 AND activity_key='batch_pieces_processed' AND active=1"""
        ).fetchone()
        piece_count = max(0, int(pieces_processed or 0))
        if piece_rule and piece_count and batch_key:
            piece_required = max(1, int(piece_rule["units_required"] or 1))
            result["piece_required"] = piece_required
            digest = hashlib.sha256(
                f"{app_user_id}|{batch_key}".encode("utf-8")
            ).hexdigest()[:40]
            source_key = f"committed-pieces-{digest}"
            existing_piece_event = connection.execute(
                "SELECT 1 FROM kids_activity_events WHERE source_event_key=?",
                (source_key,),
            ).fetchone()
            if not existing_piece_event:
                piece_event = connection.execute(
                    """INSERT INTO kids_activity_events
                       (helper_id, rule_id, source_event_key, activity_key, details)
                       VALUES (?, ?, ?, 'batch_pieces_processed', ?)""",
                    (helper_id, piece_rule["rule_id"], source_key,
                     f"{piece_count} pieces in saved batch {batch_key}"[:500]),
                )
                progress = connection.execute(
                    """SELECT progress_count FROM kids_activity_progress
                       WHERE helper_id=? AND rule_id=?""",
                    (helper_id, piece_rule["rule_id"]),
                ).fetchone()
                new_progress = int(progress["progress_count"] if progress else 0) + piece_count
                milestones = new_progress // piece_required
                remainder = new_progress % piece_required
                piece_points = milestones * max(0, int(piece_rule["points_awarded"] or 0))
                connection.execute(
                    """INSERT INTO kids_activity_progress
                       (helper_id, rule_id, progress_count, lifetime_count)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(helper_id, rule_id) DO UPDATE SET
                         progress_count=excluded.progress_count,
                         lifetime_count=kids_activity_progress.lifetime_count+excluded.lifetime_count,
                         updated_at=CURRENT_TIMESTAMP""",
                    (helper_id, piece_rule["rule_id"], remainder, piece_count),
                )
                result["piece_progress"] = remainder
                if piece_points:
                    award = connection.execute(
                        """INSERT INTO kids_activity_awards
                           (helper_id, rule_id, trigger_event_id, points_awarded, units_completed)
                           VALUES (?, ?, ?, ?, ?)""",
                        (helper_id, piece_rule["rule_id"], piece_event.lastrowid,
                         piece_points, milestones * piece_required),
                    )
                    connection.execute(
                        """INSERT INTO kids_points_ledger
                           (helper_id, activity_award_id, points_change, entry_type, description, entered_by)
                           VALUES (?, ?, ?, 'activity_award', ?, 'Committed batch rule')""",
                        (helper_id, award.lastrowid, piece_points,
                         f"{piece_rule['rule_name']}: {milestones * piece_required} saved pieces"),
                    )
                    result["piece_points_awarded"] = piece_points
                    result["points_awarded"] += piece_points

        result["balance"] = connection.execute(
            "SELECT COALESCE(SUM(points_change),0) FROM kids_points_ledger WHERE helper_id=?",
            (helper_id,),
        ).fetchone()[0]
    return result


@router.post("/kids/activity/batch-scan")
async def record_batch_scan(request: Request):
    helper = _helper(request)
    if not helper or not helper.get("account_linked"):
        return JSONResponse({"ok": False, "message": "A linked Store Helper login is required."}, status_code=403)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "Invalid scan activity."}, status_code=400)
    source_key = str(payload.get("event_key") or "").strip()
    barcode = "".join(character for character in str(payload.get("barcode") or "") if character.isdigit())
    if not _valid_source_key(source_key) or not barcode:
        return JSONResponse({"ok": False, "message": "Invalid scan activity."}, status_code=400)
    # Legacy/cached Batch Scan pages may still call this endpoint. Do not
    # award anything until the inventory batch has committed successfully.
    return JSONResponse({
        "ok": True,
        "pending_commit": True,
        "points_awarded": 0,
        "message": "Points will post when the batch is saved.",
    })
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT activity_event_id FROM kids_activity_events WHERE source_event_key=?", (source_key,)
        ).fetchone()
        if existing:
            return JSONResponse({"ok": True, "duplicate": True, "points_awarded": 0})
        product = _lookup_product(connection, barcode)
        product_id = product.get("product_id")
        if not product_id:
            return JSONResponse({"ok": False, "message": "Only valid product scans count."}, status_code=400)
        rule = connection.execute(
            """SELECT * FROM kids_point_rules
               WHERE rule_type='activity' AND activity_key='batch_valid_scan' AND active=1"""
        ).fetchone()
        if not rule:
            return JSONResponse({"ok": True, "points_awarded": 0, "rule_active": False})
        connection.execute(
            """INSERT INTO kids_activity_events
               (helper_id, rule_id, source_event_key, activity_key, barcode, product_id, details)
               VALUES (?, ?, ?, 'batch_valid_scan', ?, ?, ?)""",
            (helper["helper_id"], rule["rule_id"], source_key, barcode, product_id,
             str(product.get("product_name") or "")[:200]),
        )
        event_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        progress = connection.execute(
            "SELECT * FROM kids_activity_progress WHERE helper_id=? AND rule_id=?",
            (helper["helper_id"], rule["rule_id"]),
        ).fetchone()
        new_progress = int(progress["progress_count"] if progress else 0) + 1
        units_required = max(1, int(rule["units_required"] or 1))
        milestones = new_progress // units_required
        remainder = new_progress % units_required
        points = milestones * max(0, int(rule["points_awarded"] or 0))
        connection.execute(
            """INSERT INTO kids_activity_progress
               (helper_id, rule_id, progress_count, lifetime_count)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(helper_id, rule_id) DO UPDATE SET
                 progress_count=excluded.progress_count,
                 lifetime_count=kids_activity_progress.lifetime_count+1,
                 updated_at=CURRENT_TIMESTAMP""",
            (helper["helper_id"], rule["rule_id"], remainder),
        )
        if points:
            connection.execute(
                """INSERT INTO kids_activity_awards
                   (helper_id, rule_id, trigger_event_id, points_awarded, units_completed)
                   VALUES (?, ?, ?, ?, ?)""",
                (helper["helper_id"], rule["rule_id"], event_id, points, milestones * units_required),
            )
            award_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            connection.execute(
                """INSERT INTO kids_points_ledger
                   (helper_id, activity_award_id, points_change, entry_type, description, entered_by)
                   VALUES (?, ?, ?, 'activity_award', ?, 'Automatic rule')""",
                (helper["helper_id"], award_id, points,
                 f"{rule['rule_name']}: {milestones * units_required} valid scans"),
            )
        balance = connection.execute(
            "SELECT COALESCE(SUM(points_change),0) FROM kids_points_ledger WHERE helper_id=?",
            (helper["helper_id"],),
        ).fetchone()[0]
    return JSONResponse({
        "ok": True, "points_awarded": points, "progress": remainder,
        "required": units_required, "scans_needed": units_required - remainder,
        "balance": balance,
    })


@router.get("/kids", response_class=HTMLResponse)
def kids_home(request: Request):
    return _page(request)


@router.post("/kids/adult-login")
def adult_login(request: Request, pin: str = Form(...)):
    if not _verify_pin(pin):
        return _page(request, error="That adult PIN was not accepted.")
    response = RedirectResponse("/kids", status_code=303)
    response.set_cookie("brookshouse_adult", _sign({"role": "adult", "expires": int(time.time()) + 1800}),
                        max_age=1800, httponly=True, secure=request.url.scheme == "https", samesite="lax")
    return response


@router.post("/kids/helpers")
def add_helper(request: Request, helper_name: str = Form(...), child_pin: str = Form(...),
               avatar: str = Form("🧒")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    name = helper_name.strip()[:60]
    if name:
        try:
            pin_hash = _hash_child_pin(child_pin.strip())
            clean_avatar = _clean_avatar(avatar)
            with _connect() as connection:
                connection.execute("INSERT INTO kids_helpers(helper_name, pin_hash, avatar) VALUES (?, ?, ?)",
                                   (name, pin_hash, clean_avatar))
                helper_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'profile_created', ?)",
                                   (helper_id, f"Avatar {clean_avatar}; child PIN created"))
        except (ValueError, sqlite3.IntegrityError) as error:
            return _page(request, error=str(error))
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/helpers/{helper_id}/pin")
def reset_child_pin(request: Request, helper_id: int, child_pin: str = Form(...),
                    avatar: str = Form("🧒")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    try:
        pin_hash = _hash_child_pin(child_pin.strip())
        clean_avatar = _clean_avatar(avatar)
        with _connect() as connection:
            connection.execute("""UPDATE kids_helpers SET pin_hash=?, avatar=?, failed_attempts=0,
                                  locked_until=NULL WHERE helper_id=?""", (pin_hash, clean_avatar, helper_id))
            connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'pin_reset', ?)",
                               (helper_id, "Adult reset child PIN"))
    except ValueError as error:
        return _page(request, error=str(error))
    return RedirectResponse("/kids", status_code=303)


@router.get("/kids/profile-photo/{helper_id}")
def helper_profile_photo(helper_id: int):
    with _connect() as connection:
        helper = connection.execute("SELECT profile_image_name FROM kids_helpers WHERE helper_id=? AND active=1",
                                    (helper_id,)).fetchone()
    path = _profile_photo_path(helper["profile_image_name"] if helper else None)
    if not path or not path.is_file():
        return JSONResponse({"detail": "Profile picture not found."}, status_code=404)
    media_types = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    return FileResponse(path, media_type=media_types.get(path.suffix.lower(), "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=300"})


@router.post("/kids/helpers/{helper_id}/photo")
async def save_helper_photo(request: Request, helper_id: int, photo: UploadFile = File(...)):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    extension = allowed.get((photo.content_type or "").lower())
    if not extension:
        return _page(request, error="Use a JPG, PNG or WebP profile picture.")
    data = bytearray()
    while True:
        chunk = await photo.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 8 * 1024 * 1024:
            return _page(request, error="Profile picture must be 8 MB or smaller.")
    signatures_ok = (
        extension == ".jpg" and bytes(data[:3]) == b"\xff\xd8\xff"
        or extension == ".png" and bytes(data[:8]) == b"\x89PNG\r\n\x1a\n"
        or extension == ".webp" and bytes(data[:4]) == b"RIFF" and bytes(data[8:12]) == b"WEBP"
    )
    if not signatures_ok:
        return _page(request, error="That file does not contain a valid supported picture.")
    with _connect() as connection:
        helper = connection.execute("SELECT profile_image_name FROM kids_helpers WHERE helper_id=? AND active=1",
                                    (helper_id,)).fetchone()
        if not helper:
            return _page(request, error="Helper profile was not found.")
        file_name = f"helper-{helper_id}-{secrets.token_hex(10)}{extension}"
        target = PROFILE_PHOTO_DIRECTORY / file_name
        target.write_bytes(data)
        connection.execute("UPDATE kids_helpers SET profile_image_name=? WHERE helper_id=?", (file_name, helper_id))
        connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'profile_photo_saved', ?)",
                           (helper_id, "Adult saved a helper profile picture"))
        previous = _profile_photo_path(helper["profile_image_name"])
    if previous and previous.is_file() and previous != target:
        previous.unlink(missing_ok=True)
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/helpers/{helper_id}/photo/delete")
def delete_helper_photo(request: Request, helper_id: int):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    with _connect() as connection:
        helper = connection.execute("SELECT profile_image_name FROM kids_helpers WHERE helper_id=?", (helper_id,)).fetchone()
        path = _profile_photo_path(helper["profile_image_name"] if helper else None)
        connection.execute("UPDATE kids_helpers SET profile_image_name=NULL WHERE helper_id=?", (helper_id,))
        connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'profile_photo_deleted', ?)",
                           (helper_id, "Adult removed a helper profile picture"))
    if path and path.is_file():
        path.unlink(missing_ok=True)
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/tasks")
def add_task(request: Request, helper_id: int = Form(...), barcode: str = Form(""),
             requested_quantity: int = Form(1), location_name: str = Form(""),
             container_id: str = Form(""), assigned_by: str = Form("Adult"),
             points_value: int = Form(5), notes: str = Form(""),
             task_type: str = Form("pull_product"), task_title: str = Form(""),
             priority: str = Form("normal"), due_at: str = Form(""),
             requires_barcode: bool = Form(False), requires_photo: bool = Form(False),
             checklist: str = Form("")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    allowed_types = {"pull_product", "stock_shelves", "count_inventory", "clean_organize", "take_pictures", "custom"}
    clean_type = task_type if task_type in allowed_types else "custom"
    clean_priority = priority if priority in {"low", "normal", "high", "urgent"} else "normal"
    clean_barcode = barcode.strip()
    barcode_needed = bool(requires_barcode or clean_type == "pull_product")
    if barcode_needed and not clean_barcode:
        return _page(request, error="This task requires a product barcode.")
    with _connect() as connection:
        product = _lookup_product(connection, clean_barcode) if clean_barcode else {
            "barcode": "", "product_name": "", "image_url": None, "inventory": []
        }
        default_location = location_name.strip()
        default_container = container_id.strip()
        if product["inventory"] and not default_location:
            default_location = product["inventory"][0]["location_name"] or ""
            default_container = product["inventory"][0]["container_id"] or ""
        connection.execute(
            """INSERT INTO kids_helper_tasks
               (helper_id, barcode, product_name, image_url, location_name, container_id,
                requested_quantity, assigned_by, points_value, notes, task_type, task_title,
                priority, due_at, requires_barcode, requires_photo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (helper_id, product["barcode"], product["product_name"], product["image_url"],
             default_location, default_container, max(1, requested_quantity),
             assigned_by.strip()[:60], max(0, min(points_value, 10000)), notes.strip()[:1000],
             clean_type, task_title.strip()[:150], clean_priority, due_at.strip()[:40] or None,
             1 if barcode_needed else 0, 1 if (requires_photo or clean_type == "take_pictures") else 0),
        )
        task_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        steps = [line.strip().lstrip("-•0123456789. ").strip() for line in checklist.splitlines()]
        steps = [step for step in steps if step][:25]
        for position, step in enumerate(steps, start=1):
            connection.execute(
                "INSERT INTO kids_task_checklist_items(task_id,item_text,sort_order) VALUES (?,?,?)",
                (task_id, step[:300], position),
            )
        connection.execute(
            "INSERT INTO kids_helper_events(task_id,helper_id,event_type,details) VALUES (?,?,'directed_task_assigned',?)",
            (task_id, helper_id, f"{clean_type}; {clean_priority}; {points_value} points; {len(steps)} steps"),
        )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/tasks/from-rule")
def add_task_from_rule(request: Request, helper_id: int = Form(...), rule_id: int = Form(...),
                       assigned_by: str = Form("Adult"), notes: str = Form("")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    with _connect() as connection:
        rule = connection.execute(
            "SELECT * FROM kids_point_rules WHERE rule_id=? AND rule_type='task_preset' AND active=1",
            (rule_id,),
        ).fetchone()
        helper = connection.execute(
            "SELECT helper_id FROM kids_helpers WHERE helper_id=? AND active=1", (helper_id,)
        ).fetchone()
        if not rule or not helper:
            return _page(request, error="Choose an active task rule and helper.")
        connection.execute(
            """INSERT INTO kids_helper_tasks
               (helper_id, barcode, product_name, requested_quantity, assigned_by,
                points_value, notes, task_type, task_title, priority,
                requires_barcode, requires_photo)
               VALUES (?, '', ?, 1, ?, ?, ?, ?, ?, 'normal', 0, 0)""",
            (helper_id, rule["task_title"] or rule["rule_name"], assigned_by.strip()[:60],
             max(0, int(rule["points_awarded"] or 0)), notes.strip()[:1000],
             rule["task_type"] or "custom", rule["task_title"] or rule["rule_name"]),
        )
        task_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO kids_helper_events(task_id, helper_id, event_type, details)
               VALUES (?, ?, 'preset_task_assigned', ?)""",
            (task_id, helper_id, f"rule_id={rule_id}; {rule['points_awarded']} points after approval"),
        )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/enter")
def enter_kids_mode(request: Request, helper_id: int = Form(...), child_pin: str = Form(...)):
    with _connect() as connection:
        helper = connection.execute("SELECT * FROM kids_helpers WHERE helper_id = ? AND active = 1", (helper_id,)).fetchone()
        if not helper:
            return _page(request, error="Choose an active helper.")
        now = int(time.time())
        if helper["locked_until"] and int(helper["locked_until"]) > now:
            return _page(request, error="That helper profile is temporarily locked. Ask an adult or wait 15 minutes.")
        if not helper["pin_hash"]:
            return _page(request, error="An adult must create a PIN for that helper profile first.")
        if not _verify_child_pin(child_pin.strip(), helper["pin_hash"]):
            attempts = int(helper["failed_attempts"] or 0) + 1
            locked_until = now + 900 if attempts >= 5 else None
            connection.execute("UPDATE kids_helpers SET failed_attempts=?, locked_until=? WHERE helper_id=?",
                               (0 if locked_until else attempts, locked_until, helper_id))
            connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'child_login_failed', ?)",
                               (helper_id, "Incorrect child PIN"))
            return _page(request, error="That child PIN was not accepted.")
        connection.execute("""UPDATE kids_helpers SET failed_attempts=0, locked_until=NULL,
                              last_login_at=CURRENT_TIMESTAMP WHERE helper_id=?""", (helper_id,))
        connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'child_login_success', ?)",
                           (helper_id, "Child signed into Kids Helper Mode"))
    response = RedirectResponse("/kids", status_code=303)
    response.set_cookie("brookshouse_kids_mode",
                        _sign({"role": "kids_helper", "helper_id": helper_id,
                               "helper_name": helper["helper_name"], "avatar": helper["avatar"],
                               "profile_image_name": helper["profile_image_name"],
                               "expires": int(time.time()) + 43200}),
                        max_age=43200, httponly=True, secure=request.url.scheme == "https", samesite="lax")
    response.delete_cookie("brookshouse_adult")
    return response


@router.post("/kids/enter-adult")
def enter_kids_mode_as_adult(request: Request, helper_id: int = Form(...), pin: str = Form(...)):
    if not _verify_pin(pin):
        return _page(request, error="Adult PIN was not accepted.")
    with _connect() as connection:
        helper = connection.execute("SELECT * FROM kids_helpers WHERE helper_id=? AND active=1", (helper_id,)).fetchone()
        if not helper:
            return _page(request, error="Choose an active helper.")
        connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'adult_started_session', ?)",
                           (helper_id, "Adult opened this helper profile"))
    response = RedirectResponse("/kids", status_code=303)
    response.set_cookie("brookshouse_kids_mode",
                        _sign({"role": "kids_helper", "helper_id": helper_id,
                               "helper_name": helper["helper_name"], "avatar": helper["avatar"],
                               "profile_image_name": helper["profile_image_name"],
                               "expires": int(time.time()) + 43200}),
                        max_age=43200, httponly=True, secure=request.url.scheme == "https", samesite="lax")
    response.delete_cookie("brookshouse_adult")
    return response


@router.post("/kids/exit")
def exit_kids_mode(request: Request, pin: str = Form(...)):
    helper = _helper(request)
    if not helper or not _verify_pin(pin):
        return _page(request, error="Adult PIN required to exit Kids Mode.")
    with _connect() as connection:
        connection.execute("INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'session_ended', ?)",
                           (helper["helper_id"], "Kids Helper Mode ended"))
    response = RedirectResponse("/kids", status_code=303)
    response.delete_cookie("brookshouse_kids_mode")
    return response


@router.get("/kids/task/{task_id}/proof")
def task_proof(request: Request, task_id: int):
    helper = _helper(request)
    with _connect() as connection:
        task = connection.execute(
            "SELECT helper_id, completion_photo_name FROM kids_helper_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    if not task or (not _adult(request) and (not helper or helper["helper_id"] != task["helper_id"])):
        return JSONResponse({"detail": "Proof picture not available."}, status_code=404)
    path = _task_proof_path(task["completion_photo_name"])
    if not path or not path.is_file():
        return JSONResponse({"detail": "Proof picture not found."}, status_code=404)
    media = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower())
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=300"})


@router.post("/kids/task/{task_id}/complete")
async def complete_directed_task(request: Request, task_id: int):
    helper = _helper(request)
    if not helper:
        return RedirectResponse("/kids", status_code=303)
    form = await request.form()
    completion_notes = str(form.get("completion_notes") or "").strip()[:1000]
    try:
        counted_quantity = max(0, int(form.get("counted_quantity") or 0))
    except (TypeError, ValueError):
        counted_quantity = 0
    completed_ids = {int(value) for value in form.getlist("completed_steps") if str(value).isdigit()}
    photo = form.get("proof_photo")
    photo_data = b""
    if photo and getattr(photo, "filename", ""):
        photo_data = await photo.read(8 * 1024 * 1024 + 1)
        await photo.close()
        if len(photo_data) > 8 * 1024 * 1024:
            return _page(request, error="Task proof picture must be 8 MB or smaller.")
        if not _picture_kind(photo_data):
            return _page(request, error="Use a valid JPG, PNG or WebP proof picture.")
    with _connect() as connection:
        task = connection.execute(
            "SELECT * FROM kids_helper_tasks WHERE task_id=? AND helper_id=? AND status='assigned'",
            (task_id, helper["helper_id"]),
        ).fetchone()
        if not task:
            return RedirectResponse("/kids", status_code=303)
        if task["requires_barcode"]:
            return _page(request, error="Scan the assigned barcode to complete that task.")
        steps = connection.execute(
            "SELECT checklist_item_id FROM kids_task_checklist_items WHERE task_id=?", (task_id,)
        ).fetchall()
        required_ids = {row["checklist_item_id"] for row in steps}
        if required_ids and not required_ids.issubset(completed_ids):
            return _page(request, error="Check every task step before submitting it for adult review.")
        if task["requires_photo"] and not photo_data:
            return _page(request, error="This task requires a completion picture.")
        photo_name = None
        if photo_data:
            extension, _ = _picture_kind(photo_data)
            photo_name = f"task-{task_id}-{secrets.token_hex(10)}{extension}"
            target = TASK_PROOF_DIRECTORY / photo_name
            target.write_bytes(photo_data)
        for item_id in required_ids:
            connection.execute(
                """UPDATE kids_task_checklist_items SET checked=1, checked_at=CURRENT_TIMESTAMP
                   WHERE checklist_item_id=? AND task_id=?""", (item_id, task_id)
            )
        connection.execute(
            """UPDATE kids_helper_tasks SET status='completed', approval_status='pending',
               counted_quantity=?, completion_notes=?, completion_photo_name=?,
               completed_at=CURRENT_TIMESTAMP WHERE task_id=?""",
            (counted_quantity, completion_notes, photo_name, task_id),
        )
        connection.execute(
            """INSERT INTO kids_helper_events(task_id,helper_id,event_type,details)
               VALUES (?,?,'directed_task_completed',?)""",
            (task_id, helper["helper_id"], f"{len(required_ids)} steps; photo={bool(photo_name)}"),
        )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/task/{task_id}/scan")
def scan_task(request: Request, task_id: int, barcode: str = Form(...), counted_quantity: int = Form(1)):
    helper = _helper(request)
    if not helper:
        return JSONResponse({"ok": False, "message": "Kids Mode is not active."}, status_code=403)
    with _connect() as connection:
        task = connection.execute("SELECT * FROM kids_helper_tasks WHERE task_id = ? AND helper_id = ?",
                                  (task_id, helper["helper_id"])).fetchone()
        if not task:
            return JSONResponse({"ok": False, "message": "That task is not assigned to this helper."}, status_code=404)
        matches = _barcode_equal(barcode, task["barcode"])
        event = "barcode_matched" if matches else "wrong_barcode"
        connection.execute("INSERT INTO kids_helper_events(task_id, helper_id, event_type, scanned_barcode, details) VALUES (?, ?, ?, ?, ?)",
                           (task_id, helper["helper_id"], event, barcode.strip(), "Scan verification"))
        if matches:
            connection.execute("""UPDATE kids_helper_tasks SET status = 'found', counted_quantity = ?,
                                  approval_status = 'pending', completed_at = CURRENT_TIMESTAMP WHERE task_id = ?""",
                               (max(0, counted_quantity), task_id))
    return JSONResponse({"ok": matches, "message": "Correct item! Waiting for adult approval." if matches else "That is not the assigned item. Try again or tap Need Help."})


@router.post("/kids/task/{task_id}/status")
def task_status(request: Request, task_id: int, status: str = Form(...)):
    helper = _helper(request)
    allowed = {"not_found", "need_help"}
    if not helper or status not in allowed:
        return RedirectResponse("/kids", status_code=303)
    with _connect() as connection:
        connection.execute("""UPDATE kids_helper_tasks SET status = ?, approval_status = 'pending',
                              completed_at = CURRENT_TIMESTAMP WHERE task_id = ? AND helper_id = ?""",
                           (status, task_id, helper["helper_id"]))
        connection.execute("INSERT INTO kids_helper_events(task_id, helper_id, event_type, details) VALUES (?, ?, ?, ?)",
                           (task_id, helper["helper_id"], status, "Helper response"))
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/task/{task_id}/approve")
def approve_task(request: Request, task_id: int, decision: str = Form(...), approved_by: str = Form("Adult")):
    if not _adult(request) or decision not in {"approved", "rejected"}:
        return RedirectResponse("/kids", status_code=303)
    with _connect() as connection:
        task = connection.execute("SELECT * FROM kids_helper_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            return RedirectResponse("/kids", status_code=303)
        connection.execute("""UPDATE kids_helper_tasks SET approval_status = ?, approved_at = CURRENT_TIMESTAMP,
                              approved_by = ? WHERE task_id = ?""", (decision, approved_by.strip()[:60], task_id))
        if decision == "approved" and task["status"] in {"found", "completed"} and not task["points_awarded"]:
            points = max(0, int(task["points_value"] or 0))
            if points:
                connection.execute(
                    """INSERT OR IGNORE INTO kids_points_ledger
                       (helper_id, task_id, points_change, entry_type, description, entered_by)
                       VALUES (?, ?, ?, 'task_award', ?, ?)""",
                    (task["helper_id"], task_id, points,
                     f"Approved task: {task['product_name'] or task['barcode']}", approved_by.strip()[:60]),
                )
            connection.execute("UPDATE kids_helper_tasks SET points_awarded=1 WHERE task_id=?", (task_id,))
        connection.execute("INSERT INTO kids_helper_events(task_id, event_type, details) VALUES (?, 'adult_review', ?)",
                           (task_id, decision))
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/points/adjust")
def adjust_points(request: Request, helper_id: int = Form(...), points_change: int = Form(...),
                  description: str = Form(...), entered_by: str = Form("Adult")):
    if not _adult(request) or points_change == 0:
        return RedirectResponse("/kids", status_code=303)
    amount = max(-10000, min(points_change, 10000))
    with _connect() as connection:
        connection.execute(
            """INSERT INTO kids_points_ledger
               (helper_id, points_change, entry_type, description, entered_by)
               VALUES (?, ?, 'manual_adjustment', ?, ?)""",
            (helper_id, amount, description.strip()[:300], entered_by.strip()[:60]),
        )
        connection.execute(
            "INSERT INTO kids_helper_events(helper_id, event_type, details) VALUES (?, 'points_adjusted', ?)",
            (helper_id, f"{amount:+d} points: {description.strip()[:200]}"),
        )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/point-rules/{rule_id}")
def update_point_rule(request: Request, rule_id: int, rule_name: str = Form(...),
                      units_required: int = Form(...), points_awarded: int = Form(...),
                      active: str = Form("0")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    clean_name = rule_name.strip()[:100]
    units = max(1, min(int(units_required), 10000))
    points = max(0, min(int(points_awarded), 10000))
    enabled = 1 if active == "1" else 0
    if clean_name:
        with _connect() as connection:
            connection.execute(
                """UPDATE kids_point_rules
                   SET rule_name=?, units_required=?, points_awarded=?, active=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE rule_id=?""",
                (clean_name, units, points, enabled, rule_id),
            )
            connection.execute(
                """INSERT INTO kids_helper_events(event_type, details)
                   VALUES ('point_rule_updated', ?)""",
                (f"rule_id={rule_id}; {units} units = {points} points; active={enabled}",),
            )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/task-point-rules")
def add_task_point_rule(request: Request, rule_name: str = Form(...),
                        points_awarded: int = Form(...), task_type: str = Form("custom")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    clean_name = rule_name.strip()[:100]
    points = max(0, min(int(points_awarded), 10000))
    allowed_types = {"pull_product", "stock_shelves", "count_inventory", "clean_organize", "take_pictures", "custom"}
    clean_type = task_type if task_type in allowed_types else "custom"
    if clean_name:
        rule_key = "chore_" + secrets.token_hex(10)
        with _connect() as connection:
            connection.execute(
                """INSERT INTO kids_point_rules
                   (rule_key, rule_name, rule_type, task_type, task_title,
                    units_required, points_awarded)
                   VALUES (?, ?, 'task_preset', ?, ?, 1, ?)""",
                (rule_key, clean_name, clean_type, clean_name, points),
            )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/rewards")
def add_reward(request: Request, reward_name: str = Form(...), points_cost: int = Form(...),
               description: str = Form("")):
    if not _adult(request):
        return RedirectResponse("/kids", status_code=303)
    name = reward_name.strip()[:100]
    if name and points_cost > 0:
        with _connect() as connection:
            connection.execute(
                "INSERT INTO kids_rewards(reward_name, points_cost, description) VALUES (?, ?, ?)",
                (name, min(points_cost, 100000), description.strip()[:300]),
            )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/rewards/{reward_id}/request")
def request_reward(request: Request, reward_id: int):
    helper = _helper(request)
    if not helper:
        return RedirectResponse("/kids", status_code=303)
    with _connect() as connection:
        reward = connection.execute("SELECT * FROM kids_rewards WHERE reward_id=? AND active=1", (reward_id,)).fetchone()
        balance = connection.execute(
            "SELECT COALESCE(SUM(points_change),0) FROM kids_points_ledger WHERE helper_id=?",
            (helper["helper_id"],),
        ).fetchone()[0]
        if not reward or balance < reward["points_cost"]:
            return _page(request, error="You do not have enough available points for that reward yet.")
        connection.execute(
            """INSERT INTO kids_reward_redemptions
               (helper_id, reward_id, reward_name, points_cost)
               VALUES (?, ?, ?, ?)""",
            (helper["helper_id"], reward_id, reward["reward_name"], reward["points_cost"]),
        )
    return RedirectResponse("/kids", status_code=303)


@router.post("/kids/redemptions/{redemption_id}/review")
def review_redemption(request: Request, redemption_id: int, decision: str = Form(...),
                      reviewed_by: str = Form("Adult")):
    if not _adult(request) or decision not in {"approved", "rejected"}:
        return RedirectResponse("/kids", status_code=303)
    with _connect() as connection:
        redemption = connection.execute(
            "SELECT * FROM kids_reward_redemptions WHERE redemption_id=? AND status='pending'",
            (redemption_id,),
        ).fetchone()
        if not redemption:
            return RedirectResponse("/kids", status_code=303)
        if decision == "approved":
            balance = connection.execute(
                "SELECT COALESCE(SUM(points_change),0) FROM kids_points_ledger WHERE helper_id=?",
                (redemption["helper_id"],),
            ).fetchone()[0]
            if balance < redemption["points_cost"]:
                return _page(request, error="That helper no longer has enough points for this reward.")
            connection.execute(
                """INSERT INTO kids_points_ledger
                   (helper_id, redemption_id, points_change, entry_type, description, entered_by)
                   VALUES (?, ?, ?, 'reward_redemption', ?, ?)""",
                (redemption["helper_id"], redemption_id, -redemption["points_cost"],
                 f"Reward: {redemption['reward_name']}", reviewed_by.strip()[:60]),
            )
        connection.execute(
            """UPDATE kids_reward_redemptions SET status=?, reviewed_at=CURRENT_TIMESTAMP,
               reviewed_by=? WHERE redemption_id=?""",
            (decision, reviewed_by.strip()[:60], redemption_id),
        )
    return RedirectResponse("/kids", status_code=303)


def install_kids_helper(app) -> None:
    _initialize()
    app.include_router(router)

    @app.middleware("http")
    async def kids_mode_lock(request: Request, call_next):
        # Only the separate child-PIN kiosk session locks the browser into
        # Kids Mode. A signed-in Store Helper remains free to use approved
        # work screens such as Batch Scan and Inventory Search.
        helper = _cookie_helper(request)
        path = request.url.path
        allowed = path.startswith("/kids") or path.startswith("/static/") or path in {
            "/manifest.webmanifest", "/service-worker.js", "/notifications/service-worker.js"
        }
        if helper and not allowed:
            if request.method != "GET" or path.startswith("/api/"):
                return JSONResponse({"detail": "Kids Helper Mode blocks this action. An adult must exit Kids Mode first."}, status_code=403)
            return RedirectResponse("/kids", status_code=303)
        return await call_next(request)
