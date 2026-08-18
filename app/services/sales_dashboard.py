from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parents[1]
DB_PATH = APP_DIR / "data" / "brookshouse_store.db"
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")
VALID_CHANNELS = {"all", "shopify", "walmart", "amazon"}


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def filters_for(period: str, start: str, end: str, channel: str) -> dict[str, str]:
    today = date.today()
    period = period if period in {"today", "7", "30", "month", "custom", "all"} else "30"
    channel = channel if channel in VALID_CHANNELS else "all"

    if period == "today":
        start_date = end_date = today
    elif period == "7":
        start_date, end_date = today - timedelta(days=6), today
    elif period == "30":
        start_date, end_date = today - timedelta(days=29), today
    elif period == "month":
        start_date, end_date = today.replace(day=1), today
    elif period == "all":
        return {"period": period, "start": "", "end": "", "channel": channel}
    else:
        try:
            start_date = datetime.strptime(start, "%Y-%m-%d").date()
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            start_date, end_date, period = today - timedelta(days=29), today, "30"
        if start_date > end_date:
            start_date, end_date = end_date, start_date

    return {
        "period": period,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "channel": channel,
    }


def where_clause(filters: dict[str, str], alias: str = "o") -> tuple[str, list[Any]]:
    parts = [f"{alias}.is_cancelled=0", f"{alias}.is_test=0"]
    params: list[Any] = []
    if filters["start"]:
        parts.append(f"substr({alias}.ordered_at,1,10)>=?")
        params.append(filters["start"])
    if filters["end"]:
        parts.append(f"substr({alias}.ordered_at,1,10)<=?")
        params.append(filters["end"])
    if filters["channel"] != "all":
        parts.append(f"{alias}.sales_channel=?")
        params.append(filters["channel"])
    return " AND ".join(parts), params


