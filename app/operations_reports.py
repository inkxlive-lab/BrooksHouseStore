from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.database_resolution import configured_sqlite_path, require_application_database_match
from app.marketplace_order_service import load_marketplace_orders
from app.services.marketplace_order_ingestion import ensure_marketplace_operations_schema, run_sync_cycle, sync_health

CENTRAL = ZoneInfo("America/Chicago")
REPORT_TYPES = {"active": "Today's Active Orders", "master_pull": "Printable Master Pull List",
                "due_today": "Orders Due Today", "staged": "Staged but Not Shipped",
                "exceptions": "Fulfillment Exceptions", "reconciliation": "Marketplace Reconciliation Report"}
TERMINAL = {"shipped", "cancelled", "canceled", "refunded", "closed", "completed"}


@contextmanager
def _connect(database=None, *, allow_fixture=False, ensure_schema=False):
    target = Path(database).resolve() if database else configured_sqlite_path()
    if not allow_fixture:
        target = require_application_database_match(target)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    if ensure_schema:
        ensure_marketplace_operations_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def parse_marketplace_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        number = float(text)
        if abs(number) >= 100_000_000_000:
            number /= 1000.0
        return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def central_datetime(value: Any) -> datetime | None:
    parsed = parse_marketplace_datetime(value)
    return parsed.astimezone(CENTRAL) if parsed else None


def central_date(value: Any) -> date | None:
    parsed = central_datetime(value)
    return parsed.date() if parsed else None


def friendly_central(value: Any) -> str:
    parsed = central_datetime(value)
    return parsed.strftime("%a %m/%d/%Y %I:%M %p CT") if parsed else "Unknown"


def _quantity_progress(order: dict, line: dict) -> dict[str, int]:
    required = max(0, int(line.get("quantity") or 0))
    stage = str(order.get("local_status") or "new").casefold()
    picked = min(required, max(0, int(line.get("pulled_quantity") or 0)))
    packed = required if stage in {"packed", "staged", "shipment_submitted", "shipped"} else 0
    staged = required if stage in {"staged", "shipment_submitted", "shipped"} else 0
    marketplace = str(order.get("marketplace_status") or order.get("walmart_status") or "").casefold()
    shipped = required if order.get("is_terminal") and marketplace in {"shipped", "completed", "delivered"} else 0
    return {"required": required, "picked": picked, "packed": packed, "staged": staged, "shipped": shipped,
            "remaining": max(0, required - max(picked, staged, shipped))}


def _mapping_details(order: dict, line: dict) -> dict[str, Any]:
    confirmed_id = line.get("confirmed_product_id") or line.get("mapped_product_id")
    if order.get("channel_key") == "amazon" and line.get("product_id"):
        confirmed_id = line.get("product_id")
    candidate = bool(line.get("candidate_inventory_found") or (not confirmed_id and line.get("inventory_options")))
    status = "mapped" if confirmed_id else "unmatched_candidate" if candidate else "unmatched"
    sku = str(line.get("sku") or "")
    action = (f"/channels/walmart/orders?mapping_search={quote_plus(sku)}" if order.get("channel_key") == "walmart"
              else f"/channels/amazon/mapping?search={quote_plus(sku)}")
    return {"confirmed_product_id": confirmed_id, "mapping_status": status,
            "candidate_inventory_found": candidate, "action_url": action}


def _line_exception(order: dict, mapping: dict, available: int, remaining: int) -> tuple[str, str]:
    if mapping["mapping_status"] == "unmatched_candidate":
        return "unmatched_candidate", "Unmatched — candidate inventory found"
    if mapping["mapping_status"] == "unmatched":
        return "unmatched", "Unmatched — no confirmed BrooksHouse product"
    if available <= 0:
        return "mapped_unlocated", "Mapped — unlocated"
    if available < remaining:
        return "mapped_insufficient", "Mapped — insufficient"
    if order.get("is_overdue"):
        return "overdue", "Mapped — ready, order overdue"
    marketplace = str(order.get("marketplace_status") or order.get("walmart_status") or "").casefold()
    local = str(order.get("local_status") or "").casefold()
    if any(marker in marketplace for marker in ("ship", "cancel", "refund", "closed")) and local not in TERMINAL:
        return "status_conflict", "Channel/local status conflict"
    return "", "Mapped — ready"


