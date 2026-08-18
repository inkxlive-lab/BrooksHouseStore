#!/usr/bin/env python3
"""Read-only integrity and completeness audit for BrooksHouse Kids Points."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse


PIECE_DETAILS_PATTERN = re.compile(r"^\s*(\d+)\s+pieces committed\b", re.I)


def database_from_url(value: str) -> Path | None:
    if not value.lower().startswith("sqlite"):
        return None
    parsed = urlparse(value)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"/{parsed.netloc}{path}"
    if not path:
        return None
    if value.startswith("sqlite:////"):
        return Path(path)
    return Path(path.lstrip("/"))


def is_live_database(path: Path) -> bool:
    if not path.is_file():
        return False
    lowered = str(path).lower()
    if "backup" in lowered or "snapshot" in lowered:
        return False
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()
    except sqlite3.Error:
        return False
    return {"kids_helpers", "kids_points_ledger", "kids_point_rules"}.issubset(tables)


def resolve_database(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for variable in (
        "BROOKSHOUSE_DATABASE_PATH",
        "DATABASE_PATH",
        "DB_PATH",
        "SQLITE_DB_PATH",
    ):
        value = os.getenv(variable)
        if value:
            candidates.append(Path(value))
    url_path = database_from_url(os.getenv("DATABASE_URL", ""))
    if url_path is not None:
        candidates.append(url_path)
    try:
        from app.database.connection import engine

        if getattr(engine.url, "database", None):
            candidates.append(Path(str(engine.url.database)))
    except Exception:
        pass
    candidates.extend(
        [
            Path("/data/app-data/brookshouse_store.db"),
            Path("/data/brookshouse_store.db"),
            Path("/app/app/data/brookshouse_store.db"),
            Path("app/data/brookshouse_store.db"),
            Path(r"C:\BrooksHouseStore\app\data\brookshouse_store.db"),
        ]
    )
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if is_live_database(resolved):
            return resolved
    raise RuntimeError("Could not locate the live BrooksHouse database; use --database.")


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


class Audit:
    def __init__(self, detail_limit: int):
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passes: list[str] = []
        self.detail_limit = detail_limit

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def passed(self, message: str) -> None:
        self.passes.append(message)

    def print_results(self) -> None:
        print("\nINTEGRITY CHECKS")
        print("=" * 72)
        for message in self.passes:
            print(f"PASS | {message}")
        for message in self.warnings[: self.detail_limit]:
            print(f"WARN | {message}")
        if len(self.warnings) > self.detail_limit:
            print(f"WARN | ... plus {len(self.warnings) - self.detail_limit} warnings")
        for message in self.failures[: self.detail_limit]:
            print(f"FAIL | {message}")
        if len(self.failures) > self.detail_limit:
            print(f"FAIL | ... plus {len(self.failures) - self.detail_limit} failures")
        print("\nFINAL AUDIT STATUS")
        print("=" * 72)
        if self.failures:
            print(f"FAIL — {len(self.failures)} integrity problem(s) found")
        elif self.warnings:
            print(f"REVIEW — no broken ledger math; {len(self.warnings)} warning(s)")
        else:
            print("PASS — balances and traceable credits reconcile")


def audit_helpers(connection: sqlite3.Connection, audit: Audit) -> list[sqlite3.Row]:
    helpers = connection.execute(
        "SELECT * FROM kids_helpers WHERE active=1 ORDER BY helper_name COLLATE NOCASE"
    ).fetchall()
    if not helpers:
        audit.fail("No active kids helper profiles exist.")
        return []

    print("\nHELPER POINT BALANCES")
    print("=" * 72)
    for helper in helpers:
        breakdown_rows = connection.execute(
            """
            SELECT entry_type, COUNT(*) AS entries, COALESCE(SUM(points_change),0) AS points
            FROM kids_points_ledger
            WHERE helper_id=?
            GROUP BY entry_type
            ORDER BY entry_type
            """,
            (helper["helper_id"],),
        ).fetchall()
        totals = connection.execute(
            """
            SELECT COALESCE(SUM(points_change),0) AS balance,
                   COALESCE(SUM(CASE WHEN points_change > 0 THEN points_change ELSE 0 END),0) AS earned,
                   COALESCE(SUM(CASE WHEN points_change < 0 THEN -points_change ELSE 0 END),0) AS spent
            FROM kids_points_ledger WHERE helper_id=?
            """,
            (helper["helper_id"],),
        ).fetchone()
        balance = int(totals["balance"])
        earned = int(totals["earned"])
        spent = int(totals["spent"])
        print(
            f"{helper['helper_name']} (helper #{helper['helper_id']}, "
            f"app user {helper['app_user_id']}): balance {balance}; "
            f"positive ledger points {earned}; deductions {spent}"
        )
        for row in breakdown_rows:
            print(
                f"  {row['entry_type']}: {row['entries']} entries, "
                f"{int(row['points']):+d} points"
            )
    audit.passed("Displayed balance formula equals the sum of points ledger entries.")
    return helpers


def audit_activity_awards(connection: sqlite3.Connection, audit: Audit) -> None:
    missing_ledgers = connection.execute(
        """
        SELECT a.activity_award_id, a.helper_id, a.points_awarded
        FROM kids_activity_awards AS a
        LEFT JOIN kids_points_ledger AS ledger
          ON ledger.activity_award_id=a.activity_award_id
         AND ledger.entry_type='activity_award'
        WHERE ledger.points_entry_id IS NULL
        """
    ).fetchall()
    for row in missing_ledgers:
        audit.fail(
            f"Activity award #{row['activity_award_id']} for helper "
            f"#{row['helper_id']} has no points-ledger entry."
        )

    mismatched = connection.execute(
        """
        SELECT a.activity_award_id, a.helper_id, a.points_awarded,
               ledger.points_change, ledger.helper_id AS ledger_helper_id
        FROM kids_activity_awards AS a
        JOIN kids_points_ledger AS ledger
          ON ledger.activity_award_id=a.activity_award_id
         AND ledger.entry_type='activity_award'
        WHERE ledger.points_change != a.points_awarded
           OR ledger.helper_id != a.helper_id
        """
    ).fetchall()
    for row in mismatched:
        audit.fail(
            f"Activity award #{row['activity_award_id']} does not match its ledger entry."
        )

    orphan_ledgers = connection.execute(
        """
        SELECT ledger.points_entry_id, ledger.helper_id, ledger.activity_award_id
        FROM kids_points_ledger AS ledger
        LEFT JOIN kids_activity_awards AS a
          ON a.activity_award_id=ledger.activity_award_id
        WHERE ledger.entry_type='activity_award'
          AND (ledger.activity_award_id IS NULL OR a.activity_award_id IS NULL)
        """
    ).fetchall()
    for row in orphan_ledgers:
        audit.fail(
            f"Activity ledger entry #{row['points_entry_id']} has no valid activity award."
        )

    duplicates = connection.execute(
        """
        SELECT activity_award_id, COUNT(*) AS copies
        FROM kids_points_ledger
        WHERE activity_award_id IS NOT NULL
        GROUP BY activity_award_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicates:
        audit.fail(
            f"Activity award #{row['activity_award_id']} was credited {row['copies']} times."
        )

    if not (missing_ledgers or mismatched or orphan_ledgers or duplicates):
        audit.passed("Every activity award has exactly one matching ledger credit.")


