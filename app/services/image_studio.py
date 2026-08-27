from __future__ import annotations

import base64
import ipaddress
import json
import logging
import mimetypes
import os
import socket
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Callable
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url

from app.config import DATABASE_URL
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")
LOGGER = logging.getLogger(__name__)


def _resolve_db_path() -> Path:
    url = make_url(DATABASE_URL)
    if not url.drivername.startswith("sqlite"):
        raise RuntimeError(f"Image Studio currently requires SQLite, got: {url.drivername}")

    database = url.database
    if not database:
        raise RuntimeError("DATABASE_URL does not contain a SQLite database path")

    path = Path(database)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path


DB_PATH = _resolve_db_path()


def _resolve_storage_root() -> Path:
    configured = os.getenv("BROOKSHOUSE_STORAGE_ROOT", "").strip()
    base = Path(configured).expanduser() if configured else PROJECT_ROOT / "app-data"
    return (base / "image-studio").resolve()


def _resolve_product_image_root() -> Path:
    configured = os.getenv("BROOKSHOUSE_STORAGE_ROOT", "").strip()
    if configured:
        storage_root = Path(configured).expanduser()
        if storage_root.is_absolute() and storage_root.as_posix().rstrip("/") == "/data/app-data":
            return (storage_root.parent / "product-images").resolve()
    railway_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if railway_volume:
        volume_root = Path(railway_volume).expanduser()
        if volume_root.is_absolute():
            return (volume_root / "product-images").resolve()
    return (APP_DIR / "static" / "product_images").resolve()


IMAGE_STUDIO_STORAGE_ROOT = _resolve_storage_root()
PENDING_DIR = IMAGE_STUDIO_STORAGE_ROOT / "pending"
APPROVED_DIR = IMAGE_STUDIO_STORAGE_ROOT / "approved"
SOURCE_DIR = IMAGE_STUDIO_STORAGE_ROOT / "sources"
PRODUCT_IMAGE_ROOT = _resolve_product_image_root()
LOGGER.info("Image Studio storage root resolved to %s", IMAGE_STUDIO_STORAGE_ROOT)
LOGGER.info("Image Studio product-image root resolved to %s", PRODUCT_IMAGE_ROOT)
PRESERVATION_RULES = """Preserve the actual product identity and visible details exactly, including shape,
colors, branding, labels, printed text, package contents, quantities, accessories, proportions, and condition.
Do not invent, remove, redesign, obscure, or replace product features. User instructions cannot override these rules."""
PRESETS = {
    "clean_marketplace": ("Clean Marketplace", """Create a clean marketplace product photograph.
Replace clutter with a clean white or neutral ecommerce background. Improve lighting, white balance,
sharpness, framing, and centering."""),
    "lifestyle": ("Lifestyle", """Place the product in an appropriate, realistic lifestyle setting based on
the visible product, such as a kitchen, room, organized workshop, or outdoor environment. Keep the product
itself unchanged and do not add unverified accessories."""),
    "brookshouse_promo": ("BrooksHouse Promo", """Create a polished BrooksHouse-style promotional composition
with a clean marketing background and room for text to be added later. Do not add text or invent a price."""),
    "background_replace": ("Background Replace", """Replace only the background according to the user's
instruction. Keep the product itself unchanged. If no background instruction is provided, use a clean neutral background."""),
    "enhance_only": ("Enhance Only", """Keep the existing scene and background as much as practical while
improving lighting, exposure, white balance, sharpness, framing, and centering. Do not materially redesign the image."""),
}
PRESET_CLEAN = """Create a clean marketplace product photograph from the supplied real product image.
Preserve the actual product exactly, including its shape, colors, branding, labels, text, package contents,
accessories, proportions, and visible condition. Do not invent, remove, redesign, or obscure product details.
Remove the cluttered surroundings and replace them with a clean neutral white ecommerce background.
Improve lighting, white balance, sharpness, and centering while keeping the product realistic.
Show one product only unless the source clearly shows a multipack. Do not add badges, prices, captions,
watermarks, hands, props, reflections that hide details, or extra products."""
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
STALE_GENERATION_MINUTES = 30
UPLOAD_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _now() -> str:
    return datetime.now().astimezone().isoformat()