def _base_orders(orders: list[dict], filters: dict) -> list[dict]:
    channel = str(filters.get("channel") or "all").casefold()
    stage = str(filters.get("stage") or "all").casefold()
    site = str(filters.get("physical_site") or "all").casefold()
    include_staged = bool(filters.get("include_staged", True))
    excluded = {str(value).casefold() for value in filters.get("exclude_channels", [])}
    stale_allowed = {str(value).casefold() for value in filters.get("allow_stale_channels", [])}
    start = date.fromisoformat(filters["ship_start"]) if filters.get("ship_start") else None
    end = date.fromisoformat(filters["ship_end"]) if filters.get("ship_end") else None
    selected = []
    for order in orders:
        channel_key = str(order.get("channel_key") or "").casefold()
        local = str(order.get("local_status") or "new").casefold()
        if order.get("is_terminal") or local in TERMINAL:
            continue
        if not order.get("is_actionable") and not (channel_key in stale_allowed and not order.get("is_terminal")):
            continue
        if channel_key in excluded or (channel != "all" and channel_key != channel):
            continue
        if stage != "all" and local != stage:
            continue
        if not include_staged and local == "staged":
            continue
        sites = " ".join(str(value) for value in order.get("site_names") or []).casefold()
        if site != "all" and site not in sites:
            continue
        due = central_date(order.get("ship_by_date"))
        if start and (due is None or due < start):
            continue
        if end and (due is None or due > end):
            continue
        selected.append(order)
    return selected


def _order_rows(orders: list[dict]) -> list[dict]:
    rows, now = [], datetime.now(CENTRAL)
    for order in orders:
        due = central_datetime(order.get("ship_by_date"))
        for line in order.get("lines") or []:
            progress, mapping = _quantity_progress(order, line), _mapping_details(order, line)
            locations = [{"site": stock.get("site_name") or stock.get("location_name") or "Unknown site",
                          "location": stock.get("location_name") or "Unlocated",
                          "container": stock.get("container_id") or "Loose", "tote": stock.get("container_id") or "",
                          "available": max(0, int(stock.get("available_quantity") or 0))}
                         for stock in line.get("inventory_options") or []]
            available = sum(location["available"] for location in locations)
            exception_code, exception = _line_exception(order, mapping, available, progress["remaining"])
            parsed_due = parse_marketplace_datetime(order.get("ship_by_date"))
            rows.append({"channel": order.get("channel"), "channel_key": order.get("channel_key"),
                         "order_id": order.get("purchase_order_id"),
                         "marketplace_status": order.get("marketplace_status") or order.get("walmart_status") or "Unknown",
                         "fulfillment_stage": order.get("local_status") or "new",
                         "ordered_time_central": friendly_central(order.get("order_date")),
                         "ship_by_central": friendly_central(order.get("ship_by_date")),
                         "ship_by_utc": parsed_due.isoformat() if parsed_due else None,
                         "overdue": bool(due and due < now), "product": line.get("item_name") or "Unknown item",
                         "sku": line.get("sku") or "", "barcode": line.get("product_barcode") or line.get("upc") or "",
                         "quantity_required": progress["required"], "quantity_picked": progress["picked"],
                         "quantity_packed": progress["packed"], "quantity_staged": progress["staged"],
                         "quantity_shipped": progress["shipped"], "remaining_to_pull": progress["remaining"],
                         "mapping_status": mapping["mapping_status"], "confirmed_product_id": mapping["confirmed_product_id"],
                         "inventory_readiness": "ready" if available >= progress["remaining"] else "insufficient" if available else "unlocated",
                         "available": available, "locations": locations, "exception_code": exception_code,
                         "exception": exception, "action_url": mapping["action_url"]})
    return rows


