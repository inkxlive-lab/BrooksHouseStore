"""Owner-only, read-only channel inventory deployment preflight pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.channel_inventory_preflight import PRODUCTION_DB, build_report

templates = Jinja2Templates(directory="app/templates")


def _owner(request: Request) -> None:
    user = getattr(request.state, "auth_user", None)
    if user is not None and getattr(user, "role", "") != "owner_admin":
        raise HTTPException(status_code=403, detail="Owner/admin access is required.")


def install_channel_inventory_admin(app: FastAPI) -> None:
    @app.get("/admin/channel-inventory-engine", response_class=HTMLResponse)
    def channel_inventory_engine_admin(request: Request, hours: int = 24):
        _owner(request)
        hours = hours if hours in {1, 6, 24, 72, 168, 720} else 24
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        report = build_report(PRODUCTION_DB, cutoff=cutoff)
        return templates.TemplateResponse(request=request, name="channel_inventory_engine_admin.html",
                                          context={"report": report, "hours": hours})
