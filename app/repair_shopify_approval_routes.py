from pathlib import Path


main_path = Path(r"C:\BrooksHouseStore\app\main.py")
text = main_path.read_text(encoding="utf-8-sig")


approval_import = '''from app.services.shopify_approval import (
    build_approval_candidates,
    clear_approvals,
    save_selected_approvals,
)
'''


if "from app.services.shopify_approval import" not in text:
    possible_markers = [
        "from app.services.shopify_push_preview import",
        "from app.database.sales_channels import",
        "from app.database.models import",
        "app = FastAPI(",
    ]

    insertion_position = -1

    for marker in possible_markers:
        insertion_position = text.find(marker)

        if insertion_position != -1:
            break

    if insertion_position == -1:
        raise RuntimeError(
            "Could not find a safe import location."
        )

    text = (
        text[:insertion_position]
        + approval_import
        + "\n"
        + text[insertion_position:]
    )


route_marker = '''@app.get("/api/health")
def health_check():
'''


approval_routes = r'''
@app.get(
    "/channels/shopify/approve",
    response_class=HTMLResponse,
)
def shopify_approval_queue(
    request: Request,
    saved: str = "",
    cleared: str = "",
    database: Session = Depends(get_database),
):
    candidates = build_approval_candidates(
        database
    )

    approved_count = sum(
        1
        for row in candidates
        if row["approved"]
        and not row["stale"]
    )

    stale_count = sum(
        1
        for row in candidates
        if row["stale"]
    )

    summary = {
        "total": len(candidates),
        "approved": approved_count,
        "pending": (
            len(candidates)
            - approved_count
            - stale_count
        ),
        "stale": stale_count,
    }

    message = None

    if saved:
        message = (
            f"{saved} Shopify inventory "
            "approval(s) saved."
        )

    elif cleared == "1":
        message = (
            "All saved Shopify approvals "
            "were cleared."
        )

    return templates.TemplateResponse(
        request=request,
        name="shopify_approval_queue.html",
        context={
            "candidates": candidates,
            "summary": summary,
            "message": message,
            "error": None,
        },
    )


@app.post(
    "/channels/shopify/approve",
)
async def save_shopify_approvals(
    request: Request,
    database: Session = Depends(get_database),
):
    form_data = await request.form()

    selected_keys = {
        str(key)
        for key in form_data.getlist(
            "approval_keys"
        )
    }

    candidates = build_approval_candidates(
        database
    )

    saved_count = save_selected_approvals(
        candidates=candidates,
        selected_keys=selected_keys,
    )

    return RedirectResponse(
        url=(
            "/channels/shopify/approve"
            f"?saved={saved_count}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    "/channels/shopify/approve/clear",
)
def clear_shopify_approvals():
    clear_approvals()

    return RedirectResponse(
        url=(
            "/channels/shopify/approve"
            "?cleared=1"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/api/health")
def health_check():
'''


if '"/channels/shopify/approve"' not in text:
    if route_marker not in text:
        raise RuntimeError(
            "Could not find the API health route."
        )

    text = text.replace(
        route_marker,
        approval_routes,
        1,
    )


main_path.write_text(
    text,
    encoding="utf-8",
)

print("Shopify approval routes repaired successfully.")