def _aggregate(order_rows: list[dict], *, remaining_only: bool) -> tuple[list[dict], list[dict]]:
    grouped: dict[str, dict] = {}
    for line in order_rows:
        confirmed = line.get("confirmed_product_id")
        key = f"product:{confirmed}" if confirmed else f"unmatched:{line['channel_key']}:{line['sku']}:{line['barcode']}"
        row = grouped.setdefault(key, {"key": key, "confirmed_product_id": confirmed, "product": line["product"],
            "barcode": line["barcode"], "skus": set(), "units_required": 0, "units_picked_staged": 0,
            "remaining_to_pull": 0, "available_units": 0, "orders": [], "locations": {},
            "mapping_status": line["mapping_status"], "exception_code": "", "exception": "", "action_url": line["action_url"]})
        row["skus"].add(line["sku"]); row["units_required"] += line["quantity_required"]
        row["units_picked_staged"] += max(line["quantity_picked"], line["quantity_staged"])
        row["remaining_to_pull"] += line["remaining_to_pull"]
        row["orders"].append({"channel": line["channel"], "order_id": line["order_id"],
                              "quantity": line["quantity_required"], "ship_by_central": line["ship_by_central"]})
        for location in line["locations"]:
            location_key = (location["site"], location["location"], location["container"], location["tote"])
            row["locations"][location_key] = max(row["locations"].get(location_key, 0), location["available"])
        if line["exception_code"] and not row["exception_code"]:
            row["exception_code"], row["exception"] = line["exception_code"], line["exception"]
    rows = []
    for row in grouped.values():
        row["skus"] = sorted(value for value in row["skus"] if value)
        row["locations"] = [{"site": key[0], "location": key[1], "container": key[2], "tote": key[3], "available": qty}
                            for key, qty in sorted(row["locations"].items())]
        row["available_units"] = sum(location["available"] for location in row["locations"])
        row["recommended_location"] = max(row["locations"], key=lambda value: value["available"], default=None)
        row["shortage_quantity"] = max(0, row["remaining_to_pull"] - row["available_units"])
        if row["mapping_status"] == "mapped" and not row["locations"]:
            row["exception_code"], row["exception"] = "mapped_unlocated", "Mapped — unlocated"
        elif row["mapping_status"] == "mapped" and row["shortage_quantity"]:
            row["exception_code"], row["exception"] = "mapped_insufficient", "Mapped — insufficient"
        elif row["mapping_status"] == "mapped" and not row["exception_code"]:
            row["exception"] = "Mapped — ready"
        if not remaining_only or row["remaining_to_pull"] > 0:
            rows.append(row)
    rows.sort(key=lambda row: (bool(row["exception_code"]), row["product"].casefold()))
    return rows, [row for row in rows if row["exception_code"]]


def create_report_snapshot(*, report_type: str, filters: dict, freshness: dict, warnings: list[str],
                           actor: str = "BrooksHouse user", refresh_metadata: dict | None = None,
                           database=None, allow_fixture=False, today_central: date | None = None) -> int:
    if report_type not in REPORT_TYPES:
        raise ValueError("Unknown operations report type")
    orders = _base_orders(load_marketplace_orders(database, allow_fixture=allow_fixture), filters)
    today = today_central or datetime.now(CENTRAL).date()
    if report_type == "due_today":
        orders = [order for order in orders if central_date(order.get("ship_by_date")) == today]
    elif report_type == "staged":
        orders = [order for order in orders if str(order.get("local_status") or "").casefold() in {"packed", "staged"}]
    order_rows = _order_rows(orders)
    if report_type == "exceptions":
        order_rows = [row for row in order_rows if row["exception_code"] or row["overdue"]]
        allowed = {(row["channel_key"], row["order_id"]) for row in order_rows}
        orders = [order for order in orders if (order.get("channel_key"), order.get("purchase_order_id")) in allowed]
    pull_rows, exceptions = _aggregate(order_rows, remaining_only=report_type == "master_pull")
    if report_type == "master_pull":
        contributing = {(str(order["channel"]).casefold(), order["order_id"]) for row in pull_rows for order in row["orders"]}
        orders = [order for order in orders if (str(order.get("channel_key")), order.get("purchase_order_id")) in contributing]
        order_rows = [row for row in order_rows if row["remaining_to_pull"] > 0]
    walmart, amazon = ([order for order in orders if order.get("channel_key") == key] for key in ("walmart", "amazon"))
    totals = {"active_orders": len(orders), "active_lines": len(order_rows),
        "units_required": sum(row["quantity_required"] for row in order_rows),
        "remaining_units_to_pull": sum(row["remaining_to_pull"] for row in order_rows),
        "unique_aggregated_products": len(pull_rows), "walmart_orders": len(walmart),
        "walmart_units": sum(int(order.get("unit_count") or 0) for order in walmart), "amazon_orders": len(amazon),
        "amazon_units": sum(int(order.get("unit_count") or 0) for order in amazon), "exceptions": len(exceptions),
        "refresh_timestamp": datetime.now(timezone.utc).isoformat()}
    reconciliation_events = []
    if report_type == "reconciliation":
        with _connect(database, allow_fixture=allow_fixture) as connection:
            reconciliation_events = [dict(row) for row in connection.execute(
                "SELECT * FROM marketplace_status_audit ORDER BY audit_id DESC LIMIT 500")]
    payload = {"report_type": report_type, "report_title": REPORT_TYPES[report_type], "filters": filters,
        "freshness": freshness, "warnings": warnings, "totals": totals, "orders": orders,
        "order_rows": order_rows, "pull_rows": pull_rows, "exceptions": exceptions,
        "reconciliation_events": reconciliation_events, "refresh": refresh_metadata or {},
        "timezone": "America/Chicago", "incomplete": bool(warnings)}
    snapshot = _json(payload); digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    with _connect(database, allow_fixture=allow_fixture) as connection:
        cursor = connection.execute("""INSERT INTO operations_report_runs
            (report_type,created_at,created_by,filters_json,freshness_json,warnings_json,totals_json,snapshot_json,snapshot_sha256)
            VALUES(?,?,?,?,?,?,?,?,?)""", (report_type, datetime.now(timezone.utc).isoformat(), actor, _json(filters),
            _json(freshness), _json(warnings), _json(totals), snapshot, digest))
        connection.commit(); return int(cursor.lastrowid)


