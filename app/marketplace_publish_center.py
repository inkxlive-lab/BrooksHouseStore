"""Explicit operator routes for the shared Marketplace Publish Center."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database_resolution import configured_sqlite_path
from app.services.marketplace_publish import (
    DuplicateSubmission,
    PublishError,
    ValidationBlocked,
    connect,
    prepare,
    product_center,
    queue_rows,
    search_products,
    submit,
)


templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


def _owner(request: Request) -> None:
    user = getattr(request.state, "auth_user", None)
    if user is not None and getattr(user, "role", "") != "owner_admin":
        raise HTTPException(status_code=403, detail="Owner/admin access is required.")


def _redirect(product_id: int, *, message: str = "", error: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    query = {"product_id": product_id}
    if message:
        query["message"] = message
    if error:
        query["error"] = error
    return RedirectResponse("/channels/publish?" + urlencode(query), status_code=303)


def install_marketplace_publish_center(app: FastAPI) -> None:
    @app.get("/channels/publish", response_class=HTMLResponse)
    def marketplace_publish_center(request: Request, product_id: int | None = None, q: str = "",
                                   status: str = "", channel: str = "", message: str = "", error: str = ""):
        _owner(request)
        with connect(configured_sqlite_path()) as connection:
            selected = None
            if product_id is not None:
                try:
                    selected = product_center(connection, product_id)
                except KeyError:
                    raise HTTPException(status_code=404, detail="Product not found")
            return templates.TemplateResponse(
                request=request,
                name="marketplace_publish.html",
                context={"selected": selected, "search_results": search_products(connection, q),
                         "queue": queue_rows(connection, status, channel), "q": q,
                         "status_filter": status, "channel_filter": channel,
                         "message": message, "error": error},
            )

    @app.post("/channels/publish/{channel}/{product_id}/prepare")
    def marketplace_publish_prepare(request: Request, channel: str, product_id: int,
                                    proposed_price: str = Form(""), proposed_quantity: int = Form(0),
                                    seller_sku: str = Form(""), image_ids: list[int] = Form(default=[])):
        _owner(request)
        try:
            with connect(configured_sqlite_path()) as connection:
                row = prepare(connection, channel, product_id, proposed_price, proposed_quantity, seller_sku, image_ids)
            return _redirect(product_id, message=f"{channel.title()} draft saved as {row['status']}.")
        except (ValueError, PublishError) as exc:
            return _redirect(product_id, error=str(exc))

    @app.post("/channels/publish/{publish_id}/submit")
    def marketplace_publish_submit(request: Request, publish_id: int, product_id: int = Form(...),
                                   confirmation: str = Form("")):
        _owner(request)
        if confirmation != "publish":
            return _redirect(product_id, error="Explicit publish confirmation is required.")
        try:
            with connect(configured_sqlite_path()) as connection:
                # Intentionally no default state-changing adapters. Real adapters must be
                # configured and reviewed in a separate credential-enabled task.
                submit(connection, publish_id)
            return _redirect(product_id, message="Marketplace submission accepted.")
        except (DuplicateSubmission, ValidationBlocked, PublishError) as exc:
            return _redirect(product_id, error=str(exc))
        except Exception:
            logger.exception("Marketplace publication failed without exposing request credentials")
            return _redirect(product_id, error="Submission failed safely. Review the server log and retry manually.")
