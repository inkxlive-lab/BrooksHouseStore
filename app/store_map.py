from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import APP_DIRECTORY
from app.database.connection import engine


router = APIRouter()
templates = Jinja2Templates(directory=str(APP_DIRECTORY / "templates"))

MAP_KEY = "store-floor"
MAP_TITLE = "BrooksHouse Store Floor"
MAX_AREAS = 250
ALLOWED_KINDS = {"fixture", "wall", "divider", "zone", "door"}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _default_layout() -> list[dict]:
    return [
        {"id": "front-window", "label": "Front window displays", "kind": "fixture", "x": 40, "y": 545, "width": 260, "height": 55, "rotation": 0, "color": "#d66543", "location_id": None, "container_id": ""},
        {"id": "front-seasonal", "label": "Seasonal · Promo · Socks", "kind": "fixture", "x": 675, "y": 500, "width": 250, "height": 95, "rotation": 0, "color": "#d66543", "location_id": None, "container_id": ""},
        {"id": "right-wall", "label": "Right wall merchandise", "kind": "fixture", "x": 875, "y": 185, "width": 70, "height": 285, "rotation": 0, "color": "#b94a48", "location_id": None, "container_id": ""},
        {"id": "school-stationery", "label": "School · Stationery", "kind": "fixture", "x": 610, "y": 285, "width": 205, "height": 85, "rotation": 0, "color": "#c77843", "location_id": None, "container_id": ""},
        {"id": "center-clothing", "label": "Clothing racks · Games", "kind": "fixture", "x": 390, "y": 330, "width": 160, "height": 105, "rotation": 0, "color": "#8f5267", "location_id": None, "container_id": ""},
        {"id": "left-aisle-one", "label": "Small electric · Auto · Tools", "kind": "fixture", "x": 115, "y": 255, "width": 85, "height": 225, "rotation": 0, "color": "#9a713d", "location_id": None, "container_id": ""},
        {"id": "left-aisle-two", "label": "Kitchen · Hardware · Office", "kind": "fixture", "x": 215, "y": 255, "width": 85, "height": 225, "rotation": 0, "color": "#9a713d", "location_id": None, "container_id": ""},
        {"id": "kids-toys", "label": "Kids · Toys · Table", "kind": "fixture", "x": 35, "y": 255, "width": 55, "height": 225, "rotation": 0, "color": "#4b7b73", "location_id": None, "container_id": ""},
        {"id": "cleaning-outdoor", "label": "Cleaning · Outdoor", "kind": "fixture", "x": 45, "y": 135, "width": 500, "height": 65, "rotation": 0, "color": "#55764f", "location_id": None, "container_id": ""},
        {"id": "checkout", "label": "Checkout · Candy", "kind": "fixture", "x": 650, "y": 90, "width": 275, "height": 70, "rotation": 0, "color": "#8f1f24", "location_id": None, "container_id": ""},
        {"id": "back-stock", "label": "Back stock / Back-room access", "kind": "fixture", "x": 130, "y": 45, "width": 415, "height": 55, "rotation": 0, "color": "#555555", "location_id": None, "container_id": ""},
        {"id": "front-wall-left", "label": "Front wall", "kind": "wall", "x": 20, "y": 620, "width": 405, "height": 10, "rotation": 0, "color": "#3f3a37", "location_id": None, "container_id": ""},
        {"id": "front-wall-right", "label": "Front wall", "kind": "wall", "x": 575, "y": 620, "width": 405, "height": 10, "rotation": 0, "color": "#3f3a37", "location_id": None, "container_id": ""},
        {"id": "entrance", "label": "Front entrance", "kind": "door", "x": 425, "y": 610, "width": 150, "height": 20, "rotation": 0, "color": "#26734d", "location_id": None, "container_id": ""},
    ]


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, "auth_user", None)
    return bool(user and getattr(user, "role", "") == "owner_admin")