def load_snapshot(report_run_id: int, database=None, *, allow_fixture=False) -> tuple[dict, dict]:
    with _connect(database, allow_fixture=allow_fixture) as connection:
        row = connection.execute("SELECT * FROM operations_report_runs WHERE report_run_id=?", (report_run_id,)).fetchone()
        if row is None: raise KeyError(report_run_id)
        return dict(row), json.loads(row["snapshot_json"])


def _recent_success_usable(health: dict, channel: str) -> tuple[bool, str | None]:
    success = health.get("channels", {}).get(channel, {}).get("last_success")
    if not success or not success.get("finished_at"): return False, None
    parsed = parse_marketplace_datetime(success["finished_at"])
    window = max(1, int(os.getenv("MARKETPLACE_REPORT_STALE_FALLBACK_MINUTES", "60")))
    usable = bool(parsed and (datetime.now(timezone.utc) - parsed).total_seconds() <= window * 60)
    return usable, friendly_central(success["finished_at"])


ACTIVE_JOB_STATES = {"queued", "refreshing", "generating"}
_JOB_THREADS: dict[int, threading.Thread] = {}
_JOB_THREADS_LOCK = threading.Lock()


def _fresh_channels(health: dict, channels: list[str]) -> tuple[bool, list[str]]:
    window = max(1, int(os.getenv("MARKETPLACE_REPORT_FRESH_MINUTES", "15")))
    stale = []
    now = datetime.now(timezone.utc)
    for channel in channels:
        success = health.get("channels", {}).get(channel, {}).get("last_success")
        parsed = parse_marketplace_datetime(success.get("finished_at")) if success else None
        if not parsed or (now - parsed).total_seconds() > window * 60:
            stale.append(channel)
    return not stale, stale


def recover_stale_report_jobs(database=None, *, allow_fixture=False, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    timeout = max(30, int(os.getenv("OPERATIONS_REPORT_JOB_STALE_SECONDS", "600")))
    cutoff = (now.timestamp() - timeout)
    recovered = 0
    with _connect(database, allow_fixture=allow_fixture) as connection:
        rows = connection.execute(
            "SELECT report_job_id,updated_at FROM operations_report_jobs WHERE state IN ('queued','refreshing','generating')"
        ).fetchall()
        for row in rows:
            updated = parse_marketplace_datetime(row["updated_at"])
            if not updated or updated.timestamp() <= cutoff:
                connection.execute(
                    "UPDATE operations_report_jobs SET state='failed',finished_at=?,updated_at=?,"
                    "progress_message='Recovered stale/incomplete report job',error_message='The prior report job stopped or exceeded its safe time limit.' "
                    "WHERE report_job_id=? AND state IN ('queued','refreshing','generating')",
                    (now.isoformat(), now.isoformat(), row["report_job_id"]),
                )
                recovered += 1
        connection.commit()
    return recovered


def enqueue_report_job(*, report_type: str, mode: str, filters: dict, actor: str,
                       database=None, allow_fixture=False, start: bool = True) -> tuple[int, bool]:
    if report_type not in REPORT_TYPES or mode not in {"current", "refresh"}:
        raise ValueError("Invalid report request")
    recover_stale_report_jobs(database, allow_fixture=allow_fixture)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(database, allow_fixture=allow_fixture) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT report_job_id FROM operations_report_jobs WHERE state IN ('queued','refreshing','generating') "
            "ORDER BY report_job_id LIMIT 1"
        ).fetchone()
        if active:
            connection.rollback()
            return int(active["report_job_id"]), False
        cursor = connection.execute(
            "INSERT INTO operations_report_jobs(report_type,mode,state,requested_at,updated_at,requested_by,filters_json,progress_message) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (report_type, mode, "queued", now, now, actor[:100], _json(filters), "Queued for report generation"),
        )
        job_id = int(cursor.lastrowid)
        connection.commit()
    if start:
        start_report_job(job_id, database=database, allow_fixture=allow_fixture)
    return job_id, True


