from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parents[1]
DB_PATH = APP_DIR / "data" / "brookshouse_store.db"
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def ensure_tables() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS shopify_quick_sale_cost_rules (
                normalized_title TEXT PRIMARY KEY,
                display_title TEXT NOT NULL,
                cost_method TEXT NOT NULL,
                cost_value REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shopify_exact_title_matches (
                normalized_title TEXT PRIMARY KEY,
                display_title TEXT NOT NULL,
                product_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def refresh_foundation() -> None:
    from app.services.sales_foundation import refresh
    refresh(DB_PATH)


def page_data() -> dict:
    ensure_tables()
    with connect() as connection:
        exact_candidates = connection.execute(
            """
            WITH unmatched AS (
                SELECT lower(trim(title)) normalized_title, min(title) display_title,
                       count(*) lines, sum(current_quantity) units,
                       round(sum(net_amount),2) sales
                FROM shopify_sales_lines
                WHERE product_id IS NULL AND trim(coalesce(title,''))<>''
                GROUP BY lower(trim(title))
            ), candidates AS (
                SELECT u.*,
                       count(DISTINCT p.product_id) candidate_count,
                       min(p.product_id) product_id,
                       min(p.product_name) product_name
                FROM unmatched u
                JOIN products p ON lower(trim(p.product_name))=u.normalized_title
                GROUP BY u.normalized_title
            )
            SELECT c.*, m.product_id approved_product_id
            FROM candidates c
            LEFT JOIN shopify_exact_title_matches m
              ON m.normalized_title=c.normalized_title AND m.active=1
            WHERE c.candidate_count=1
            ORDER BY c.sales DESC, c.lines DESC
            """
        ).fetchall()

        quick_sales = connection.execute(
            """
            WITH grouped AS (
                SELECT lower(trim(title)) normalized_title, min(title) display_title,
                       count(*) lines, sum(current_quantity) units,
                       round(sum(net_amount),2) sales,
                       sum(CASE WHEN trim(coalesce(barcode,''))='' THEN 1 ELSE 0 END) missing_barcode
                FROM shopify_sales_lines
                WHERE product_id IS NULL AND trim(coalesce(title,''))<>''
                GROUP BY lower(trim(title))
            )
            SELECT g.*, r.cost_method, r.cost_value, coalesce(r.active,0) rule_active,
                   CASE WHEN r.active=1 AND r.cost_method='percent_of_sales'
                        THEN round(g.sales*r.cost_value/100,2)
                        WHEN r.active=1 AND r.cost_method='fixed_per_unit'
                        THEN round(g.units*r.cost_value,2) END estimated_cost
            FROM grouped g
            LEFT JOIN shopify_quick_sale_cost_rules r USING(normalized_title)
            LEFT JOIN shopify_exact_title_matches m
              ON m.normalized_title=g.normalized_title AND m.active=1
            WHERE m.normalized_title IS NULL
            ORDER BY g.sales DESC, g.lines DESC
            """
        ).fetchall()

        summary = connection.execute(
            """
            SELECT count(*) total_lines,
                   sum(CASE WHEN product_id IS NOT NULL THEN 1 ELSE 0 END) matched_lines,
                   sum(CASE WHEN product_id IS NULL THEN 1 ELSE 0 END) unmatched_lines,
                   round(sum(CASE WHEN product_id IS NULL THEN net_amount ELSE 0 END),2) unmatched_sales
            FROM shopify_sales_lines
            """
        ).fetchone()
        approved = connection.execute(
            """SELECT m.*, p.product_name FROM shopify_exact_title_matches m
               JOIN products p ON p.product_id=m.product_id WHERE m.active=1
               ORDER BY m.display_title"""
        ).fetchall()
    return {
        "exact_candidates": [dict(row) for row in exact_candidates],
        "quick_sales": [dict(row) for row in quick_sales],
        "summary": dict(summary),
        "approved": [dict(row) for row in approved],
    }


def install_shopify_cost_rules(app: FastAPI) -> None:
    ensure_tables()

    @app.get("/sales/shopify-cost-rules", response_class=HTMLResponse)
    def shopify_cost_rules_page(request: Request):
        return TEMPLATES.TemplateResponse(
            request=request,
            name="shopify_cost_rules.html",
            context={
                **page_data(),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
        )

    @app.post("/sales/shopify-cost-rules/exact")
    def approve_exact_title(
        title: str = Form(...), product_id: int = Form(...)
    ):
        normalized = title.strip().casefold()
        try:
            with connect() as connection:
                product = connection.execute(
                    "SELECT product_name FROM products WHERE product_id=?", (product_id,)
                ).fetchone()
                if not product:
                    raise ValueError("BrooksHouse product was not found.")
                count = connection.execute(
                    "SELECT count(*) FROM products WHERE lower(trim(product_name))=?",
                    (normalized,),
                ).fetchone()[0]
                if int(count) != 1 or str(product[0]).strip().casefold() != normalized:
                    raise ValueError("The title is no longer a unique exact match.")
                connection.execute(
                    """INSERT INTO shopify_exact_title_matches
                       (normalized_title,display_title,product_id,active)
                       VALUES (?,?,?,1)
                       ON CONFLICT(normalized_title) DO UPDATE SET
                         display_title=excluded.display_title, product_id=excluded.product_id,
                         active=1, updated_at=CURRENT_TIMESTAMP""",
                    (normalized, title.strip(), product_id),
                )
            refresh_foundation()
            return RedirectResponse(
                "/sales/shopify-cost-rules?message=Exact title match approved.", status_code=303
            )
        except Exception as exc:
            return RedirectResponse(
                f"/sales/shopify-cost-rules?error={quote_plus(str(exc))}", status_code=303
            )

    @app.post("/sales/shopify-cost-rules/rule")
    def save_cost_rule(
        title: str = Form(...), cost_method: str = Form(...), cost_value: float = Form(0)
    ):
        normalized = title.strip().casefold()
        try:
            if cost_method not in {"unknown", "percent_of_sales", "fixed_per_unit"}:
                raise ValueError("Unknown cost method.")
            if cost_value < 0:
                raise ValueError("Cost cannot be negative.")
            if cost_method == "percent_of_sales" and cost_value > 100:
                raise ValueError("Cost percentage must be between 0 and 100.")
            active = 0 if cost_method == "unknown" else 1
            with connect() as connection:
                connection.execute(
                    """INSERT INTO shopify_quick_sale_cost_rules
                       (normalized_title,display_title,cost_method,cost_value,active)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(normalized_title) DO UPDATE SET
                         display_title=excluded.display_title, cost_method=excluded.cost_method,
                         cost_value=excluded.cost_value, active=excluded.active,
                         updated_at=CURRENT_TIMESTAMP""",
                    (normalized, title.strip(), cost_method, cost_value, active),
                )
            refresh_foundation()
            return RedirectResponse(
                "/sales/shopify-cost-rules?message=Quick sale cost rule saved.", status_code=303
            )
        except Exception as exc:
            return RedirectResponse(
                f"/sales/shopify-cost-rules?error={quote_plus(str(exc))}", status_code=303
            )

    @app.post("/sales/shopify-cost-rules/exact/remove")
    def remove_exact_title(title: str = Form(...)):
        with connect() as connection:
            connection.execute(
                "UPDATE shopify_exact_title_matches SET active=0,updated_at=CURRENT_TIMESTAMP WHERE normalized_title=?",
                (title.strip().casefold(),),
            )
        refresh_foundation()
        return RedirectResponse(
            "/sales/shopify-cost-rules?message=Exact title match removed.", status_code=303
        )