def _require_admin(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Owner/admin access is required to edit the store map.")
    return request.state.auth_user


def _initialize_tables() -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS storage_gallery_settings (
                location_id INTEGER PRIMARY KEY,
                display_name VARCHAR(160) NOT NULL,
                slot_prefix VARCHAR(80) NOT NULL,
                cover_image_path VARCHAR(500),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS store_maps (
                map_key VARCHAR(80) PRIMARY KEY,
                title VARCHAR(160) NOT NULL,
                layout_json TEXT NOT NULL,
                updated_by_user_id INTEGER,
                updated_by_name VARCHAR(160),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS store_map_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                map_key VARCHAR(80) NOT NULL,
                layout_json TEXT NOT NULL,
                saved_by_user_id INTEGER,
                saved_by_name VARCHAR(160),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS store_map_area_photos (
                map_key VARCHAR(80) NOT NULL,
                area_key VARCHAR(120) NOT NULL,
                photo_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (map_key, area_key, photo_id)
            )
        """))
        exists = connection.execute(
            text("SELECT map_key FROM store_maps WHERE map_key = :map_key"),
            {"map_key": MAP_KEY},
        ).first()
        if not exists:
            connection.execute(
                text("""
                    INSERT INTO store_maps (map_key, title, layout_json)
                    VALUES (:map_key, :title, :layout_json)
                """),
                {"map_key": MAP_KEY, "title": MAP_TITLE, "layout_json": json.dumps(_default_layout())},
            )


def _locations(connection) -> list[dict]:
    rows = connection.execute(text("""
        SELECT location_id, location_name, location_type, description
        FROM inventory_locations
        WHERE active = 1
        ORDER BY location_name
    """)).mappings().all()
    return [dict(row) for row in rows]


def _load_layout(connection, map_key: str = MAP_KEY) -> tuple[dict, list[dict]]:
    row = connection.execute(
        text("SELECT * FROM store_maps WHERE map_key = :map_key"),
        {"map_key": map_key},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Map not found.")
    try:
        layout = json.loads(row["layout_json"])
    except Exception:
        layout = _default_layout()
    return dict(row), layout


def _money(value) -> float:
    return round(float(value or 0), 2)


def _inventory_summary(connection, location_id: int | None, container_id: str = "") -> dict:
    if not location_id:
        return {"products": 0, "units": 0, "cost_value": 0, "store_value": 0, "suggested_value": 0, "margin": 0, "linked": False}
    parameters = {"location_id": int(location_id)}
    container_clause = ""
    clean_container = str(container_id or "").strip()
    if clean_container:
        container_clause = " AND UPPER(TRIM(i.container_id)) = UPPER(:container_id)"
        parameters["container_id"] = clean_container
    row = connection.execute(text(f"""
        SELECT
            COUNT(DISTINCT CASE WHEN COALESCE(i.quantity_on_hand, 0) > 0 THEN i.product_id END) AS products,
            COALESCE(SUM(CASE WHEN i.quantity_on_hand > 0 THEN i.quantity_on_hand ELSE 0 END), 0) AS units,
            COALESCE(SUM(CASE WHEN i.quantity_on_hand > 0 THEN i.quantity_on_hand * COALESCE(p.average_cost, 0) ELSE 0 END), 0) AS cost_value,
            COALESCE(SUM(CASE WHEN i.quantity_on_hand > 0 THEN i.quantity_on_hand * COALESCE(p.store_price, 0) ELSE 0 END), 0) AS store_value,
            COALESCE(SUM(CASE WHEN i.quantity_on_hand > 0 THEN i.quantity_on_hand * COALESCE(p.suggested_retail_price, 0) ELSE 0 END), 0) AS suggested_value
        FROM inventory i
        JOIN products p ON p.product_id = i.product_id
        WHERE i.location_id = :location_id {container_clause}
    """), parameters).mappings().first()
    store_value = _money(row["store_value"])
    cost_value = _money(row["cost_value"])
    return {
        "products": int(row["products"] or 0), "units": int(row["units"] or 0),
        "cost_value": cost_value, "store_value": store_value,
        "suggested_value": _money(row["suggested_value"]),
        "margin": _money(store_value - cost_value), "linked": True,
    }


def _area_photos(connection, map_key: str, area: dict) -> list[dict]:
    rows = connection.execute(text("""
        SELECT sp.photo_id, sp.image_path, sp.caption, sp.created_at
        FROM store_map_area_photos link
        JOIN storage_photos sp ON sp.photo_id = link.photo_id
        WHERE link.map_key = :map_key AND link.area_key = :area_key
        ORDER BY sp.created_at DESC
        LIMIT 12
    """), {"map_key": map_key, "area_key": area.get("id", "")}).mappings().all()
    if rows:
        return [dict(row) for row in rows]
    location_id = area.get("location_id")
    container_id = str(area.get("container_id") or "").strip()
    if not location_id or not container_id:
        return []
    rows = connection.execute(text("""
        SELECT photo_id, image_path, caption, created_at
        FROM storage_photos
        WHERE location_id = :location_id AND UPPER(TRIM(container_id)) = UPPER(:container_id)
        ORDER BY created_at DESC LIMIT 12
    """), {"location_id": int(location_id), "container_id": container_id}).mappings().all()
    return [dict(row) for row in rows]


def _storage_payload(connection, locations: list[dict]) -> list[dict]:
    storage = []
    for location in locations:
        name = str(location["location_name"] or "")
        if "trailer" not in name.casefold() and "container" not in name.casefold():
            continue
        summary = _inventory_summary(connection, location["location_id"])
        media = connection.execute(text("""
            SELECT photo_id, image_path, caption, created_at
            FROM storage_photos WHERE location_id = :location_id
            ORDER BY created_at DESC LIMIT 6
        """), {"location_id": location["location_id"]}).mappings().all()
        settings = connection.execute(text("""
            SELECT display_name, cover_image_path FROM storage_gallery_settings
            WHERE location_id = :location_id
        """), {"location_id": location["location_id"]}).mappings().first()
        storage.append({
            **location,
            "display_name": settings["display_name"] if settings and settings["display_name"] else name,
            "cover_image_path": settings["cover_image_path"] if settings else None,
            "summary": summary,
            "photos": [dict(row) for row in media],
            "gallery_url": f"/storage-gallery/{location['location_id']}",
        })
    return storage


def _payload(map_key: str = MAP_KEY) -> dict:
    with engine.begin() as connection:
        map_record, layout = _load_layout(connection, map_key)
        locations = _locations(connection)
        location_names = {row["location_id"]: row["location_name"] for row in locations}
        areas = []
        for raw in layout:
            area = dict(raw)
            area["summary"] = _inventory_summary(connection, area.get("location_id"), area.get("container_id", ""))
            area["photos"] = _area_photos(connection, map_key, area)
            area["location_name"] = location_names.get(area.get("location_id"))
            areas.append(area)
        return {
            "map": {"map_key": map_record["map_key"], "title": map_record["title"], "updated_at": str(map_record.get("updated_at") or ""), "updated_by_name": map_record.get("updated_by_name")},
            "areas": areas,
            "locations": locations,
            "outside_storage": _storage_payload(connection, locations),
            "all_location_summaries": [{**location, "summary": _inventory_summary(connection, location["location_id"])} for location in locations],
        }


def _clean_area(raw: dict, valid_location_ids: set[int]) -> dict:
    area_id = re.sub(r"[^a-zA-Z0-9_-]", "-", str(raw.get("id") or ""))[:120].strip("-")
    if not area_id:
        raise HTTPException(status_code=400, detail="Every map object needs an id.")
    kind = str(raw.get("kind") or "fixture").lower()
    if kind not in ALLOWED_KINDS:
        kind = "fixture"
    def number(name, minimum, maximum, default):
        try: value = float(raw.get(name, default))
        except (TypeError, ValueError): value = float(default)
        return round(max(minimum, min(maximum, value)), 2)
    location_id = raw.get("location_id")
    try: location_id = int(location_id) if location_id not in (None, "") else None
    except (TypeError, ValueError): location_id = None
    if location_id not in valid_location_ids:
        location_id = None
    color = str(raw.get("color") or "#d66543")
    if not HEX_COLOR.match(color):
        color = "#d66543"
    return {
        "id": area_id, "label": str(raw.get("label") or "Unnamed area").strip()[:100] or "Unnamed area",
        "kind": kind, "x": number("x", 0, 1000, 100), "y": number("y", 0, 650, 100),
        "width": number("width", 8, 1000, 120), "height": number("height", 5, 650, 60),
        "rotation": number("rotation", 0, 355, 0), "color": color,
        "location_id": location_id, "container_id": str(raw.get("container_id") or "").strip()[:120],
    }


@router.get("/store-map", response_class=HTMLResponse)
def store_map_page(request: Request):
    payload = _payload()
    return templates.TemplateResponse(request=request, name="store_map.html", context={
        "payload_json": json.dumps(payload, default=str).replace("</", "<\\/"),
        "is_admin": _is_admin(request), "user": getattr(request.state, "auth_user", None),
    })


@router.get("/api/store-map/data")
def store_map_data():
    return JSONResponse(_payload())


@router.post("/api/store-map/layout")
async def save_store_map_layout(request: Request):
    user = _require_admin(request)
    body = await request.json()
    incoming = body.get("areas") if isinstance(body, dict) else None
    if not isinstance(incoming, list) or len(incoming) > MAX_AREAS:
        raise HTTPException(status_code=400, detail=f"Layout must contain no more than {MAX_AREAS} objects.")
    with engine.begin() as connection:
        valid_location_ids = {int(row[0]) for row in connection.execute(text("SELECT location_id FROM inventory_locations WHERE active = 1"))}
        cleaned = [_clean_area(area, valid_location_ids) for area in incoming]
        ids = [area["id"] for area in cleaned]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=400, detail="Map object ids must be unique.")
        current, _ = _load_layout(connection, MAP_KEY)
        connection.execute(text("""
            INSERT INTO store_map_versions (map_key, layout_json, saved_by_user_id, saved_by_name)
            VALUES (:map_key, :layout_json, :user_id, :display_name)
        """), {"map_key": MAP_KEY, "layout_json": current["layout_json"], "user_id": user.user_id, "display_name": user.display_name})
        connection.execute(text("""
            UPDATE store_maps SET layout_json = :layout_json, updated_by_user_id = :user_id,
                updated_by_name = :display_name, updated_at = CURRENT_TIMESTAMP WHERE map_key = :map_key
        """), {"layout_json": json.dumps(cleaned), "user_id": user.user_id, "display_name": user.display_name, "map_key": MAP_KEY})
    return {"ok": True, "message": "Store map saved for every device.", "payload": _payload()}


@router.post("/api/store-map/photo")
async def upload_store_map_photo(
    request: Request,
    area_key: str = Form(...),
    caption: str = Form(""),
    photo: UploadFile = File(...),
):
    _require_admin(request)
    allowed = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif"}
    content_type = str(photo.content_type or "").casefold()
    if content_type not in allowed:
        raise HTTPException(status_code=400, detail="Choose a JPG, PNG, WEBP, HEIC, or HEIF photo.")
    content = await photo.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo is larger than 25 MB.")
    with engine.begin() as connection:
        _, layout = _load_layout(connection, MAP_KEY)
        area = next((item for item in layout if str(item.get("id")) == area_key), None)
        if not area or not area.get("location_id"):
            raise HTTPException(status_code=400, detail="Link this map area to an inventory location before adding photos.")
        location_id = int(area["location_id"])
        container_id = str(area.get("container_id") or "").strip() or f"MAPAREA:{area_key}"
        folder = APP_DIRECTORY / "static" / "storage_gallery" / "store_map" / area_key
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid4().hex}{allowed[content_type]}"
        (folder / filename).write_bytes(content)
        public_path = f"/static/storage_gallery/store_map/{area_key}/{filename}"
        result = connection.execute(text("""
            INSERT INTO storage_photos (location_id, container_id, image_path, caption, created_at)
            VALUES (:location_id, :container_id, :image_path, :caption, CURRENT_TIMESTAMP)
        """), {"location_id": location_id, "container_id": container_id, "image_path": public_path, "caption": caption.strip()[:500] or None})
        photo_id = result.lastrowid
        connection.execute(text("""
            INSERT INTO store_map_area_photos (map_key, area_key, photo_id)
            VALUES (:map_key, :area_key, :photo_id)
        """), {"map_key": MAP_KEY, "area_key": area_key, "photo_id": photo_id})
    return {"ok": True, "message": "Photo added to this map area and Storage Gallery.", "payload": _payload()}


def install_store_map(app) -> None:
    _initialize_tables()
    app.include_router(router)