def load_report_job(job_id: int, database=None, *, allow_fixture=False) -> dict:
    with _connect(database, allow_fixture=allow_fixture) as connection:
        row = connection.execute("SELECT * FROM operations_report_jobs WHERE report_job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)


def _update_job(job_id: int, state: str, message: str, *, database=None, allow_fixture=False,
                result_run_id: int | None = None, error: str | None = None, finished: bool = False) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(database, allow_fixture=allow_fixture) as connection:
        connection.execute(
            "UPDATE operations_report_jobs SET state=?,updated_at=?,progress_message=?,result_report_run_id=?,"
            "error_message=?,started_at=COALESCE(started_at,?),finished_at=CASE WHEN ? THEN ? ELSE finished_at END "
            "WHERE report_job_id=?",
            (state, now, message[:500], result_run_id, (error or "")[:1000] or None, now, int(finished), now, job_id),
        )
        connection.commit()


def _bounded_call(callback, timeout_seconds: int):
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="operations-report-bounded")
    future = executor.submit(callback)
    try:
        return future.result(timeout=max(1, timeout_seconds))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _run_report_job(job_id: int, *, database=None, allow_fixture=False) -> None:
    try:
        job = load_report_job(job_id, database, allow_fixture=allow_fixture)
        filters = json.loads(job["filters_json"])
        requested = [filters["channel"]] if filters.get("channel") in {"walmart", "amazon"} else ["walmart", "amazon"]
        health = sync_health(database, allow_fixture=allow_fixture)
        refresh = {}
        warnings: list[str] = []
        if job["mode"] == "current":
            fresh, stale = _fresh_channels(health, requested)
            if not fresh:
                raise RuntimeError(f"Current reconciled data is stale for: {', '.join(stale)}. Use the explicit Refresh channels action.")
        else:
            _update_job(job_id, "refreshing", "Refreshing selected channels and reconciling statuses", database=database, allow_fixture=allow_fixture)
            started, clock = datetime.now(timezone.utc), time.monotonic()
            try:
                results = _bounded_call(lambda: run_sync_cycle(channels=requested, database=database,
                                                                allow_fixture=allow_fixture),
                                        int(os.getenv("OPERATIONS_REPORT_REFRESH_TIMEOUT_SECONDS", "180")))
            except FutureTimeout as error:
                raise RuntimeError("Marketplace refresh exceeded the safe server timeout. It was not queued again; check status before retrying.") from error
            completed = datetime.now(timezone.utc)
            if any(result.get("busy") for result in results.values()):
                raise RuntimeError("Another marketplace refresh is already running. This duplicate request was not queued.")
            health = sync_health(database, allow_fixture=allow_fixture)
            refresh = {"started_at_utc": started.isoformat(), "started_at_central": friendly_central(started),
                       "completed_at_utc": completed.isoformat(), "completed_at_central": friendly_central(completed),
                       "duration_seconds": round(time.monotonic() - clock, 3), "channels_requested": requested, "results": results}
            for name in requested:
                if not results.get(name, {}).get("success"):
                    warnings.append(f"{name.title()} refresh failed: {results.get(name, {}).get('error', 'unknown error')}")
        _update_job(job_id, "generating", "Validating totals and creating immutable snapshot", database=database, allow_fixture=allow_fixture)
        run_id = _bounded_call(
            lambda: create_report_snapshot(report_type=job["report_type"], filters=filters, freshness=health,
                                           warnings=warnings, actor=job["requested_by"] or "BrooksHouse user",
                                           refresh_metadata=refresh, database=database, allow_fixture=allow_fixture),
            int(os.getenv("OPERATIONS_REPORT_GENERATE_TIMEOUT_SECONDS", "60")),
        )
        _update_job(job_id, "complete", "Report snapshot is ready", database=database, allow_fixture=allow_fixture,
                    result_run_id=run_id, finished=True)
    except FutureTimeout:
        _update_job(job_id, "failed", "Report generation timed out", database=database, allow_fixture=allow_fixture,
                    error="Report generation exceeded the safe server timeout.", finished=True)
    except Exception as error:
        _update_job(job_id, "failed", "Report generation failed", database=database, allow_fixture=allow_fixture,
                    error=f"{type(error).__name__}: {error}", finished=True)
    finally:
        with _JOB_THREADS_LOCK:
            _JOB_THREADS.pop(job_id, None)


