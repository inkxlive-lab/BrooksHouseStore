from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import os
import socket
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")
DB_PATH = APP_DIR / "data" / "brookshouse_store.db"
PENDING_DIR = APP_DIR / "static" / "generated-images" / "pending"
APPROVED_DIR = APP_DIR / "static" / "product-images" / "ai-studio"
PRESET_CLEAN = """Create a clean marketplace product photograph from the supplied real product image.
Preserve the actual product exactly, including its shape, colors, branding, labels, text, package contents,
accessories, proportions, and visible condition. Do not invent, remove, redesign, or obscure product details.
Remove the cluttered surroundings and replace them with a clean neutral white ecommerce background.
Improve lighting, white balance, sharpness, and centering while keeping the product realistic.
Show one product only unless the source clearly shows a multipack. Do not add badges, prices, captions,
watermarks, hands, props, reflections that hide details, or extra products."""
MAX_SOURCE_BYTES = 20 * 1024 * 1024


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
        """
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


def _display_reference(reference: str) -> str:
    local = _local_static_path(reference)
    if local is None:
        return reference
    relative = local.relative_to((APP_DIR / "static").resolve()).as_posix()
    return f"/static/{relative}"


def _read_source(reference: str) -> tuple[bytes, str, str]:
    local = _local_static_path(reference)
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


def _history(connection: sqlite3.Connection, product_id: int | None) -> list[dict]:
    if product_id is None:
        return []
    return [dict(row) for row in connection.execute(
        """SELECT * FROM image_studio_generations WHERE product_id=?
           ORDER BY generation_id DESC LIMIT 20""", (product_id,)
    ).fetchall()]


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


def install_image_studio(app: FastAPI) -> None:
    @app.get("/images/studio", response_class=HTMLResponse)
    def image_studio_page(
        request: Request, product_id: int | None = None, search: str = "", message: str = "", error: str = ""
    ):
        provider = _provider_factory()
        with _connect() as connection:
            _ensure_schema(connection)
            products = _load_products(connection, product_id, search.strip())
            images = _load_images(connection, product_id)
            history = _history(connection, product_id)
        return TEMPLATES.TemplateResponse(request=request, name="image_studio.html", context={
            "products": products, "selected_product_id": product_id, "images": images,
            "history": history, "search": search, "message": message, "error": error,
            "provider_name": provider.name, "provider_model": provider.model,
            "provider_configured": provider.configured, "preset_clean": PRESET_CLEAN,
        })

    @app.post("/images/studio/generate")
    def image_studio_generate(
        product_id: int = Form(...), source_image_id: int = Form(...),
        preset: str = Form("clean_marketplace"), instruction: str = Form(""),
    ):
        provider = _provider_factory()
        if not provider.configured:
            return _redirect(product_id, error="Add OPENAI_API_KEY to the server environment before generating images.")
        instruction = instruction.strip()
        if len(instruction) > 4000:
            return _redirect(product_id, error="Instructions must be 4,000 characters or fewer.")
        prompt = PRESET_CLEAN + (f"\n\nAdditional instruction: {instruction}" if instruction else "")
        with _connect() as connection:
            _ensure_schema(connection)
            source_row = connection.execute(
                """SELECT * FROM product_images WHERE image_id=? AND product_id=?""",
                (source_image_id, product_id),
            ).fetchone()
            if source_row is None or not _image_reference(source_row):
                return _redirect(product_id, error="Select a valid image belonging to this product.")
            reference = _image_reference(source_row)
            cursor = connection.execute(
                """INSERT INTO image_studio_generations
                   (product_id,source_image_id,source_image_reference,preset_name,instruction,effective_prompt,
                    provider,model,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (product_id, source_image_id, reference, preset, instruction, prompt,
                 provider.name, provider.model, "generating", _now()),
            )
            generation_id = int(cursor.lastrowid)
            connection.commit()
        try:
            source, filename, content_type = _read_source(reference)
            result = provider.edit(source, filename, content_type, prompt)
            PENDING_DIR.mkdir(parents=True, exist_ok=True)
            relative_path = f"/static/generated-images/pending/{generation_id}-{uuid4().hex}{result.extension}"
            output_path = APP_DIR / relative_path.lstrip("/")
            output_path.write_bytes(result.image_bytes)
            with _connect() as connection:
                connection.execute(
                    "UPDATE image_studio_generations SET status='pending', generated_image_path=? WHERE generation_id=?",
                    (relative_path, generation_id),
                )
                connection.commit()
            return _redirect(product_id, message="Image generated. Review it carefully before approval.")
        except Exception as exc:
            safe_error = str(exc)[:500] or "Image generation failed."
            with _connect() as connection:
                connection.execute(
                    "UPDATE image_studio_generations SET status='failed', error_message=?, reviewed_at=? WHERE generation_id=?",
                    (safe_error, _now(), generation_id),
                )
                connection.commit()
            return _redirect(product_id, error=safe_error)

    @app.post("/images/studio/{generation_id}/approve")
    def image_studio_approve(generation_id: int, save_as_primary: bool = Form(False)):
        with _connect() as connection:
            _ensure_schema(connection)
            row = _generation(connection, generation_id)
            if row["status"] != "pending" or not row["generated_image_path"]:
                return _redirect(row["product_id"], error="Only pending images can be approved.")
            source_path = _local_static_path(row["generated_image_path"])
            if source_path is None or not source_path.is_file() or source_path.parent != PENDING_DIR.resolve():
                return _redirect(row["product_id"], error="The generated preview file is unavailable.")
            APPROVED_DIR.mkdir(parents=True, exist_ok=True)
            destination = APPROVED_DIR / f"product-{row['product_id']}-{generation_id}{source_path.suffix.lower()}"
            destination.write_bytes(source_path.read_bytes())
            gallery_path = f"/static/product-images/ai-studio/{destination.name}"
            try:
                if save_as_primary:
                    connection.execute("UPDATE product_images SET is_primary=0 WHERE product_id=?", (row["product_id"],))
                cursor = connection.execute(
                    """INSERT INTO product_images
                       (product_id,image_path,image_url,image_type,is_primary,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (row["product_id"], gallery_path, None, "ai_generated", int(save_as_primary), _now()),
                )
                connection.execute(
                    """UPDATE image_studio_generations SET status='approved', approved_product_image_id=?,
                       save_as_primary=?, reviewed_at=? WHERE generation_id=?""",
                    (cursor.lastrowid, int(save_as_primary), _now(), generation_id),
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
            source_path = _local_static_path(row["generated_image_path"] or "")
            if source_path is not None and source_path.parent == PENDING_DIR.resolve():
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
        local = _local_static_path(row["source_image_reference"])
        if local is None or not local.is_file():
            raise HTTPException(status_code=404, detail="Local source image not found")
        return FileResponse(local)
