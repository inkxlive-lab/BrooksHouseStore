from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database_resolution import configured_sqlite_path, require_application_database_match


APP_ROOT = Path(__file__).resolve().parent
DB_PATH = configured_sqlite_path()
PROFILE_PHOTO_DIRECTORY = APP_ROOT / "data" / "team-profile-photos"
ENV_PATH = APP_ROOT.parent / ".env"
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
router = APIRouter()

ROLE_LABELS = {
    "owner_admin": "Owner / Admin",
    "manager": "Manager",
    "store_worker": "Store Helper",
    "view_only": "View Only",
}
SESSION_HOURS = 12
IDLE_MINUTES = 120
COOKIE_NAME = "brookshouse_session"


@dataclass
class AuthUser:
    user_id: int
    username: str
    display_name: str
    role: str
    session_id: int
    profile_image_name: Optional[str] = None

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()


def _connect() -> sqlite3.Connection:
    database = require_application_database_match(DB_PATH)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _initialize() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS app_user_sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                revoked_at TEXT,
                FOREIGN KEY(user_id) REFERENCES app_users(user_id)
            );
            CREATE TABLE IF NOT EXISTS app_access_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                event_type TEXT NOT NULL,
                path TEXT,
                method TEXT,
                details TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_app_sessions_token ON app_user_sessions(token_hash);
            CREATE INDEX IF NOT EXISTS ix_app_audit_created ON app_access_audit(created_at);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(app_users)")}
        if "profile_image_name" not in columns:
            connection.execute("ALTER TABLE app_users ADD COLUMN profile_image_name TEXT")
        transaction_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(inventory_transactions)"
            )
        }
        if transaction_columns:
            if "performed_by_user_id" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions "
                    "ADD COLUMN performed_by_user_id INTEGER"
                )
            if "performed_by_name" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions "
                    "ADD COLUMN performed_by_name TEXT"
                )
            if "performed_by_role" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE inventory_transactions "
                    "ADD COLUMN performed_by_role TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS "
                "ix_inventory_transactions_performed_by_user_id "
                "ON inventory_transactions(performed_by_user_id)"
            )
    PROFILE_PHOTO_DIRECTORY.mkdir(parents=True, exist_ok=True)


def hash_password(password: str) -> str:
    if len(password) < 4 or len(password) > 10:
        raise ValueError("Password must contain between 4 and 10 characters.")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


_DUMMY_HASH = "scrypt$16384$8$1$000102030405060708090a0b0c0d0e0f$67e6096a5feca6ad33c1859ee48ee0b62be5ab5e27b4a9e2bd3e99f42fdf18ef"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt, wanted = stored.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=int(n), r=int(r), p=int(p), dklen=32).hex()
        return hmac.compare_digest(actual, wanted)
    except Exception:
        return False


def normalize_username(username: str) -> str:
    return str(username or "").strip().lower()[:100]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ip(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    return (forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else ""))[:80]