def audit_progress(connection: sqlite3.Connection, audit: Audit) -> None:
    rules = connection.execute(
        """
        SELECT * FROM kids_point_rules
        WHERE rule_type='activity'
        ORDER BY rule_id
        """
    ).fetchall()
    print("\nACTIVE AND HISTORICAL ACTIVITY RULES")
    print("=" * 72)
    for rule in rules:
        print(
            f"rule #{rule['rule_id']} | {rule['rule_name']} | "
            f"key {rule['activity_key']} | {rule['units_required']} units = "
            f"{rule['points_awarded']} points | active={rule['active']}"
        )

    progress_rows = connection.execute(
        """
        SELECT p.*, h.helper_name, r.activity_key, r.units_required
        FROM kids_activity_progress AS p
        JOIN kids_helpers AS h ON h.helper_id=p.helper_id
        JOIN kids_point_rules AS r ON r.rule_id=p.rule_id
        ORDER BY h.helper_name, r.rule_id
        """
    ).fetchall()

    for row in progress_rows:
        event_rows = connection.execute(
            """
            SELECT details FROM kids_activity_events
            WHERE helper_id=? AND rule_id=?
            """,
            (row["helper_id"], row["rule_id"]),
        ).fetchall()
        if row["activity_key"] == "batch_pieces_processed":
            calculated_lifetime = 0
            unparseable = 0
            for event in event_rows:
                match = PIECE_DETAILS_PATTERN.match(event["details"] or "")
                if match:
                    calculated_lifetime += int(match.group(1))
                else:
                    unparseable += 1
            if unparseable:
                audit.warn(
                    f"{row['helper_name']} has {unparseable} piece event(s) whose "
                    "quantity could not be read from details."
                )
        else:
            calculated_lifetime = len(event_rows)

        if calculated_lifetime != int(row["lifetime_count"]):
            audit.fail(
                f"{row['helper_name']} rule {row['activity_key']}: activity events "
                f"represent {calculated_lifetime} units but progress lifetime says "
                f"{row['lifetime_count']}."
            )
        else:
            audit.passed(
                f"{row['helper_name']} {row['activity_key']} lifetime count "
                f"reconciles at {calculated_lifetime}."
            )

        required = max(1, int(row["units_required"] or 1))
        expected_remainder = int(row["lifetime_count"]) % required
        if int(row["progress_count"]) != expected_remainder:
            audit.warn(
                f"{row['helper_name']} rule {row['activity_key']}: stored progress "
                f"is {row['progress_count']}, while current-rule remainder is "
                f"{expected_remainder}. The rule may have changed after credits began."
            )


