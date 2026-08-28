"""Validated local navigation helpers for cross-tool product workflows."""

from __future__ import annotations

from urllib.parse import urlsplit


ALLOWED_WORKFLOW_PATHS = (
    "/channels/publish",
    "/images/studio",
    "/smart-scan",
)


def safe_return_to(value: str | None, default: str = "/channels/publish") -> str:
    candidate = str(value or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return default
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return default
    if not any(parsed.path == prefix or parsed.path.startswith(prefix + "/") for prefix in ALLOWED_WORKFLOW_PATHS):
        return default
    return candidate
