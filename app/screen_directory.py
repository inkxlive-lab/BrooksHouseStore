"""Screen Directory route installer."""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.screen_registry import directory_context


def install_screen_directory(app: FastAPI, templates) -> None:
    @app.get("/screen-directory", response_class=HTMLResponse)
    def screen_directory(request: Request):
        route_paths = [getattr(route, "path", "") for route in request.app.routes]
        context = directory_context(route_paths)
        context["request"] = request
        return templates.TemplateResponse(
            request=request, name="screen_directory.html", context=context,
        )
