"""Owner-only, read-only channel inventory deployment preflight pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.channel_inventory_preflight import PRODUCTION_DB, build_report
from app.services.channel_inventory_production_review import run_production_review
from app.services.channel_inventory_review_workflow import (
    EXPLICIT_MAPPING_CONFIRMATION, EXPLICIT_REVIEW_CONFIRMATION, apply_confirmed_mapping,
    mapping_confirmation_preview, mark_reviewed, search_products,
)

templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


def _owner(request: Request) -> None:
    user = getattr(request.state, "auth_user", None)
    if user is not None and getattr(user, "role", "") != "owner_admin":
        raise HTTPException(status_code=403, detail="Owner/admin access is required.")


def _strict_owner(request: Request):
    user = getattr(request.state,"auth_user",None)
    if user is None or getattr(user,"role","") != "owner_admin":
        raise HTTPException(status_code=403,detail="Owner/admin access is required.")
    return user


def _review_context(request: Request, view: str, days: int, query: str = "", selected: dict | None = None,
                    error: str = "", message: str = "") -> dict:
    cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    report = run_production_review(PRODUCTION_DB,cutoff)
    current = [row for row in report["lines"] if row["current_open_candidate"]]
    historical = [row for row in report["lines"] if not row["current_open_candidate"]]
    lines = historical if view == "historical" else current
    candidates = search_products(PRODUCTION_DB,query) if query else []
    return {"request":request,"report":report,"lines":lines,"view":view,"days":days,"query":query,
            "candidates":candidates,"mapping_preview":selected,
            "mapping_confirmation":EXPLICIT_MAPPING_CONFIRMATION,"review_confirmation":EXPLICIT_REVIEW_CONFIRMATION,
            "error":error,"message":message}


def install_channel_inventory_admin(app: FastAPI) -> None:
    @app.get("/admin/channel-inventory-engine", response_class=HTMLResponse)
    def channel_inventory_engine_admin(request: Request, hours: int = 24):
        _owner(request)
        hours = hours if hours in {1, 6, 24, 72, 168, 720} else 24
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        report = build_report(PRODUCTION_DB, cutoff=cutoff)
        return templates.TemplateResponse(request=request, name="channel_inventory_engine_admin.html",
                                          context={"report": report, "hours": hours})

    @app.get("/admin/channel-inventory-review",response_class=HTMLResponse)
    def channel_inventory_review(request:Request,view:str="current",days:int=30,q:str="",message:str="",error:str=""):
        _strict_owner(request)
        view = view if view in {"current","historical"} else "current"
        days = days if days in {7,14,30,60,90} else 30
        return templates.TemplateResponse(request=request,name="channel_inventory_review.html",
                                          context=_review_context(request,view,days,q,error=error,message=message))

    @app.post("/admin/channel-inventory-review/mapping-preview",response_class=HTMLResponse)
    def channel_inventory_mapping_preview(request:Request,channel:str=Form(...),order_id:str=Form(...),
                                          order_line_id:str=Form(...),product_id:int=Form(...),days:int=Form(30)):
        _strict_owner(request)
        try:
            selected = mapping_confirmation_preview(PRODUCTION_DB,channel,order_id,order_line_id,product_id)
        except (ValueError,RuntimeError) as exc:
            return templates.TemplateResponse(request=request,name="channel_inventory_review.html",
                                              context=_review_context(request,"current",days,error=str(exc)),
                                              status_code=400)
        return templates.TemplateResponse(request=request,name="channel_inventory_review.html",
                                          context=_review_context(request,"current",days,"",selected))

    @app.post("/admin/channel-inventory-review/confirm-mapping")
    def channel_inventory_confirm_mapping(request:Request,channel:str=Form(...),order_id:str=Form(...),
                                          order_line_id:str=Form(...),selected_product_id:int=Form(...),
                                          confirmation_phrase:str=Form(""),days:int=Form(30)):
        _strict_owner(request)
        selected = None
        try:
            selected = mapping_confirmation_preview(
                PRODUCTION_DB,channel,order_id,order_line_id,selected_product_id)
            apply_confirmed_mapping(PRODUCTION_DB,selected,confirmation=confirmation_phrase)
        except (ValueError, RuntimeError) as exc:
            context = _review_context(request,"current",days,selected=selected,error=str(exc))
            return templates.TemplateResponse(request=request,name="channel_inventory_review.html",
                                              context=context,status_code=400)
        except Exception:
            logger.exception("Marketplace mapping confirmation failed and was rolled back")
            context = _review_context(
                request,"current",days,selected=selected,
                error="Mapping was not changed because the transaction failed. Review the server log and retry.")
            return templates.TemplateResponse(request=request,name="channel_inventory_review.html",
                                              context=context,status_code=500)
        return RedirectResponse("/admin/channel-inventory-review?message=Mapping+confirmed",status_code=303)

    @app.post("/admin/channel-inventory-review/mark-reviewed")
    def channel_inventory_mark_reviewed(request:Request,channel:str=Form(...),order_id:str=Form(...),
                                        order_line_id:str=Form(...),confirmation:str=Form("")):
        user = _strict_owner(request)
        actor = str(getattr(user,"display_name",None) or getattr(user,"user_id","owner_admin"))
        mark_reviewed(PRODUCTION_DB,channel,order_id,order_line_id,actor,confirmation=confirmation)
        return RedirectResponse("/admin/channel-inventory-review?message=Marked+reviewed",status_code=303)
