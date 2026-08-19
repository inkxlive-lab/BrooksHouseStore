"""Durable review and apply workflow for product enrichment."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.database.models import (
    Product, ProductBarcode, ProductEnrichmentAuditEvent, ProductEnrichmentBatch,
    ProductEnrichmentItem, ProductEnrichmentProposal, ProductImage,
)
from app.services.product_enrichment_lookup import (
    DEFAULT_INTERNET_RATE_LIMITER, RateLimiter, internet_candidates,
    local_candidates, save_candidates, save_source_error,
)


DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 25
PRODUCT_FIELDS = (
    "product_name", "brand", "description", "category", "size_value",
    "size_unit", "suggested_retail_price",
)
ALL_FIELDS = (*PRODUCT_FIELDS, "product_image")
PLACEHOLDER_NAMES = {"unknown", "unknown product", "new product", "no description", "item"}


class StaleProductError(ValueError):
    pass


def _json_default(value: Any) -> str:
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    raise TypeError(type(value).__name__)


def _dump(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def _actor(user: Any) -> tuple[int | None, str, str | None]:
    user_id = int(getattr(user, "user_id", 0) or 0) or None
    name = (str(getattr(user, "display_name", "") or "").strip()
            or str(getattr(user, "username", "") or "").strip() or "System")
    role = str(getattr(user, "role", "") or "").strip() or None
    return user_id, name, role


def audit(
    database: Session, batch_id: int, event_type: str, user: Any = None,
    item_id: int | None = None, proposal_id: int | None = None,
    field_name: str | None = None, old_value: Any = None, new_value: Any = None,
    source_name: str | None = None, details: dict[str, Any] | None = None,
) -> None:
    user_id, name, role = _actor(user)
    database.add(ProductEnrichmentAuditEvent(
        batch_id=batch_id, item_id=item_id, proposal_id=proposal_id,
        event_type=event_type, actor_user_id=user_id, actor_name=name,
        actor_role=role, field_name=field_name,
        old_value=None if old_value is None else str(old_value),
        new_value=None if new_value is None else str(new_value),
        source_name=source_name, details_json=_dump(details or {}),
    ))


def _image_snapshot(product: Product) -> list[dict[str, Any]]:
    return [{"image_id": image.image_id, "image_url": image.image_url,
             "image_path": image.image_path, "is_primary": bool(image.is_primary)}
            for image in sorted(product.images, key=lambda row: row.image_id or 0)]


def product_snapshot(product: Product) -> dict[str, Any]:
    return {
        **{field: None if getattr(product, field) is None else str(getattr(product, field))
           for field in PRODUCT_FIELDS},
        "product_image": _image_snapshot(product),
    }


def missing_fields(product: Product) -> list[str]:
    missing: list[str] = []
    name = (product.product_name or "").strip()
    if not name or name.casefold() in PLACEHOLDER_NAMES or name.isdigit():
        missing.append("product_name")
    for field in ("brand", "description", "category", "size_value", "size_unit",
                  "suggested_retail_price"):
        value = getattr(product, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    if not any((image.image_url or image.image_path or "").strip() for image in product.images):
        missing.append("product_image")
    return missing


def create_batch(database: Session, user: Any, batch_size: int = DEFAULT_BATCH_SIZE) -> ProductEnrichmentBatch:
    batch_size = int(batch_size)
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"Batch size must be between 1 and {MAX_BATCH_SIZE}.")
    user_id, name, _role = _actor(user)
    batch = ProductEnrichmentBatch(
        status="draft", requested_batch_size=batch_size,
        selection_config_json=_dump({"active_only": True, "fields": list(ALL_FIELDS)}),
        created_by_user_id=user_id, created_by_name=name,
    )
    database.add(batch)
    database.flush()
    products = database.scalars(
        select(Product).where(Product.active.is_(True)).options(
            selectinload(Product.images), selectinload(Product.barcodes)
        ).order_by(Product.product_id)
    ).all()
    selected = [(product, missing_fields(product)) for product in products]
    selected = [(product, fields) for product, fields in selected if fields][:batch_size]
    for position, (product, fields) in enumerate(selected, 1):
        barcode = next((row.barcode for row in product.barcodes if row.is_primary), None)
        barcode = barcode or next((row.barcode for row in product.barcodes), None)
        database.add(ProductEnrichmentItem(
            batch_id=batch.batch_id, product_id=product.product_id, position=position,
            primary_barcode=barcode, missing_fields_json=_dump(fields),
            product_snapshot_json=_dump(product_snapshot(product)), status="pending",
        ))
    batch.selected_count = len(selected)
    if not selected:
        batch.status = "completed"
        batch.completed_at = datetime.now()
    audit(database, batch.batch_id, "batch_created", user,
          details={"batch_size": batch_size, "selected_count": len(selected)})
    database.commit()
    database.refresh(batch)
    return batch


def set_batch_status(database: Session, batch: ProductEnrichmentBatch, status: str, user: Any) -> None:
    allowed = {"running", "paused", "cancelled"}
    if status not in allowed:
        raise ValueError("Unsupported batch status transition.")
    old = batch.status
    batch.status = status
    now = datetime.now()
    if status == "running":
        batch.started_at = batch.started_at or now
        batch.paused_at = None
    elif status == "paused":
        batch.paused_at = now
    elif status == "cancelled":
        batch.completed_at = now
    audit(database, batch.batch_id, f"batch_{status}", user, old_value=old, new_value=status)
    database.commit()


def process_next_item(
    database: Session, batch: ProductEnrichmentBatch, user: Any = None,
    internet_lookup: Callable[[str], dict[str, Any]] | None = None,
    limiter: RateLimiter | None = None, include_internet: bool = True,
) -> ProductEnrichmentItem | None:
    if batch.status != "running":
        raise ValueError("Batch must be running before lookup can continue.")
    item = database.scalar(
        select(ProductEnrichmentItem).where(
            ProductEnrichmentItem.batch_id == batch.batch_id,
            ProductEnrichmentItem.status.in_(("pending", "error")),
        ).options(selectinload(ProductEnrichmentItem.proposals))
        .order_by(ProductEnrichmentItem.position)
    )
    if item is None:
        batch.status = "reviewing"
        audit(database, batch.batch_id, "lookup_complete", user)
        database.commit()
        return None
    item.status = "looking_up"
    item.started_at = item.started_at or datetime.now()
    item.attempt_count += 1
    item.last_error = None
    database.commit()  # durable checkpoint before external work
    errors: list[str] = []
    try:
        save_candidates(database, item, local_candidates(database, item))
        database.commit()  # local results survive an internet failure
        if include_internet:
            candidates, error = internet_candidates(
                item.primary_barcode or "",
                lookup=internet_lookup or __import__(
                    "app.integrations.product_lookup", fromlist=["lookup_upc_online"]
                ).lookup_upc_online,
                limiter=limiter or DEFAULT_INTERNET_RATE_LIMITER,
                database=database,
            )
            save_candidates(database, item, candidates)
            if error:
                errors.append(error)
                save_source_error(database, item, "Internet lookup", error)
        retryable_error = bool(errors and item.primary_barcode and item.attempt_count < 3)
        item.status = "error" if retryable_error else "ready"
        item.lookup_completed_at = None if retryable_error else datetime.now()
        item.last_error = " | ".join(errors)[:2000] or None
        if errors:
            batch.error_count += 1
        if not retryable_error:
            batch.processed_count += 1
            batch.next_item_position = item.position + 1
        audit(database, batch.batch_id,
              "item_lookup_retry_required" if retryable_error else "item_lookup_completed",
              user, item_id=item.item_id,
              details={"proposal_count": len(item.proposals), "errors": errors,
                       "attempt": item.attempt_count})
        database.commit()
        return item
    except Exception as exc:
        database.rollback()
        item = database.get(ProductEnrichmentItem, item.item_id)
        batch = database.get(ProductEnrichmentBatch, batch.batch_id)
        item.status = "error"
        item.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        batch.error_count += 1
        batch.last_error = item.last_error
        audit(database, batch.batch_id, "item_lookup_error", user, item_id=item.item_id,
              details={"error": item.last_error})
        database.commit()
        return item


def review_proposal(
    database: Session, proposal: ProductEnrichmentProposal, action: str, user: Any
) -> None:
    if action not in {"approve", "reject"}:
        raise ValueError("Unsupported review action.")
    item = proposal.item
    now = datetime.now()
    user_id, name, _role = _actor(user)
    if action == "approve":
        for other in database.scalars(select(ProductEnrichmentProposal).where(
            ProductEnrichmentProposal.item_id == item.item_id,
            ProductEnrichmentProposal.field_name == proposal.field_name,
            ProductEnrichmentProposal.proposal_id != proposal.proposal_id,
            ProductEnrichmentProposal.status == "approved",
        )):
            if (other.field_name == proposal.field_name and other.proposal_id != proposal.proposal_id
                    and other.status == "approved"):
                other.status = "superseded"
        proposal.status = "approved"
    else:
        proposal.status = "rejected"
    proposal.reviewed_by_user_id = user_id
    proposal.reviewed_by_name = name
    proposal.reviewed_at = now
    item.reviewed_at = now
    item.status = "reviewed"
    audit(database, item.batch_id, f"proposal_{action}d", user, item.item_id,
          proposal.proposal_id, proposal.field_name, source_name=proposal.source_name,
          new_value=proposal.proposed_value)
    batch = database.get(ProductEnrichmentBatch, item.batch_id)
    batch.approved_count = database.scalar(
        select(func.count()).select_from(ProductEnrichmentProposal)
        .join(ProductEnrichmentItem)
        .where(ProductEnrichmentItem.batch_id == item.batch_id,
               ProductEnrichmentProposal.status == "approved")
    )
    batch.rejected_count = database.scalar(
        select(func.count()).select_from(ProductEnrichmentProposal)
        .join(ProductEnrichmentItem)
        .where(ProductEnrichmentItem.batch_id == item.batch_id,
               ProductEnrichmentProposal.status == "rejected")
    )
    database.commit()


def edit_proposal(
    database: Session, proposal: ProductEnrichmentProposal, value: str, user: Any
) -> ProductEnrichmentProposal:
    value = str(value or "").strip()
    if not value:
        raise ValueError("Edited value cannot be blank.")
    item = proposal.item
    edited = ProductEnrichmentProposal(
        item_id=item.item_id, field_name=proposal.field_name, proposed_value=value,
        normalized_value=value.casefold(), source_type="user_edit", source_name="Admin edit",
        source_reference=str(proposal.proposal_id), confidence=Decimal("1.0000"),
        status="edited", original_proposal_id=proposal.proposal_id,
    )
    database.add(edited)
    database.flush()
    audit(database, item.batch_id, "proposal_edited", user, item.item_id,
          edited.proposal_id, proposal.field_name, old_value=proposal.proposed_value,
          new_value=value, source_name="Admin edit")
    database.commit()
    database.refresh(edited)
    return edited


def _coerce(field: str, value: str) -> Any:
    if field in {"size_value", "suggested_retail_price"}:
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError):
            raise ValueError(f"{field} must be numeric.")
        if number < 0:
            raise ValueError(f"{field} cannot be negative.")
        return number
    return value.strip()


def apply_item(database: Session, item: ProductEnrichmentItem, user: Any) -> int:
    item_id = item.item_id
    item = database.scalar(
        select(ProductEnrichmentItem).where(ProductEnrichmentItem.item_id == item_id)
    )
    product = database.scalar(
        select(Product).where(Product.product_id == item.product_id)
        .options(selectinload(Product.images))
    )
    approved = database.scalars(select(ProductEnrichmentProposal).where(
        ProductEnrichmentProposal.item_id == item.item_id,
        ProductEnrichmentProposal.status == "approved",
    ).order_by(ProductEnrichmentProposal.proposal_id)).all()
    if not approved:
        raise ValueError("Approve at least one proposal before Apply.")
    snapshot = json.loads(item.product_snapshot_json)
    conflicts = []
    for proposal in approved:
        current = product_snapshot(product)[proposal.field_name]
        if current != snapshot.get(proposal.field_name):
            conflicts.append(proposal.field_name)
    if conflicts:
        audit(database, item.batch_id, "apply_stale_conflict", user, item.item_id,
              details={"fields": sorted(set(conflicts))})
        database.commit()
        raise StaleProductError(
            "Product changed after lookup; review again before applying: "
            + ", ".join(sorted(set(conflicts)))
        )
    now = datetime.now()
    for proposal in approved:
        old_value = snapshot.get(proposal.field_name)
        if proposal.field_name == "product_image":
            product.images.append(ProductImage(
                image_url=proposal.proposed_value, image_type="enrichment", is_primary=False
            ))
        else:
            setattr(product, proposal.field_name, _coerce(proposal.field_name, proposal.proposed_value or ""))
        proposal.status = "applied"
        proposal.applied_at = now
        audit(database, item.batch_id, "field_applied", user, item.item_id,
              proposal.proposal_id, proposal.field_name, old_value, proposal.proposed_value,
              proposal.source_name)
    item.status = "applied"
    item.applied_at = now
    batch = database.get(ProductEnrichmentBatch, item.batch_id)
    batch.applied_count += 1
    remaining = database.scalar(select(func.count()).select_from(ProductEnrichmentItem).where(
        ProductEnrichmentItem.batch_id == item.batch_id,
        ProductEnrichmentItem.status.not_in(("applied", "skipped")),
    ))
    if remaining == 0:
        batch.status = "completed"
        batch.completed_at = now
    database.commit()
    return len(approved)
