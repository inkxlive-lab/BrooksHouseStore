"""Read-only BrooksHouse inventory activity / transaction history screen."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from math import ceil
from urllib.parse import urlencode

from fastapi import Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.database.connection import SessionLocal
from app.database.models import InventoryLocation, InventoryTransaction, Product, ProductBarcode


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _date(value: str | None):
    try:
        return datetime.strptime(_clean(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def install_inventory_activity(app, templates) -> None:
    @app.get("/inventory/activity", response_class=HTMLResponse)
    def inventory_activity(
        request: Request,
        search: str = Query(""),
        location_id: str = Query(""),
        transaction_type: str = Query(""),
        performed_by: str = Query(""),
        date_from: str = Query(""),
        date_to: str = Query(""),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=25, le=200),
    ):
        clean_search = _clean(search)
        clean_type = _clean(transaction_type)
        clean_person = _clean(performed_by)
        start_date = _date(date_from)
        end_date = _date(date_to)

        with SessionLocal() as database:
            filters = []
            if _clean(location_id).isdigit():
                filters.append(InventoryTransaction.location_id == int(_clean(location_id)))
            if clean_type:
                filters.append(InventoryTransaction.transaction_type == clean_type)
            if clean_person:
                filters.append(InventoryTransaction.performed_by_name.ilike(f"%{clean_person}%"))
            if start_date:
                filters.append(InventoryTransaction.created_at >= datetime.combine(start_date, time.min))
            if end_date:
                filters.append(InventoryTransaction.created_at < datetime.combine(end_date + timedelta(days=1), time.min))
            if clean_search:
                like = f"%{clean_search}%"
                barcode_products = select(ProductBarcode.product_id).where(ProductBarcode.barcode.ilike(like))
                filters.append(or_(
                    Product.product_name.ilike(like),
                    Product.description.ilike(like),
                    InventoryTransaction.container_id.ilike(like),
                    InventoryTransaction.reference_number.ilike(like),
                    InventoryTransaction.notes.ilike(like),
                    InventoryTransaction.product_id.in_(barcode_products),
                ))

            base = select(InventoryTransaction).join(Product, InventoryTransaction.product_id == Product.product_id)
            if filters:
                base = base.where(*filters)

            total = database.scalar(select(func.count()).select_from(base.subquery())) or 0
            pages = max(1, ceil(total / page_size))
            page = min(page, pages)
            transactions = database.scalars(
                base.options(
                    selectinload(InventoryTransaction.product).selectinload(Product.barcodes),
                    selectinload(InventoryTransaction.location),
                )
                .order_by(InventoryTransaction.created_at.desc(), InventoryTransaction.transaction_id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()

            locations = database.scalars(
                select(InventoryLocation).order_by(InventoryLocation.location_name)
            ).all()
            transaction_types = database.scalars(
                select(InventoryTransaction.transaction_type)
                .distinct().order_by(InventoryTransaction.transaction_type)
            ).all()

            added = sum(max(0, int(row.quantity_change or 0)) for row in transactions)
            removed = sum(abs(min(0, int(row.quantity_change or 0))) for row in transactions)
            pagination_query = urlencode({
                "search": clean_search, "location_id": location_id or "",
                "transaction_type": clean_type, "performed_by": clean_person,
                "date_from": date_from, "date_to": date_to, "page_size": page_size,
            })

            return templates.TemplateResponse(
                request=request,
                name="inventory_activity.html",
                context={
                    "transactions": transactions,
                    "locations": locations,
                    "transaction_types": transaction_types,
                    "filters": {
                        "search": clean_search, "location_id": location_id,
                        "transaction_type": clean_type, "performed_by": clean_person,
                        "date_from": date_from, "date_to": date_to, "page_size": page_size,
                    },
                    "total": total, "page": page, "pages": pages,
                    "page_added": added, "page_removed": removed,
                    "pagination_query": pagination_query,
                },
            )