def dashboard_data(filters: dict[str, str]) -> dict[str, Any]:
    with connect() as connection:
        if not table_exists(connection, "sales_foundation_orders"):
            raise RuntimeError("Phase 1 Sales Foundation has not been installed.")

        where, params = where_clause(filters)
        order_summary = connection.execute(
            f"""
            SELECT COUNT(*) AS orders, COALESCE(SUM(net_sales),0) AS net_sales,
                   COALESCE(SUM(order_total),0) AS collected_total
            FROM sales_foundation_orders o WHERE {where}
            """,
            params,
        ).fetchone()

        line_summary = connection.execute(
            f"""
            SELECT COALESCE(SUM(l.quantity),0) AS units,
                   COALESCE(SUM(l.net_sales),0) AS line_sales,
                   COALESCE(SUM(CASE WHEN l.product_id IS NOT NULL THEN l.net_sales ELSE 0 END),0) AS matched_sales,
                   COALESCE(SUM(CASE WHEN l.product_id IS NULL THEN l.net_sales ELSE 0 END),0) AS unmatched_sales,
                   COALESCE(SUM(l.estimated_cost),0) AS estimated_cost,
                   COALESCE(SUM(l.estimated_gross_profit),0) AS estimated_profit,
                   COALESCE(SUM(CASE WHEN l.product_id IS NULL AND l.estimated_cost IS NOT NULL THEN l.estimated_cost ELSE 0 END),0) AS rule_cost,
                   COALESCE(SUM(CASE WHEN l.product_id IS NULL AND l.estimated_cost IS NOT NULL THEN l.estimated_gross_profit ELSE 0 END),0) AS rule_profit,
                   COALESCE(SUM(CASE WHEN l.estimated_cost IS NOT NULL THEN l.net_sales ELSE 0 END),0) AS covered_sales,
                   SUM(CASE WHEN l.product_id IS NULL THEN 1 ELSE 0 END) AS unmatched_lines,
                   SUM(CASE WHEN l.estimated_cost IS NULL THEN 1 ELSE 0 END) AS uncovered_lines,
                   SUM(CASE WHEN l.product_id IS NULL AND l.estimated_cost IS NOT NULL THEN 1 ELSE 0 END) AS rule_lines,
                   COUNT(*) AS total_lines
            FROM sales_foundation_lines l
            JOIN sales_foundation_orders o
              ON o.sales_channel=l.sales_channel AND o.external_order_id=l.external_order_id
            WHERE {where}
            """,
            params,
        ).fetchone()

        channels = connection.execute(
            f"""
            SELECT o.sales_channel, COUNT(DISTINCT o.external_order_id) AS orders,
                   COALESCE(SUM(o.net_sales),0) AS net_sales,
                   COALESCE(SUM(o.order_total),0) AS collected_total,
                   COALESCE((SELECT SUM(l.quantity) FROM sales_foundation_lines l
                       JOIN sales_foundation_orders ox ON ox.sales_channel=l.sales_channel
                        AND ox.external_order_id=l.external_order_id
                       WHERE ox.sales_channel=o.sales_channel AND {where.replace('o.', 'ox.')}),0) AS units
            FROM sales_foundation_orders o WHERE {where}
            GROUP BY o.sales_channel ORDER BY net_sales DESC
            """,
            params + params,
        ).fetchall()

        top_products = connection.execute(
            f"""
            SELECT l.product_id,
                   COALESCE(NULLIF(TRIM(p.product_name),''), NULLIF(TRIM(l.product_title),''),
                            NULLIF(TRIM(l.sku),''), NULLIF(TRIM(l.barcode),''), 'Unknown product') AS product_name,
                   COALESCE(l.barcode, (
                       SELECT pb.barcode FROM product_barcodes pb
                       WHERE pb.product_id=l.product_id
                       ORDER BY pb.is_primary DESC, pb.rowid LIMIT 1
                   ), '') AS barcode,
                   l.sales_channel, SUM(l.quantity) AS units,
                   SUM(l.net_sales) AS net_sales, SUM(l.estimated_cost) AS estimated_cost,
                   SUM(l.estimated_gross_profit) AS estimated_profit,
                   MAX(l.amount_quality) AS amount_quality
            FROM sales_foundation_lines l
            JOIN sales_foundation_orders o
              ON o.sales_channel=l.sales_channel AND o.external_order_id=l.external_order_id
            LEFT JOIN products p ON p.product_id=l.product_id
            WHERE {where}
            GROUP BY l.product_id, product_name, l.barcode, l.sales_channel
            ORDER BY units DESC, net_sales DESC LIMIT 20
            """,
            params,
        ).fetchall()

        unmatched = connection.execute(
            f"""
            SELECT l.sales_channel, COALESCE(NULLIF(TRIM(l.product_title),''),
                   NULLIF(TRIM(l.sku),''), NULLIF(TRIM(l.barcode),''), 'Unknown product') AS product_name,
                   l.sku, l.barcode, SUM(l.quantity) AS units, SUM(l.net_sales) AS net_sales,
                   COUNT(*) AS lines
            FROM sales_foundation_lines l
            JOIN sales_foundation_orders o
              ON o.sales_channel=l.sales_channel AND o.external_order_id=l.external_order_id
            WHERE {where} AND l.product_id IS NULL
            GROUP BY l.sales_channel, product_name, l.sku, l.barcode
            ORDER BY net_sales DESC, units DESC LIMIT 25
            """,
            params,
        ).fetchall()

        recent_orders = connection.execute(
            f"""
            SELECT sales_channel, external_order_id, order_number, ordered_at,
                   order_status, fulfillment_status, net_sales, order_total, currency
            FROM sales_foundation_orders o WHERE {where}
            ORDER BY ordered_at DESC LIMIT 30
            """,
            params,
        ).fetchall()

        daily = connection.execute(
            f"""
            SELECT substr(ordered_at,1,10) AS sales_date, SUM(net_sales) AS net_sales,
                   COUNT(*) AS orders FROM sales_foundation_orders o WHERE {where}
            GROUP BY substr(ordered_at,1,10) ORDER BY sales_date
            """,
            params,
        ).fetchall()

    total_lines = integer(line_summary["total_lines"])
    unmatched_lines = integer(line_summary["unmatched_lines"])
    matched_lines = total_lines - unmatched_lines
    line_sales = money(line_summary["line_sales"])
    matched_sales = money(line_summary["matched_sales"])
    covered_sales = money(line_summary["covered_sales"])
    return {
        "summary": {
            "orders": integer(order_summary["orders"]),
            "units": integer(line_summary["units"]),
            "net_sales": money(order_summary["net_sales"]),
            "collected_total": money(order_summary["collected_total"]),
            "estimated_cost": money(line_summary["estimated_cost"]),
            "estimated_profit": money(line_summary["estimated_profit"]),
            "rule_cost": money(line_summary["rule_cost"]),
            "rule_profit": money(line_summary["rule_profit"]),
            "rule_lines": integer(line_summary["rule_lines"]),
            "uncovered_lines": integer(line_summary["uncovered_lines"]),
            "matched_lines": matched_lines,
            "unmatched_lines": unmatched_lines,
            "total_lines": total_lines,
            "match_rate": round((matched_lines / total_lines * 100), 1) if total_lines else 0,
            "profit_coverage": round((covered_sales / line_sales * 100), 1) if line_sales else 0,
        },
        "channels": [dict(row) for row in channels],
        "top_products": [dict(row) for row in top_products],
        "unmatched": [dict(row) for row in unmatched],
        "recent_orders": [dict(row) for row in recent_orders],
        "daily": [dict(row) for row in daily],
    }