def audit_tasks(connection: sqlite3.Connection, audit: Audit) -> None:
    task_columns = table_columns(connection, "kids_helper_tasks")
    required = {"points_awarded", "points_value", "approval_status", "status"}
    if not required.issubset(task_columns):
        audit.warn("Task-credit audit skipped because the task table is an older schema.")
        return
    problems = connection.execute(
        """
        SELECT task.task_id, task.helper_id, task.points_value,
               task.points_awarded, task.status, task.approval_status,
               ledger.points_entry_id, ledger.points_change
        FROM kids_helper_tasks AS task
        LEFT JOIN kids_points_ledger AS ledger
          ON ledger.task_id=task.task_id AND ledger.entry_type='task_award'
        WHERE (
            task.points_awarded=1
            AND task.points_value > 0
            AND (
                ledger.points_entry_id IS NULL
                OR ledger.points_change != task.points_value
                OR ledger.helper_id != task.helper_id
            )
        ) OR (
            ledger.points_entry_id IS NOT NULL
            AND (
                task.points_awarded != 1
                OR task.approval_status != 'approved'
            )
        )
        """
    ).fetchall()
    for row in problems:
        audit.fail(f"Directed task #{row['task_id']} has inconsistent points credit.")
    duplicates = connection.execute(
        """
        SELECT task_id, COUNT(*) copies
        FROM kids_points_ledger
        WHERE task_id IS NOT NULL AND entry_type='task_award'
        GROUP BY task_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicates:
        audit.fail(f"Directed task #{row['task_id']} was credited {row['copies']} times.")
    if not (problems or duplicates):
        audit.passed("Directed-task approvals and ledger credits reconcile.")


def audit_redemptions(connection: sqlite3.Connection, audit: Audit) -> None:
    problems = connection.execute(
        """
        SELECT redemption.redemption_id, redemption.helper_id,
               redemption.points_cost, redemption.status,
               ledger.points_entry_id, ledger.points_change
        FROM kids_reward_redemptions AS redemption
        LEFT JOIN kids_points_ledger AS ledger
          ON ledger.redemption_id=redemption.redemption_id
         AND ledger.entry_type='reward_redemption'
        WHERE (
            redemption.status='approved'
            AND (
                ledger.points_entry_id IS NULL
                OR ledger.points_change != -redemption.points_cost
                OR ledger.helper_id != redemption.helper_id
            )
        ) OR (
            redemption.status != 'approved'
            AND ledger.points_entry_id IS NOT NULL
        )
        """
    ).fetchall()
    for row in problems:
        audit.fail(f"Reward redemption #{row['redemption_id']} has inconsistent deduction.")
    duplicates = connection.execute(
        """
        SELECT redemption_id, COUNT(*) copies
        FROM kids_points_ledger
        WHERE redemption_id IS NOT NULL AND entry_type='reward_redemption'
        GROUP BY redemption_id HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicates:
        audit.fail(
            f"Reward redemption #{row['redemption_id']} was deducted {row['copies']} times."
        )
    if not (problems or duplicates):
        audit.passed("Reward approvals and ledger deductions reconcile.")


def audit_piece_batches(
    connection: sqlite3.Connection,
    helpers: list[sqlite3.Row],
    audit: Audit,
) -> None:
    transaction_columns = table_columns(connection, "inventory_transactions")
    if "performed_by_user_id" not in transaction_columns:
        audit.warn(
            "Saved-batch completeness check skipped: transactions do not record app user IDs."
        )
        return

    helper_by_user = {
        int(helper["app_user_id"]): helper
        for helper in helpers
        if helper["app_user_id"] is not None
    }
    if not helper_by_user:
        audit.warn("No active helper is linked to an app user; batch completeness is unknown.")
        return

    batches = connection.execute(
        """
        SELECT reference_number, performed_by_user_id,
               SUM(quantity_change) AS pieces,
               MIN(created_at) AS created_at
        FROM inventory_transactions
        WHERE transaction_type='batch_adjustment_add'
          AND quantity_change > 0
          AND reference_number IS NOT NULL
          AND TRIM(reference_number) != ''
          AND performed_by_user_id IS NOT NULL
        GROUP BY reference_number, performed_by_user_id
        ORDER BY created_at
        """
    ).fetchall()

    attributable = 0
    missing = []
    quantity_mismatches = []
    for batch in batches:
        app_user_id = int(batch["performed_by_user_id"])
        helper = helper_by_user.get(app_user_id)
        if helper is None:
            continue
        attributable += 1
        digest = hashlib.sha256(
            f"{app_user_id}:{batch['reference_number']}".encode("utf-8")
        ).hexdigest()[:32]
        source_key = f"committed-pieces-{digest}"
        event = connection.execute(
            """
            SELECT * FROM kids_activity_events
            WHERE source_event_key=? AND helper_id=?
            """,
            (source_key, helper["helper_id"]),
        ).fetchone()
        if event is None:
            missing.append((helper["helper_name"], batch["reference_number"], batch["pieces"]))
            continue
        match = PIECE_DETAILS_PATTERN.match(event["details"] or "")
        event_pieces = int(match.group(1)) if match else None
        if event_pieces != int(batch["pieces"]):
            quantity_mismatches.append(
                (
                    helper["helper_name"],
                    batch["reference_number"],
                    int(batch["pieces"]),
                    event_pieces,
                )
            )

    for name, reference, pieces in missing:
        audit.fail(
            f"{name} batch {reference} committed {pieces} pieces but has no piece event."
        )
    for name, reference, pieces, event_pieces in quantity_mismatches:
        audit.fail(
            f"{name} batch {reference}: transaction total is {pieces} pieces but "
            f"reward event records {event_pieces}."
        )
    if attributable and not (missing or quantity_mismatches):
        audit.passed(
            f"All {attributable} attributable committed batches have matching piece events."
        )
    elif not attributable:
        audit.warn(
            "No committed batches with linked helper user IDs were found; historical "
            "piece completeness cannot be confirmed from transactions."
        )

    unattributed = connection.execute(
        """
        SELECT COUNT(DISTINCT reference_number)
        FROM inventory_transactions
        WHERE transaction_type='batch_adjustment_add'
          AND quantity_change > 0
          AND (performed_by_user_id IS NULL)
        """
    ).fetchone()[0]
    if unattributed:
        audit.warn(
            f"{unattributed} historical batch reference(s) lack an app user ID and "
            "cannot be assigned to a child for completeness checking."
        )


def audit_source_duplicates(connection: sqlite3.Connection, audit: Audit) -> None:
    duplicates = connection.execute(
        """
        SELECT source_event_key, COUNT(*) copies
        FROM kids_activity_events
        GROUP BY source_event_key HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in duplicates:
        audit.fail(
            f"Activity source key {row['source_event_key']} appears {row['copies']} times."
        )
    if not duplicates:
        audit.passed("No duplicate activity source keys exist.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only BrooksHouse kids-points audit")
    parser.add_argument("--database", help="Explicit SQLite database path")
    parser.add_argument("--detail-limit", type=int, default=100)
    arguments = parser.parse_args()

    database_path = resolve_database(arguments.database)
    print(f"Database: {database_path}")
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    audit = Audit(max(1, arguments.detail_limit))
    try:
        required_tables = {
            "kids_helpers",
            "kids_points_ledger",
            "kids_point_rules",
            "kids_activity_events",
            "kids_activity_progress",
            "kids_activity_awards",
            "kids_helper_tasks",
            "kids_reward_redemptions",
            "inventory_transactions",
        }
        missing = sorted(table for table in required_tables if not table_exists(connection, table))
        if missing:
            raise RuntimeError(f"Missing required tables: {missing}")

        helpers = audit_helpers(connection, audit)
        audit_activity_awards(connection, audit)
        audit_progress(connection, audit)
        audit_tasks(connection, audit)
        audit_redemptions(connection, audit)
        audit_piece_batches(connection, helpers, audit)
        audit_source_duplicates(connection, audit)
        audit.print_results()

        print("\nSCAN-AUDIT LIMITATION")
        print("=" * 72)
        print(
            "The database can prove that saved scan events were not duplicated and "
            "that their awards reached the ledger. It cannot reconstruct scanner "
            "trigger pulls that never became a committed server event. Piece-batch "
            "completeness is checked separately from inventory transactions."
        )
    finally:
        connection.close()
    return 2 if audit.failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