def start_report_job(job_id: int, *, database=None, allow_fixture=False) -> bool:
    with _JOB_THREADS_LOCK:
        existing = _JOB_THREADS.get(job_id)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(target=_run_report_job, kwargs={"job_id": job_id, "database": database,
                                  "allow_fixture": allow_fixture}, name=f"operations-report-{job_id}", daemon=True)
        _JOB_THREADS[job_id] = thread
        thread.start()
        return True


def _csv_response(rows: list[list[Any]], filename: str) -> Response:
    output = io.StringIO(newline=""); csv.writer(output).writerows(rows)
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def install_operations_reports(app, templates) -> None:
    with _connect(ensure_schema=True) as connection:
        connection.commit()
    recover_stale_report_jobs()

    @app.get("/operations/reports", response_class=HTMLResponse)
    def operations_reports_page(request: Request):
        recover_stale_report_jobs()
        health = sync_health()
        with _connect() as connection:
            history = [dict(row) for row in connection.execute("SELECT report_run_id,report_type,created_at,created_by,warnings_json,totals_json FROM operations_report_runs ORDER BY report_run_id DESC LIMIT 25")]
            jobs = [dict(row) for row in connection.execute(
                "SELECT * FROM operations_report_jobs ORDER BY report_job_id DESC LIMIT 15"
            )]
        for row in history:
            row["warnings"] = json.loads(row.get("warnings_json") or "[]"); row["totals"] = json.loads(row.get("totals_json") or "{}")
        return templates.TemplateResponse(request=request, name="operations_reports.html", context={"report_types": REPORT_TYPES,
            "health": health, "history": history, "jobs": jobs, "message": request.query_params.get("message"), "error": request.query_params.get("error")})

    @app.post("/operations/reports/generate")
    def operations_reports_generate(request: Request, report_type: str = Form("master_pull"), channel: str = Form("all"),
            ship_start: str = Form(""), ship_end: str = Form(""), physical_site: str = Form("all"),
            stage: str = Form("all"), include_staged: str | None = Form(None)):
        return _submit_report_job(request, report_type, channel, ship_start, ship_end, physical_site,
                                  stage, include_staged, mode="current")

    @app.post("/operations/reports/refresh-generate")
    def operations_reports_refresh_generate(request: Request, report_type: str = Form("master_pull"), channel: str = Form("all"),
            ship_start: str = Form(""), ship_end: str = Form(""), physical_site: str = Form("all"),
            stage: str = Form("all"), include_staged: str | None = Form(None)):
        user = getattr(request.state, "auth_user", None)
        if not user or getattr(user, "role", "") != "owner_admin":
            return RedirectResponse("/operations/reports?error=" + quote_plus("Only an owner administrator can explicitly refresh marketplaces."), status_code=303)
        return _submit_report_job(request, report_type, channel, ship_start, ship_end, physical_site,
                                  stage, include_staged, mode="refresh")

    def _submit_report_job(request: Request, report_type: str, channel: str, ship_start: str, ship_end: str,
                           physical_site: str, stage: str, include_staged: str | None, *, mode: str):
        filters = {"channel": channel, "ship_start": ship_start, "ship_end": ship_end,
                   "physical_site": physical_site, "stage": stage, "include_staged": include_staged == "yes",
                   "exclude_channels": [], "allow_stale_channels": []}
        user = getattr(request.state, "auth_user", None)
        actor = str(getattr(user, "display_name", None) or getattr(user, "username", None) or "BrooksHouse user")
        job_id, created = enqueue_report_job(report_type=report_type, mode=mode, filters=filters, actor=actor)
        suffix = "" if created else "?message=" + quote_plus("Another report or refresh is already running; showing its status instead of queuing a duplicate.")
        return RedirectResponse(f"/operations/reports/jobs/{job_id}{suffix}", status_code=303)

    @app.get("/operations/reports/jobs/{job_id}", response_class=HTMLResponse)
    def operations_report_job_status(job_id: int, request: Request):
        recover_stale_report_jobs()
        job = load_report_job(job_id)
        return templates.TemplateResponse(request=request, name="operations_report_job.html",
                                          context={"job": job, "message": request.query_params.get("message")})

    @app.get("/operations/reports/jobs/{job_id}/status")
    def operations_report_job_status_json(job_id: int):
        recover_stale_report_jobs()
        job = load_report_job(job_id)
        return {key: job.get(key) for key in ("report_job_id", "state", "progress_message", "result_report_run_id",
                                              "error_message", "updated_at", "finished_at")}

    @app.get("/operations/reports/{report_run_id}", response_class=HTMLResponse)
    def operations_report_preview(report_run_id: int, request: Request):
        metadata, snapshot = load_snapshot(report_run_id)
        return templates.TemplateResponse(request=request, name="operations_report_snapshot.html", context={"metadata": metadata, "snapshot": snapshot})

    @app.get("/operations/reports/{report_run_id}/export.csv")
    @app.get("/operations/reports/{report_run_id}/export-pull-list.csv")
    def operations_report_pull_csv(report_run_id: int):
        _, snapshot = load_snapshot(report_run_id)
        rows = [["Product", "UPC/barcode", "Marketplace SKU", "Total units required", "Units already picked/staged",
                 "Remaining units to pull", "Total available", "Contributing orders and ship deadlines",
                 "Exact site/location/container/tote quantities", "Recommended pull location", "Shortage quantity",
                 "Mapping/exception status", "Repair action"]]
        for row in snapshot.get("pull_rows", []):
            recommended = row.get("recommended_location") or {}
            rows.append([row["product"], row["barcode"], "; ".join(row["skus"]), row["units_required"], row["units_picked_staged"],
                row["remaining_to_pull"], row["available_units"], "; ".join(f"{o['channel']} {o['order_id']} due {o['ship_by_central']}" for o in row["orders"]),
                "; ".join(f"{x['site']} / {x['location']} / {x['container']} / {x['tote'] or 'no tote'} = {x['available']}" for x in row["locations"]),
                f"{recommended.get('site','')} / {recommended.get('location','')} / {recommended.get('container','')} ({recommended.get('available',0)})" if recommended else "",
                row["shortage_quantity"], row["exception"] or row["mapping_status"], row["action_url"]])
        return _csv_response(rows, f"operations-pull-list-{report_run_id}.csv")

    @app.get("/operations/reports/{report_run_id}/export-orders.csv")
    def operations_report_orders_csv(report_run_id: int):
        _, snapshot = load_snapshot(report_run_id)
        rows = [["Channel", "Marketplace order ID", "Marketplace lifecycle status", "BrooksHouse fulfillment stage",
                 "Ordered/received time (Central)", "Ship-by (Central)", "Overdue", "Product", "SKU", "UPC/barcode",
                 "Quantity required", "Quantity picked", "Quantity packed", "Quantity staged", "Quantity shipped",
                 "Mapping status", "Inventory readiness", "Exception/reason", "Repair action"]]
        for row in snapshot.get("order_rows", []):
            rows.append([row["channel"], row["order_id"], row["marketplace_status"], row["fulfillment_stage"],
                row["ordered_time_central"], row["ship_by_central"], "YES" if row["overdue"] else "NO", row["product"], row["sku"], row["barcode"],
                row["quantity_required"], row["quantity_picked"], row["quantity_packed"], row["quantity_staged"], row["quantity_shipped"],
                row["mapping_status"], row["inventory_readiness"], row["exception"], row["action_url"]])
        return _csv_response(rows, f"operations-orders-{report_run_id}.csv")