def _audit(connection: sqlite3.Connection, request: Request, event: str,
           user: Optional[AuthUser] = None, username: str = "", details: str = "") -> None:
    connection.execute(
        """INSERT INTO app_access_audit
           (user_id, username, event_type, path, method, details, ip_address)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user.user_id if user else None, user.username if user else username,
         event, request.url.path, request.method, details[:500], _ip(request)),
    )


def _current_user(request: Request) -> Optional[AuthUser]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    now = _now()
    with _connect() as connection:
        row = connection.execute(
            """SELECT s.session_id, s.last_seen_at, s.expires_at, s.revoked_at,
                      u.user_id, u.username, u.display_name, u.role, u.active,
                      u.profile_image_name
               FROM app_user_sessions s JOIN app_users u ON u.user_id = s.user_id
               WHERE s.token_hash = ? LIMIT 1""",
            (_token_hash(token),),
        ).fetchone()
        if not row or row["revoked_at"] or not row["active"]:
            return None
        if _parse(row["expires_at"]) <= now or _parse(row["last_seen_at"]) + timedelta(minutes=IDLE_MINUTES) <= now:
            connection.execute("UPDATE app_user_sessions SET revoked_at = ? WHERE session_id = ?",
                               (_iso(now), row["session_id"]))
            return None
        connection.execute("UPDATE app_user_sessions SET last_seen_at = ? WHERE session_id = ?",
                           (_iso(now), row["session_id"]))
        return AuthUser(row["user_id"], row["username"], row["display_name"], row["role"],
                        row["session_id"], row["profile_image_name"])


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")


def _safe_origin(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        return urlparse(origin).hostname == request.url.hostname
    except Exception:
        return False


def _permission(user: AuthUser, request: Request) -> bool:
    path = request.url.path
    method = request.method
    if path.startswith("/profile"):
        return True
    if user.role == "owner_admin":
        return True

    owner_only = (
        path.startswith("/access"), path.startswith("/tools/sql"), path.startswith("/tools/python"),
        path.startswith("/channels/shopify/settings"), path.startswith("/channels/shopify/push-preview"),
        path.startswith("/channels/shopify/approve"), path.startswith("/channels/shopify/storefront-import"),
        path.startswith("/channels/amazon/mapping"), path.startswith("/channels/amazon/link"),
        path.startswith("/channels/amazon/unlink"), path.startswith("/tools/notifications/settings"),
    )
    if any(owner_only):
        return False

    if user.role == "manager":
        return True

    if user.role == "store_worker":
        if path.startswith("/products/add"):
            return False
        if path.startswith("/api/products") and method not in {"GET", "HEAD"}:
            return False
        prefixes = (
            "/role-home", "/smart-scan", "/scan", "/inventory/search", "/inventory/receive",
            "/inventory/tote-repair", "/inventory/tote-audit", "/inventory/adjust/batch",
            "/offline", "/api/offline", "/storage-gallery", "/store-map", "/api/store-map",
            "/channels/orders/pull-list", "/channels/walmart/orders/pull-list",
            "/channels/amazon/orders/pull-list", "/api/smart-scan", "/api/barcodes/",
            "/api/locations", "/api/products", "/products/",
        )
        if path.startswith(prefixes):
            return True
        if path.startswith("/channels/walmart/orders/lines/") and path.endswith("/pull"):
            return True
        if path.startswith("/channels/walmart/orders/") and "/stage/picked" in path:
            return True
        return False

    if user.role == "view_only":
        if method not in {"GET", "HEAD"}:
            return False
        if path.startswith("/products/add"):
            return False
        prefixes = (
            "/dashboard", "/inventory/search", "/inventory/search/export", "/channels/stats",
            "/products", "/channels/orders", "/channels/walmart/orders", "/channels/amazon/orders",
            "/api/dashboard/", "/api/products", "/api/locations",
            "/api/barcodes/", "/storage-gallery", "/offline", "/api/offline/snapshot",
            "/store-map", "/api/store-map",
        )
        return path.startswith(prefixes)
    return False


def _home(user: AuthUser) -> str:
    return "/role-home" if user.role == "store_worker" else "/dashboard"


def _render(request: Request, name: str, **context):
    return templates.TemplateResponse(request=request, name=name,
                                      context={"request": request, "user": getattr(request.state, "auth_user", None),
                                               "roles": ROLE_LABELS, **context})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if getattr(request.state, "auth_user", None):
        return RedirectResponse(_home(request.state.auth_user), status_code=303)
    return _render(request, "access_login.html")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    clean = normalize_username(username)
    now = _now()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM app_users WHERE username = ?", (clean,)).fetchone()
        stored = row["password_hash"] if row else _DUMMY_HASH
        password_ok = verify_password(password, stored)
        locked = bool(row and row["locked_until"] and _parse(row["locked_until"]) > now)
        if not row or not row["active"] or locked or not password_ok:
            if row and not locked:
                attempts = row["failed_attempts"] + 1
                lock_until = _iso(now + timedelta(minutes=15)) if attempts >= 5 else None
                connection.execute("UPDATE app_users SET failed_attempts = ?, locked_until = ? WHERE user_id = ?",
                                   (0 if lock_until else attempts, lock_until, row["user_id"]))
            _audit(connection, request, "login_failed", username=clean,
                   details="Account unavailable, locked, or password incorrect")
            time.sleep(0.35)
            return _render(request, "access_login.html", error="Login was not accepted. Check the username and password, or wait 15 minutes after repeated attempts.")
        token = secrets.token_urlsafe(48)
        expires = now + timedelta(hours=SESSION_HOURS)
        connection.execute("""INSERT INTO app_user_sessions
                              (user_id, token_hash, expires_at, ip_address, user_agent, last_seen_at)
                              VALUES (?, ?, ?, ?, ?, ?)""",
                           (row["user_id"], _token_hash(token), _iso(expires), _ip(request),
                            request.headers.get("user-agent", "")[:300], _iso(now)))
        session_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("UPDATE app_users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE user_id = ?",
                           (_iso(now), row["user_id"]))
        user = AuthUser(row["user_id"], row["username"], row["display_name"], row["role"],
                        session_id, row["profile_image_name"])
        _audit(connection, request, "login_success", user=user)
    response = RedirectResponse(_home(user), status_code=303)
    host = request.url.hostname or ""
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS * 3600, httponly=True,
                        secure=host not in {"127.0.0.1", "localhost"} and not host.startswith("10."),
                        samesite="strict", path="/")
    return response


@router.post("/logout")
def logout(request: Request):
    user = getattr(request.state, "auth_user", None)
    if user:
        with _connect() as connection:
            connection.execute("UPDATE app_user_sessions SET revoked_at = ? WHERE session_id = ?",
                               (_iso(_now()), user.session_id))
            _audit(connection, request, "logout", user=user)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/role-home", response_class=HTMLResponse)
def role_home(request: Request):
    user = request.state.auth_user
    if user.role == "store_worker":
        with _connect() as connection:
            helper = connection.execute(
                "SELECT helper_id, helper_name FROM kids_helpers "
                "WHERE app_user_id=? AND active=1",
                (user.user_id,),
            ).fetchone()
            task_summary = None
            point_balance = 0
            if helper:
                task_summary = connection.execute(
                    """SELECT
                           SUM(CASE WHEN status='assigned' THEN 1 ELSE 0 END) assigned,
                           SUM(CASE WHEN status!='assigned' AND approval_status='pending' THEN 1 ELSE 0 END) waiting
                       FROM kids_helper_tasks WHERE helper_id=?""",
                    (helper["helper_id"],),
                ).fetchone()
                point_balance = connection.execute(
                    "SELECT COALESCE(SUM(points_change),0) FROM kids_points_ledger WHERE helper_id=?",
                    (helper["helper_id"],),
                ).fetchone()[0]
        return _render(
            request,
            "store_helper_home.html",
            helper=helper,
            task_summary=task_summary,
            point_balance=point_balance,
        )
    return _render(request, "access_role_home.html")


def _profile_photo_kind(data: bytes) -> Optional[tuple[str, str]]:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _safe_profile_path(name: Optional[str]) -> Optional[Path]:
    if not name or Path(name).name != name:
        return None
    candidate = PROFILE_PHOTO_DIRECTORY / name
    return candidate if candidate.is_file() else None


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    return _render(request, "team_profile.html")


@router.get("/profile-photo/{user_id}")
def profile_photo(request: Request, user_id: int):
    with _connect() as connection:
        row = connection.execute(
            "SELECT profile_image_name FROM app_users WHERE user_id=? AND active=1", (user_id,)
        ).fetchone()
    path = _safe_profile_path(row["profile_image_name"] if row else None)
    if not path:
        return JSONResponse({"detail": "Profile picture not found."}, status_code=404)
    media = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower())
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=300"})


@router.post("/profile/photo")
async def save_profile_photo(request: Request, photo: UploadFile = File(...)):
    user = request.state.auth_user
    data = await photo.read(8 * 1024 * 1024 + 1)
    await photo.close()
    if len(data) > 8 * 1024 * 1024:
        return RedirectResponse("/profile?error=Picture+must+be+8+MB+or+smaller", status_code=303)
    kind = _profile_photo_kind(data)
    if not kind:
        return RedirectResponse("/profile?error=Use+a+JPG,+PNG,+or+WebP+picture", status_code=303)
    extension, _ = kind
    new_name = f"user-{user.user_id}-{secrets.token_hex(12)}{extension}"
    new_path = PROFILE_PHOTO_DIRECTORY / new_name
    with new_path.open("xb") as output:
        output.write(data)
    old_path = None
    try:
        with _connect() as connection:
            row = connection.execute("SELECT profile_image_name FROM app_users WHERE user_id=?", (user.user_id,)).fetchone()
            old_path = _safe_profile_path(row["profile_image_name"] if row else None)
            connection.execute(
                "UPDATE app_users SET profile_image_name=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (new_name, user.user_id),
            )
            _audit(connection, request, "profile_picture_updated", user=user)
    except Exception:
        new_path.unlink(missing_ok=True)
        raise
    if old_path and old_path != new_path:
        old_path.unlink(missing_ok=True)
    user.profile_image_name = new_name
    return RedirectResponse("/profile?message=Profile+picture+saved", status_code=303)


@router.post("/profile/photo/delete")
def delete_profile_photo(request: Request):
    user = request.state.auth_user
    with _connect() as connection:
        row = connection.execute("SELECT profile_image_name FROM app_users WHERE user_id=?", (user.user_id,)).fetchone()
        old_path = _safe_profile_path(row["profile_image_name"] if row else None)
        connection.execute(
            "UPDATE app_users SET profile_image_name=NULL, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (user.user_id,),
        )
        _audit(connection, request, "profile_picture_deleted", user=user)
    if old_path:
        old_path.unlink(missing_ok=True)
    user.profile_image_name = None
    return RedirectResponse("/profile?message=Profile+picture+removed", status_code=303)


@router.get("/access", response_class=HTMLResponse)
def access_admin(request: Request):
    with _connect() as connection:
        users = connection.execute("""SELECT u.*, kh.helper_id, kh.helper_name,
                                    (SELECT COUNT(*) FROM app_user_sessions s WHERE s.user_id=u.user_id AND s.revoked_at IS NULL) active_sessions
                                    FROM app_users u
                                    LEFT JOIN kids_helpers kh ON kh.app_user_id=u.user_id
                                    ORDER BY u.display_name""").fetchall()
        helper_profiles = connection.execute(
            """SELECT h.helper_id, h.helper_name, h.app_user_id,
                      u.display_name AS linked_user_name
               FROM kids_helpers h
               LEFT JOIN app_users u ON u.user_id=h.app_user_id
               WHERE h.active=1 ORDER BY h.helper_name"""
        ).fetchall()
        audit = connection.execute("SELECT * FROM app_access_audit ORDER BY audit_id DESC LIMIT 100").fetchall()
    return _render(
        request,
        "access_admin.html",
        users=users,
        helper_profiles=helper_profiles,
        audit=audit,
    )


@router.post("/access/users/{user_id}/helper")
def link_helper_profile(
    request: Request,
    user_id: int,
    helper_id: int = Form(0),
):
    with _connect() as connection:
        user_row = connection.execute(
            "SELECT username, role FROM app_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not user_row:
            return RedirectResponse("/access?error=User+not+found", status_code=303)
        if user_row["role"] != "store_worker":
            return RedirectResponse(
                "/access?error=Only+Store+Helper+accounts+can+be+linked+to+a+helper+profile",
                status_code=303,
            )
        connection.execute(
            "UPDATE kids_helpers SET app_user_id=NULL WHERE app_user_id=?",
            (user_id,),
        )
        linked_name = "none"
        if helper_id:
            helper = connection.execute(
                "SELECT helper_name FROM kids_helpers WHERE helper_id=? AND active=1",
                (helper_id,),
            ).fetchone()
            if not helper:
                return RedirectResponse("/access?error=Helper+profile+not+found", status_code=303)
            connection.execute(
                "UPDATE kids_helpers SET app_user_id=NULL WHERE helper_id=?",
                (helper_id,),
            )
            connection.execute(
                "UPDATE kids_helpers SET app_user_id=? WHERE helper_id=?",
                (user_id, helper_id),
            )
            linked_name = helper["helper_name"]
        _audit(
            connection,
            request,
            "helper_profile_linked",
            user=request.state.auth_user,
            details=f"{user_row['username']} linked helper={linked_name}",
        )
    return RedirectResponse(
        "/access?message=Store+Helper+profile+connection+saved",
        status_code=303,
    )


@router.post("/access/users")
def create_user(request: Request, username: str = Form(...), display_name: str = Form(...),
                role: str = Form(...), password: str = Form(...)):
    if role not in ROLE_LABELS:
        return RedirectResponse("/access?error=Invalid+role", status_code=303)
    try:
        password_hash = hash_password(password)
        with _connect() as connection:
            connection.execute("INSERT INTO app_users(username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                               (normalize_username(username), display_name.strip()[:100], role, password_hash))
            _audit(connection, request, "user_created", user=request.state.auth_user,
                   details=f"Created {username.strip()} as {role}")
    except (ValueError, sqlite3.IntegrityError) as error:
        return RedirectResponse(f"/access?error={str(error).replace(' ', '+')}", status_code=303)
    return RedirectResponse("/access?message=User+created", status_code=303)


@router.post("/access/users/{user_id}/password")
def reset_password(request: Request, user_id: int, password: str = Form(...)):
    try:
        password_hash = hash_password(password)
        with _connect() as connection:
            row = connection.execute("SELECT username FROM app_users WHERE user_id=?", (user_id,)).fetchone()
            connection.execute("UPDATE app_users SET password_hash=?, failed_attempts=0, locked_until=NULL, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                               (password_hash, user_id))
            connection.execute("UPDATE app_user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                               (_iso(_now()), user_id))
            _audit(connection, request, "password_reset", user=request.state.auth_user,
                   details=f"Reset password for {row['username'] if row else user_id}")
    except ValueError as error:
        return RedirectResponse(f"/access?error={str(error).replace(' ', '+')}", status_code=303)
    return RedirectResponse("/access?message=Password+reset+and+sessions+ended", status_code=303)


@router.post("/access/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int):
    if user_id == request.state.auth_user.user_id:
        return RedirectResponse("/access?error=You+cannot+disable+your+own+account", status_code=303)
    with _connect() as connection:
        row = connection.execute("SELECT username, active FROM app_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            active = 0 if row["active"] else 1
            connection.execute("UPDATE app_users SET active=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (active, user_id))
            if not active:
                connection.execute("UPDATE app_user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                                   (_iso(_now()), user_id))
            _audit(connection, request, "user_status_changed", user=request.state.auth_user,
                   details=f"{row['username']} active={active}")
    return RedirectResponse("/access", status_code=303)


@router.post("/access/users/{user_id}/sessions/revoke")
def revoke_sessions(request: Request, user_id: int):
    with _connect() as connection:
        connection.execute("UPDATE app_user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                           (_iso(_now()), user_id))
        _audit(connection, request, "sessions_revoked", user=request.state.auth_user, details=f"user_id={user_id}")
    return RedirectResponse("/access", status_code=303)


def install_access_control(app) -> None:
    _initialize()
    app.include_router(router)

    @app.middleware("http")
    async def brookshouse_access_control(request: Request, call_next):
        from app.services.transaction_actor import (
            reset_transaction_actor,
            set_transaction_actor,
        )
        path = request.url.path
        public = (
            path in {"/login", "/api/health", "/notifications/service-worker.js", "/service-worker.js"}
            or path.startswith("/static/") or path.startswith("/kids")
        )
        if not _safe_origin(request):
            return JSONResponse({"detail": "Request origin was not accepted."}, status_code=403)
        user = _current_user(request)
        request.state.auth_user = user
        actor_token = set_transaction_actor(user)
        try:
            if public:
                return await call_next(request)
            if not user:
                if _is_api(request):
                    return JSONResponse({"detail": "Login required."}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            if not _permission(user, request):
                with _connect() as connection:
                    _audit(connection, request, "access_denied", user=user, details=f"role={user.role}")
                if _is_api(request):
                    return JSONResponse({"detail": "Your BrooksHouse role does not allow this action."}, status_code=403)
                return _render(request, "access_denied.html", attempted_path=path)
            return await call_next(request)
        finally:
            reset_transaction_actor(actor_token)
