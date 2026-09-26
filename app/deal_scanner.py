"""Deal Scanner: a mobile sourcing shell over the shared Smart Scan engine."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.database.connection import get_database
from app.database.models import DealScanReference
from app.integrations.product_lookup import lookup_upc_online
from app.services.smart_scan_engine import (
    build_scan_result,
    calculate_deal_economics,
    identify_barcode,
)


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
PHOTO_ROOT = Path(__file__).resolve().parent / "static" / "product-images" / "deal-scanner"
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class DealScanRequest(BaseModel):
    raw_value: str = ""
    scan_type: str = "barcode"
    mode: str = "personal"
    ocr_text: str = ""
    upc: str | None = None
    ean: str | None = None
    asin: str | None = None
    sku: str | None = None
    model_number: str | None = None
    manufacturer: str | None = None
    brand: str | None = None
    product_name: str | None = None
    variant: str | None = None
    size: str | None = None
    quantity_or_pack_count: str | None = None
    image_refs: list[Any] = Field(default_factory=list)
    acquisition_cost: float | None = Field(default=None, ge=0)
    candidate_resale_price: float | None = Field(default=None, ge=0)
    fees: float = Field(default=0, ge=0)
    shipping_estimate: float = Field(default=0, ge=0)
    quantity: int = Field(default=1, ge=1)
    bin_price: float | None = Field(default=None, ge=0)
    condition: str = "Unknown"
    inspection: dict[str, bool] = Field(default_factory=dict)
    decision: str = "INSPECT_FIRST"


def _has_reference_table(database: Session) -> bool:
    return inspect(database.get_bind()).has_table("deal_scan_references")


def _json(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value if value is not None else fallback, default=str)
    except (TypeError, ValueError):
        return json.dumps(fallback)


def _scan_result(request: DealScanRequest, database: Session) -> dict[str, Any]:
    identifiers = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else request.dict(exclude_none=True)
    if request.raw_value and request.scan_type == "barcode":
        initial = identify_barcode(database, request.raw_value, external_lookup=lookup_upc_online)
        if initial.get("catalog_product_id") is not None:
            result = initial
            result["ocr_text"] = request.ocr_text or result.get("ocr_text", "")
            if request.ocr_text and "ocr" not in result["identification_sources"]:
                result["identification_sources"].append("ocr")
        else:
            result = build_scan_result(
                request.raw_value,
                scan_type=request.scan_type,
                identifiers=identifiers,
                ocr_text=request.ocr_text,
                external=initial if initial.get("status") == "IDENTIFIED_EXTERNAL" else None,
            )
    else:
        result = build_scan_result(
            request.raw_value,
            scan_type=request.scan_type,
            identifiers=identifiers,
            ocr_text=request.ocr_text,
            image_refs=request.image_refs,
        )
    economics = calculate_deal_economics(
        mode=request.mode,
        bin_price=request.bin_price,
        acquisition_cost=request.acquisition_cost,
        candidate_resale_price=request.candidate_resale_price,
        fees=request.fees,
        shipping_estimate=request.shipping_estimate,
        quantity=request.quantity,
    )
    return {
        "scan": result,
        "economics": economics,
        "comparison_prices": [],
        "comparison_status": "provider_deferred",
        "condition": request.condition,
        "inspection": request.inspection,
        "decision": "INSPECT_FIRST" if request.decision not in {"GRAB", "PASS"} else request.decision,
    }


@router.get("/deal-scanner")
def deal_scanner_page(request: Request):
    return templates.TemplateResponse(request=request, name="deal_scanner.html", context={})


@router.post("/api/deal-scanner/scan")
def deal_scanner_scan(payload: DealScanRequest, database: Session = Depends(get_database)):
    return {"ok": True, **_scan_result(payload, database)}


@router.post("/api/deal-scanner/references")
def create_scan_reference(payload: DealScanRequest, database: Session = Depends(get_database)):
    if not _has_reference_table(database):
        raise HTTPException(status_code=503, detail="Deal Scanner migration is not installed.")
    result = _scan_result(payload, database)
    scan = result["scan"]
    economics = result["economics"]
    row = DealScanReference(
        mode=payload.mode, scan_type=scan["scan_type"], raw_value=scan["raw_value"],
        barcode=scan.get("barcode"), upc=scan.get("upc"), ean=scan.get("ean"), asin=scan.get("asin"),
        sku=scan.get("sku"), model_number=scan.get("model_number"), manufacturer=scan.get("manufacturer"),
        brand=scan.get("brand"), product_name=scan.get("product_name"), variant=scan.get("variant"),
        size=str(scan.get("size")) if scan.get("size") is not None else None,
        quantity_or_pack_count=str(scan.get("quantity_or_pack_count")) if scan.get("quantity_or_pack_count") is not None else None,
        ocr_text=scan.get("ocr_text"), catalog_match=bool(scan.get("catalog_match")),
        catalog_product_id=scan.get("catalog_product_id"), identification_confidence=scan.get("identification_confidence"),
        identification_sources_json=_json(scan.get("identification_sources"), []), image_refs_json=_json(scan.get("image_refs"), []),
        status=scan.get("status", "UNKNOWN"), acquisition_cost=economics.get("acquisition_cost"),
        candidate_resale_price=economics.get("candidate_resale_price"), fees=payload.fees,
        shipping_estimate=payload.shipping_estimate, estimated_profit=economics.get("estimated_profit"),
        estimated_margin=economics.get("estimated_margin"), quantity=economics.get("quantity", 1),
        potential_total_profit=economics.get("potential_total_profit"), bin_price=payload.bin_price,
        condition=payload.condition, inspection_json=_json(payload.inspection, {}), decision=result["decision"],
    )
    database.add(row)
    try:
        database.commit()
    except OperationalError:
        database.rollback()
        raise HTTPException(status_code=503, detail="Deal Scanner migration is not installed.")
    return {"ok": True, "reference_id": row.reference_id, "status": row.status, "inventory_created": False}


@router.post("/api/deal-scanner/references/{reference_id}/photos")
async def add_scan_photo(reference_id: int, photo: UploadFile = File(...), category: str = "product",
                          database: Session = Depends(get_database)):
    if not _has_reference_table(database):
        raise HTTPException(status_code=503, detail="Deal Scanner migration is not installed.")
    row = database.get(DealScanReference, reference_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scan reference not found.")
    suffix = PHOTO_TYPES.get(photo.content_type or "")
    if not suffix:
        raise HTTPException(status_code=415, detail="Use a JPEG, PNG, or WebP photo.")
    data = await photo.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo is larger than 10 MB.")
    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    filename = f"{reference_id}-{uuid.uuid4().hex}{suffix}"
    path = PHOTO_ROOT / filename
    path.write_bytes(data)
    refs = json.loads(row.image_refs_json or "[]")
    refs.append({"category": category[:40], "path": f"/static/product-images/deal-scanner/{filename}", "content_type": photo.content_type})
    row.image_refs_json = json.dumps(refs)
    row.updated_at = datetime.now()
    database.commit()
    return {"ok": True, "image_ref": refs[-1]}


def install_deal_scanner(app) -> None:
    app.include_router(router)
