"""Owner-admin routes for review-first product enrichment."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.connection import SessionLocal
from app.database.models import (
    Product, ProductEnrichmentAuditEvent, ProductEnrichmentBatch, ProductEnrichmentItem,
    ProductEnrichmentProposal,
)
from app.services.product_enrichment_workflow import (
    MAX_BATCH_SIZE, StaleProductError, apply_item, create_batch, edit_proposal,
    process_next_item, review_proposal, set_batch_status,
)


router = APIRouter(prefix="/admin/product-enrichment", tags=["product-enrichment"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _review_fields(product: Product) -> list[tuple[str, str, object]]:
    return [
        ("product_name", "Product name", product.product_name),
        ("brand", "Brand", product.brand),
        ("description", "Description", product.description),
        ("category", "Category", product.category),
        ("size_value", "Size value", product.size_value),
        ("size_unit", "Size unit", product.size_unit),
        ("suggested_retail_price", "Suggested retail price", product.suggested_retail_price),
        ("product_image", "Image", None),
    ]


def _owner(request: Request):
    user = getattr(request.state, "auth_user", None)
    if user is None or getattr(user, "role", "") != "owner_admin":
        raise HTTPException(status_code=403, detail="Owner-admin access is required.")
    return user


def _redirect(path: str, message: str = "", error: str = ""):
    query = []
    if message:
        query.append("message=" + quote_plus(message))
    if error:
        query.append("error=" + quote_plus(error))
    return RedirectResponse(path + (("?" + "&".join(query)) if query else ""), status_code=303)


@router.get("", response_class=HTMLResponse)
def batches_page(request: Request, message: str = "", error: str = ""):
    _owner(request)
    with SessionLocal() as database:
        batches = database.scalars(
            select(ProductEnrichmentBatch).order_by(ProductEnrichmentBatch.batch_id.desc())
        ).all()
        return templates.TemplateResponse(request=request, name="product_enrichment_batches.html",
            context={"batches": batches, "default_batch_size": 10,
                     "maximum_batch_size": MAX_BATCH_SIZE, "message": message, "error": error})


@router.post("/batches")
def create_batch_route(request: Request, batch_size: int = Form(10)):
    user = _owner(request)
    with SessionLocal() as database:
        try:
            batch = create_batch(database, user, batch_size)
        except ValueError as exc:
            return _redirect("/admin/product-enrichment", error=str(exc))
    return _redirect(f"/admin/product-enrichment/batches/{batch.batch_id}",
                     message=f"Created a {batch.selected_count}-product review batch.")


def _load_batch(database, batch_id: int):
    batch = database.scalar(
        select(ProductEnrichmentBatch).where(ProductEnrichmentBatch.batch_id == batch_id)
        .options(selectinload(ProductEnrichmentBatch.items).selectinload(ProductEnrichmentItem.product))
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Enrichment batch not found.")
    return batch


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: int, message: str = "", error: str = ""):
    _owner(request)
    with SessionLocal() as database:
        batch = _load_batch(database, batch_id)
        return templates.TemplateResponse(request=request, name="product_enrichment_batch.html",
            context={"batch": batch, "items": sorted(batch.items, key=lambda row: row.position),
                     "message": message, "error": error})


@router.post("/batches/{batch_id}/{action}")
def batch_action(request: Request, batch_id: int, action: str):
    user = _owner(request)
    with SessionLocal() as database:
        batch = _load_batch(database, batch_id)
        try:
            if action in {"start", "resume"}:
                set_batch_status(database, batch, "running", user)
            elif action == "pause":
                set_batch_status(database, batch, "paused", user)
            elif action == "cancel":
                set_batch_status(database, batch, "cancelled", user)
            elif action == "process-next":
                item = process_next_item(database, batch, user)
                detail = "Lookup queue is complete." if item is None else f"Processed product #{item.product_id}."
                return _redirect(f"/admin/product-enrichment/batches/{batch_id}", message=detail)
            else:
                raise ValueError("Unknown batch action.")
        except ValueError as exc:
            return _redirect(f"/admin/product-enrichment/batches/{batch_id}", error=str(exc))
    return _redirect(f"/admin/product-enrichment/batches/{batch_id}", message=f"Batch {action} saved.")


@router.get("/items/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int, message: str = "", error: str = ""):
    _owner(request)
    with SessionLocal() as database:
        item = database.scalar(
            select(ProductEnrichmentItem).where(ProductEnrichmentItem.item_id == item_id)
            .options(selectinload(ProductEnrichmentItem.product).selectinload(Product.images),
                     selectinload(ProductEnrichmentItem.proposals))
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Enrichment item not found.")
        proposals = sorted(item.proposals, key=lambda row: (row.field_name, -(float(row.confidence or 0))))
        return templates.TemplateResponse(request=request, name="product_enrichment_review.html",
            context={"item": item, "product": item.product, "proposals": proposals,
                     "fields": _review_fields(item.product),
                     "message": message, "error": error})


def _proposal(database, proposal_id: int):
    proposal = database.scalar(
        select(ProductEnrichmentProposal).where(ProductEnrichmentProposal.proposal_id == proposal_id)
        .options(selectinload(ProductEnrichmentProposal.item).selectinload(ProductEnrichmentItem.proposals))
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    return proposal


@router.post("/proposals/{proposal_id}/{action}")
def proposal_action(request: Request, proposal_id: int, action: str, edited_value: str = Form("")):
    user = _owner(request)
    with SessionLocal() as database:
        proposal = _proposal(database, proposal_id)
        item_id = proposal.item_id
        try:
            if action == "edit":
                edit_proposal(database, proposal, edited_value, user)
            else:
                review_proposal(database, proposal, action, user)
        except ValueError as exc:
            return _redirect(f"/admin/product-enrichment/items/{item_id}", error=str(exc))
    return _redirect(f"/admin/product-enrichment/items/{item_id}", message=f"Proposal {action} saved.")


@router.post("/items/{item_id}/apply")
def apply_item_route(request: Request, item_id: int):
    user = _owner(request)
    with SessionLocal() as database:
        item = database.get(ProductEnrichmentItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Enrichment item not found.")
        try:
            count = apply_item(database, item, user)
        except (ValueError, StaleProductError) as exc:
            return _redirect(f"/admin/product-enrichment/items/{item_id}", error=str(exc))
    return _redirect(f"/admin/product-enrichment/items/{item_id}",
                     message=f"Applied {count} approved field(s) to BrooksHouse.")


@router.get("/batches/{batch_id}/audit", response_class=HTMLResponse)
def audit_page(request: Request, batch_id: int):
    _owner(request)
    with SessionLocal() as database:
        batch = _load_batch(database, batch_id)
        events = database.scalars(select(ProductEnrichmentAuditEvent).where(
            ProductEnrichmentAuditEvent.batch_id == batch_id
        ).order_by(ProductEnrichmentAuditEvent.event_id.desc())).all()
        return templates.TemplateResponse(request=request, name="product_enrichment_audit.html",
            context={"batch": batch, "events": events})


def install_product_enrichment(app) -> None:
    app.include_router(router)

    @app.get("/product-enrichment", include_in_schema=False)
    def legacy_product_enrichment_redirect():
        """Keep the former short URL useful without duplicating the owner-admin screen."""
        return RedirectResponse("/admin/product-enrichment", status_code=307)