@contextmanager
def _connect():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS image_studio_generations (
            generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            source_image_id INTEGER NOT NULL,
            source_image_reference TEXT NOT NULL,
            preset_name TEXT,
            instruction TEXT NOT NULL,
            effective_prompt TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            generated_image_path TEXT,
            approved_product_image_id INTEGER,
            save_as_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            error_message TEXT,
            FOREIGN KEY(product_id) REFERENCES products(product_id),
            FOREIGN KEY(source_image_id) REFERENCES product_images(image_id),
            FOREIGN KEY(approved_product_image_id) REFERENCES product_images(image_id)
        );
        CREATE INDEX IF NOT EXISTS ix_image_studio_product
            ON image_studio_generations(product_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS ix_image_studio_status
            ON image_studio_generations(status, created_at DESC);
        CREATE TABLE IF NOT EXISTS image_studio_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            stored_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            product_image_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(product_id),
            FOREIGN KEY(product_image_id) REFERENCES product_images(image_id)
        );
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(image_studio_generations)")}
    for name, definition in (
        ("source_upload_id", "INTEGER"),
        ("metadata_json", "TEXT"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE image_studio_generations ADD COLUMN {name} {definition}")


def _prompt_for(preset: str, instruction: str) -> tuple[str, str]:
    if preset not in PRESETS:
        raise ValueError("Select a valid generation preset.")
    label, preset_prompt = PRESETS[preset]
    user_part = f"\n\nUser request (subject to preservation rules): {instruction}" if instruction else ""
    return label, f"{PRESERVATION_RULES}\n\n{preset_prompt}{user_part}"


def _mark_stale_generations(connection: sqlite3.Connection) -> None:
    cutoff = (datetime.now().astimezone() - timedelta(minutes=STALE_GENERATION_MINUTES)).isoformat()
    connection.execute(
        """UPDATE image_studio_generations SET status='failed', error_message=?, reviewed_at=?
           WHERE status='generating' AND created_at < ?""",
        ("Generation was interrupted before completion. Retry when ready.", _now(), cutoff),
    )


@dataclass(frozen=True)
class ProviderResult:
    image_bytes: bytes
    extension: str = ".png"


class ImageProvider(ABC):
    name: str
    model: str

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def edit(self, source: bytes, filename: str, content_type: str, prompt: str) -> ProviderResult: ...


class OpenAIImageProvider(ImageProvider):
    name = "openai"

    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("BROOKSHOUSE_IMAGE_MODEL", "gpt-image-2").strip() or "gpt-image-2"
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.timeout = float(os.getenv("BROOKSHOUSE_IMAGE_TIMEOUT_SECONDS", "180"))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def edit(self, source: bytes, filename: str, content_type: str, prompt: str) -> ProviderResult:
        if not self.configured:
            raise RuntimeError("OpenAI image generation is not configured.")
        response = httpx.post(
            f"{self.base_url}/images/edits",
            headers={"Authorization": f"Bearer {self.api_key}"},
            data={"model": self.model, "prompt": prompt, "size": "1024x1024", "quality": "high"},
            files={"image[]": (filename, source, content_type)},
            timeout=self.timeout,
        )
        if response.is_error:
            request_id = response.headers.get("x-request-id")
            suffix = f" (request {request_id})" if request_id else ""
            raise RuntimeError(f"OpenAI image edit failed with status {response.status_code}{suffix}.")
        try:
            encoded = response.json()["data"][0]["b64_json"]
            return ProviderResult(base64.b64decode(encoded, validate=True))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenAI returned an image response the studio could not read.") from exc


ProviderFactory = Callable[[], ImageProvider]
_provider_factory: ProviderFactory = OpenAIImageProvider


def _public_http_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def _local_static_path(reference: str) -> Path | None:
    direct = Path(reference)
    static_root = (APP_DIR / "static").resolve()
    if direct.is_absolute():
        candidate = direct.resolve()
        return candidate if candidate.is_relative_to(static_root) else None
    parsed = urlparse(reference)
    value = parsed.path if parsed.scheme in {"http", "https"} else reference
    if not value.startswith("/static/"):
        return None
    candidate = (APP_DIR / value.lstrip("/")).resolve()
    return candidate if candidate.is_relative_to(static_root) else None


def _studio_file_path(reference: str, expected_area: str | None = None) -> Path | None:
    parsed = urlparse(reference)
    path = parsed.path if parsed.scheme in {"http", "https"} else reference
    prefix = "/images/studio/files/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) != 2 or parts[0] not in {"pending", "approved", "sources"} or not parts[1]:
        return None
    area, filename = parts
    if expected_area is not None and area != expected_area:
        return None
    roots = {"pending": PENDING_DIR, "approved": APPROVED_DIR, "sources": SOURCE_DIR}
    root = roots[area]
    candidate = (root / filename).resolve()
    return candidate if candidate.parent == root.resolve() else None


def _persistent_product_image_path(reference: str) -> Path | None:
    parsed = urlparse(reference)
    value = parsed.path if parsed.scheme in {"http", "https"} else reference
    relative: Path | None = None
    for prefix in ("/static/product_images/", "/images/studio/product-images/"):
        if value.startswith(prefix):
            relative = Path(value[len(prefix):])
            break
    if relative is None:
        windows_path = PureWindowsPath(reference)
        legacy_root = PureWindowsPath(r"C:\BrooksHouseStore\app\static\product_images")
        path_parts = tuple(part.casefold() for part in windows_path.parts)
        root_parts = tuple(part.casefold() for part in legacy_root.parts)
        if path_parts[:len(root_parts)] != root_parts or len(path_parts) <= len(root_parts):
            return None
        relative = Path(*windows_path.parts[len(root_parts):])
    root = PRODUCT_IMAGE_ROOT.resolve()
    candidate = (root / relative).resolve()
    return candidate if candidate.is_relative_to(root) and candidate != root else None


def _source_local_path(reference: str) -> Path | None:
    studio = _studio_file_path(reference)
    if studio is not None:
        return studio
    persistent = _persistent_product_image_path(reference)
    if persistent is not None and persistent.is_file():
        return persistent
    return _local_static_path(reference)


def _display_reference(reference: str) -> str:
    if _studio_file_path(reference) is not None:
        return urlparse(reference).path
    persistent = _persistent_product_image_path(reference)
    if persistent is not None and persistent.is_file():
        relative = persistent.relative_to(PRODUCT_IMAGE_ROOT.resolve()).as_posix()
        return f"/images/studio/product-images/{quote(relative, safe='/')}"
    local = _local_static_path(reference)
    if local is None:
        return reference
    relative = local.relative_to((APP_DIR / "static").resolve()).as_posix()
    return f"/static/{relative}"


def _read_source(reference: str) -> tuple[bytes, str, str]:
    local = _source_local_path(reference)
    if local is not None:
        if not local.is_file():
            raise ValueError("The selected local source image is unavailable.")
        if local.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("The selected source image is larger than 20 MB.")
        return local.read_bytes(), local.name, mimetypes.guess_type(local.name)[0] or "image/png"
    if not _public_http_url(reference):
        raise ValueError("The selected source image location is not allowed.")
    with httpx.Client(follow_redirects=False, timeout=30) as client:
        response = client.get(reference)
    if response.is_redirect:
        raise ValueError("Redirecting source image URLs are not allowed.")
    response.raise_for_status()
    if len(response.content) > MAX_SOURCE_BYTES:
        raise ValueError("The selected source image is larger than 20 MB.")
    content_type = response.headers.get("content-type", "image/png").split(";", 1)[0].lower()
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ValueError("The selected source is not a supported image.")
    filename = Path(urlparse(reference).path).name or "product-image.png"
    return response.content, filename, content_type


def _image_reference(row: sqlite3.Row) -> str | None:
    return row["image_path"] or row["image_url"]


def _load_products(connection: sqlite3.Connection, selected_id: int | None, search: str) -> list[dict]:
    where = ""
    values: list[object] = []
    if search:
        where = "WHERE p.product_name LIKE ? OR CAST(p.product_id AS TEXT)=?"
        values.extend([f"%{search}%", search])
    if selected_id is not None:
        where = "WHERE p.product_id=?"
        values = [selected_id]
    rows = connection.execute(
        f"""SELECT p.product_id, p.product_name,
                   (SELECT pb.barcode FROM product_barcodes pb WHERE pb.product_id=p.product_id
                    ORDER BY pb.is_primary DESC, pb.barcode_id LIMIT 1) barcode,
                   COUNT(pi.image_id) image_count
            FROM products p JOIN product_images pi ON pi.product_id=p.product_id
            {where}
            GROUP BY p.product_id, p.product_name
            ORDER BY p.product_name LIMIT 100""",
        values,
    ).fetchall()
    return [dict(row) for row in rows]


def _load_images(connection: sqlite3.Connection, product_id: int | None) -> list[dict]:
    if product_id is None:
        return []
    rows = connection.execute(
        """SELECT image_id, product_id, image_path, image_url, image_type, is_primary, created_at
           FROM product_images WHERE product_id=? ORDER BY is_primary DESC, image_id""",
        (product_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["reference"] = _image_reference(row)
        if item["reference"]:
            item["display_url"] = _display_reference(item["reference"])
            result.append(item)
    return result


def _load_uploaded_sources(connection: sqlite3.Connection, product_id: int | None) -> list[dict]:
    if product_id is None:
        return []
    return [dict(row) | {"display_url": row["stored_path"]} for row in connection.execute(
        """SELECT source_id, stored_path, original_filename, content_type, byte_size,
                  product_image_id, created_at FROM image_studio_sources
           WHERE product_id=? ORDER BY source_id DESC""", (product_id,)
    ).fetchall()]


def _history(connection: sqlite3.Connection, product_id: int | None, status_filter: str) -> list[dict]:
    if product_id is None:
        return []
    allowed = {"pending", "approved", "failed", "discarded"}
    where = " AND status=?" if status_filter in allowed else ""
    values: list[object] = [product_id]
    if where:
        values.append(status_filter)
    rows = connection.execute(
        f"""SELECT * FROM image_studio_generations WHERE product_id=?{where}
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, generation_id DESC LIMIT 50""", values
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["preset_label"] = PRESETS.get(item.get("preset_name"), (item.get("preset_name") or "Custom", ""))[0]
        item["source_display_url"] = _display_reference(item["source_image_reference"])
        result.append(item)
    return result


def _generation(connection: sqlite3.Connection, generation_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM image_studio_generations WHERE generation_id=?", (generation_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    return row


def _redirect(product_id: int | None = None, message: str = "", error: str = "") -> RedirectResponse:
    params = []
    if product_id is not None:
        params.append(f"product_id={product_id}")
    if message:
        params.append(f"message={quote(message)}")
    if error:
        params.append(f"error={quote(error)}")
    return RedirectResponse("/images/studio" + ("?" + "&".join(params) if params else ""), status_code=303)


def _run_generation(
    provider: ImageProvider, product_id: int, source_image_id: int,
    source_upload_id: int | None, reference: str, preset: str, instruction: str,
) -> tuple[int, str | None]:
    preset_label, prompt = _prompt_for(preset, instruction)
    with _connect() as connection:
        cursor = connection.execute(
            """INSERT INTO image_studio_generations
               (product_id,source_image_id,source_upload_id,source_image_reference,preset_name,instruction,
                effective_prompt,provider,model,status,created_at,metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (product_id, source_image_id, source_upload_id, reference, preset, instruction, prompt,
             provider.name, provider.model, "generating", _now(), json.dumps({"preset_label": preset_label})),
        )
        generation_id = int(cursor.lastrowid)
        connection.commit()
    try:
        source, filename, content_type = _read_source(reference)
        result = provider.edit(source, filename, content_type, prompt)
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        output_path = PENDING_DIR / f"{generation_id}-{uuid4().hex}{result.extension}"
        output_path.write_bytes(result.image_bytes)
        public_path = f"/images/studio/files/pending/{output_path.name}"
        with _connect() as connection:
            connection.execute(
                "UPDATE image_studio_generations SET status='pending', generated_image_path=? WHERE generation_id=?",
                (public_path, generation_id),
            )
            connection.commit()
        return generation_id, None
    except Exception as exc:
        safe_error = str(exc)[:500] or "Image generation failed."
        with _connect() as connection:
            connection.execute(
                """UPDATE image_studio_generations SET status='failed', error_message=?, reviewed_at=?
                   WHERE generation_id=?""", (safe_error, _now(), generation_id),
            )
            connection.commit()
        return generation_id, safe_error


def install_image_studio(app: FastAPI) -> None:
    @app.get("/images/studio/files/{area}/{filename}")
    def image_studio_file(area: str, filename: str):
        path = _studio_file_path(f"/images/studio/files/{area}/{filename}", area)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Image Studio file not found")
        return FileResponse(path)

    @app.get("/images/studio/product-images/{relative_path:path}")
    def image_studio_product_image(relative_path: str):
        path = _persistent_product_image_path(f"/images/studio/product-images/{relative_path}")
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Product image not found")
        return FileResponse(path)

    @app.get("/images/studio", response_class=HTMLResponse)
    def image_studio_page(
        request: Request, product_id: int | None = None, search: str = "", history_status: str = "all",
        message: str = "", error: str = ""
    ):
        provider = _provider_factory()
        with _connect() as connection:
            _ensure_schema(connection)
            _mark_stale_generations(connection)
            connection.commit()
            products = _load_products(connection, product_id, search.strip())
            images = _load_images(connection, product_id)
            uploaded_sources = _load_uploaded_sources(connection, product_id)
            history = _history(connection, product_id, history_status)
        return TEMPLATES.TemplateResponse(request=request, name="image_studio.html", context={
            "products": products, "selected_product_id": product_id, "images": images,
            "history": history, "search": search, "message": message, "error": error,
            "provider_name": provider.name, "provider_model": provider.model,
            "provider_configured": provider.configured, "presets": PRESETS,
            "uploaded_sources": uploaded_sources, "history_status": history_status,
        })

    @app.post("/images/studio/source/upload")
    async def image_studio_upload_source(
        product_id: int = Form(...), photo: UploadFile = File(...),
        save_to_gallery: bool = Form(False),
    ):
        content_type = (photo.content_type or "").lower()
        if content_type not in UPLOAD_TYPES:
            return _redirect(product_id, error="Upload a JPEG, PNG, or WebP image.")
        data = await photo.read(MAX_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_UPLOAD_BYTES:
            return _redirect(product_id, error="The source photo must be between 1 byte and 20 MB.")
        signatures = {
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        }
        if not signatures[content_type]:
            return _redirect(product_id, error="The uploaded file content does not match its image type.")
        with _connect() as connection:
            _ensure_schema(connection)
            if connection.execute("SELECT 1 FROM products WHERE product_id=?", (product_id,)).fetchone() is None:
                return _redirect(error="Select a valid product before uploading.")
            SOURCE_DIR.mkdir(parents=True, exist_ok=True)
            filename = f"product-{product_id}-{uuid4().hex}{UPLOAD_TYPES[content_type]}"
            destination = (SOURCE_DIR / filename).resolve()
            if destination.parent != SOURCE_DIR.resolve():
                raise HTTPException(status_code=400, detail="Unsafe upload path")
            destination.write_bytes(data)
            public_path = f"/images/studio/files/sources/{filename}"
            try:
                product_image_id = None
                if save_to_gallery:
                    cursor = connection.execute(
                        """INSERT INTO product_images
                           (product_id,image_path,image_url,image_type,is_primary,created_at)
                           VALUES (?,?,?,?,0,?)""",
                        (product_id, public_path, None, "original_upload", _now()),
                    )
                    product_image_id = int(cursor.lastrowid)
                connection.execute(
                    """INSERT INTO image_studio_sources
                       (product_id,stored_path,original_filename,content_type,byte_size,product_image_id,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (product_id, public_path, Path(photo.filename or "photo").name,
                     content_type, len(data), product_image_id, _now()),
                )
                connection.commit()
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        suffix = " and added to the gallery" if save_to_gallery else ""
        return _redirect(product_id, message=f"Source photo uploaded{suffix}. Originals are protected.")

    @app.post("/images/studio/generate")
    def image_studio_generate(
        product_id: int = Form(...), source_image_id: int | None = Form(None),
        source_upload_id: int | None = Form(None), preset: str = Form("clean_marketplace"),
        instruction: str = Form(""), variations: int = Form(1),
    ):
        provider = _provider_factory()
        if not provider.configured:
            return _redirect(product_id, error="Add OPENAI_API_KEY to the server environment before generating images.")
        instruction = instruction.strip()
        if len(instruction) > 4000:
            return _redirect(product_id, error="Instructions must be 4,000 characters or fewer.")
        if variations not in {1, 2, 3, 4}:
            return _redirect(product_id, error="Choose between 1 and 4 variations.")
        try:
            _prompt_for(preset, instruction)
        except ValueError as exc:
            return _redirect(product_id, error=str(exc))
        with _connect() as connection:
            _ensure_schema(connection)
            reference = None
            selected_image_id = 0
            if source_upload_id is not None:
                source_row = connection.execute(
                    "SELECT * FROM image_studio_sources WHERE source_id=? AND product_id=?",
                    (source_upload_id, product_id),
                ).fetchone()
                if source_row is not None:
                    reference = source_row["stored_path"]
            elif source_image_id is not None:
                source_row = connection.execute(
                    "SELECT * FROM product_images WHERE image_id=? AND product_id=?",
                    (source_image_id, product_id),
                ).fetchone()
                if source_row is not None:
                    reference = _image_reference(source_row)
                    selected_image_id = source_image_id
            if not reference:
                return _redirect(product_id, error="Select a valid source image belonging to this product.")
        failures = []
        for _ in range(variations):
            _, error_message = _run_generation(
                provider, product_id, selected_image_id, source_upload_id, reference, preset, instruction
            )
            if error_message:
                failures.append(error_message)
        if failures:
            return _redirect(product_id, error=f"{len(failures)} of {variations} variation(s) failed: {failures[0]}")
        return _redirect(product_id, message=f"{variations} preview variation(s) generated. Review before approval.")

    @app.post("/images/studio/{generation_id}/retry")
    def image_studio_retry(generation_id: int):
        provider = _provider_factory()
        if not provider.configured:
            raise HTTPException(status_code=503, detail="Image generation is not configured")
        with _connect() as connection:
            _ensure_schema(connection)
            row = _generation(connection, generation_id)
            if row["status"] != "failed":
                return _redirect(row["product_id"], error="Only failed generations can be retried.")
            source_upload_id = row["source_upload_id"] if "source_upload_id" in row.keys() else None
        _, error_message = _run_generation(
            provider, row["product_id"], row["source_image_id"], source_upload_id,
            row["source_image_reference"], row["preset_name"], row["instruction"],
        )
        if error_message:
            return _redirect(row["product_id"], error=f"Retry failed: {error_message}")
        return _redirect(row["product_id"], message="Retry generated a new pending preview.")

    @app.post("/images/studio/products/{product_id}/images/{image_id}/primary")
    def image_studio_set_primary(product_id: int, image_id: int):
        with _connect() as connection:
            image = connection.execute(
                "SELECT image_id FROM product_images WHERE image_id=? AND product_id=?", (image_id, product_id)
            ).fetchone()
            if image is None:
                return _redirect(product_id, error="Product image not found.")
            connection.execute("UPDATE product_images SET is_primary=0 WHERE product_id=?", (product_id,))
            connection.execute("UPDATE product_images SET is_primary=1 WHERE image_id=?", (image_id,))
            connection.commit()
        return _redirect(product_id, message=f"Product image {image_id} is now primary.")

    @app.post("/images/studio/{generation_id}/approve")
    def image_studio_approve(generation_id: int, save_as_primary: bool = Form(False)):
        with _connect() as connection:
            _ensure_schema(connection)
            row = _generation(connection, generation_id)
            if row["status"] != "pending" or not row["generated_image_path"]:
                return _redirect(row["product_id"], error="Only pending images can be approved.")
            source_path = _studio_file_path(row["generated_image_path"], "pending")
            if source_path is None or not source_path.is_file():
                return _redirect(row["product_id"], error="The generated preview file is unavailable.")
            APPROVED_DIR.mkdir(parents=True, exist_ok=True)
            destination = APPROVED_DIR / f"product-{row['product_id']}-{generation_id}{source_path.suffix.lower()}"
            destination.write_bytes(source_path.read_bytes())
            gallery_path = f"/images/studio/files/approved/{destination.name}"
            try:
                if save_as_primary:
                    connection.execute("UPDATE product_images SET is_primary=0 WHERE product_id=?", (row["product_id"],))
                cursor = connection.execute(
                    """INSERT INTO product_images
                       (product_id,image_path,image_url,image_type,is_primary,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (row["product_id"], gallery_path, None, "ai_studio", int(save_as_primary), _now()),
                )
                connection.execute(
                    """UPDATE image_studio_generations SET status='approved', approved_product_image_id=?,
                       generated_image_path=?, save_as_primary=?, reviewed_at=? WHERE generation_id=?""",
                    (cursor.lastrowid, gallery_path, int(save_as_primary), _now(), generation_id),
                )
                connection.commit()
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        source_path.unlink(missing_ok=True)
        return _redirect(row["product_id"], message="Approved image saved to the product gallery.")

    @app.post("/images/studio/{generation_id}/discard")
    def image_studio_discard(generation_id: int):
        with _connect() as connection:
            _ensure_schema(connection)
            row = _generation(connection, generation_id)
            if row["status"] != "pending":
                return _redirect(row["product_id"], error="Only pending images can be discarded.")
            source_path = _studio_file_path(row["generated_image_path"] or "", "pending")
            if source_path is not None:
                source_path.unlink(missing_ok=True)
            connection.execute(
                "UPDATE image_studio_generations SET status='discarded', generated_image_path=NULL, reviewed_at=? WHERE generation_id=?",
                (_now(), generation_id),
            )
            connection.commit()
        return _redirect(row["product_id"], message="Generated preview discarded. The original was not changed.")

    @app.get("/images/studio/{generation_id}/source")
    def image_studio_source(generation_id: int):
        with _connect() as connection:
            _ensure_schema(connection)
            row = _generation(connection, generation_id)
        local = _source_local_path(row["source_image_reference"])
        if local is None or not local.is_file():
            raise HTTPException(status_code=404, detail="Local source image not found")
        return FileResponse(local)