def export_rows(filters: dict[str, str]) -> list[dict[str, Any]]:
    where, params = where_clause(filters)
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT o.sales_channel, o.external_order_id, o.order_number, o.ordered_at,
                   o.order_status, l.external_line_id, l.product_id, l.sku, l.barcode,
                   l.product_title, l.quantity, l.net_sales, l.unit_cost_snapshot,
                   l.estimated_cost, l.estimated_gross_profit, l.product_match_status,
                   l.amount_quality
            FROM sales_foundation_orders o
            JOIN sales_foundation_lines l ON l.sales_channel=o.sales_channel
             AND l.external_order_id=o.external_order_id
            WHERE {where} ORDER BY o.ordered_at, o.sales_channel, o.external_order_id
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def install_sales_dashboard(app: FastAPI) -> None:
    @app.get("/sales", response_class=HTMLResponse, name="sales_dashboard")
    def sales_dashboard(
        request: Request,
        period: str = "30",
        start: str = "",
        end: str = "",
        channel: str = "all",
    ):
        selected = filters_for(period, start, end, channel)
        error = None
        try:
            data = dashboard_data(selected)
        except Exception as exc:
            error = str(exc)
            data = {"summary": {}, "channels": [], "top_products": [], "unmatched": [], "recent_orders": [], "daily": []}
        return TEMPLATES.TemplateResponse(
            request=request,
            name="sales_dashboard.html",
            context={"filters": selected, "error": error, **data},
        )

    @app.post("/sales/refresh")
    def refresh_sales_dashboard():
        from app.services.sales_foundation import refresh
        refresh(DB_PATH)
        return RedirectResponse(url="/sales?period=30", status_code=303)

    @app.get("/sales/export.csv")
    def export_sales(
        period: str = "30", start: str = "", end: str = "", channel: str = "all"
    ):
        selected = filters_for(period, start, end, channel)
        rows = export_rows(selected)
        output = io.StringIO()
        fields = list(rows[0].keys()) if rows else ["sales_channel", "external_order_id", "ordered_at"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        filename = f"brookshouse-sales-{date.today().isoformat()}.csv"
        return StreamingResponse(
            iter([output.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
