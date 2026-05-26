"""Normalization helpers for managed sources."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_telegram_channel_input(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("@"):
        value = value[1:].strip()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").strip("/")
    if host in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        parts = [part for part in path.split("/") if part]
    elif not host and "/" not in value:
        parts = [value]
    elif not host and value.lower().startswith("t.me/"):
        parts = [part for part in value[5:].strip("/").split("/") if part]
    else:
        parts = [part for part in value.strip("/").split("/") if part]
    if not parts:
        return ""
    first = parts[0].lower()
    if first in {"joinchat", "c"} or first.startswith("+"):
        return ""
    if first == "s":
        if len(parts) < 2:
            return ""
        first = parts[1].lower()
    return first.lstrip("@").strip()


def normalize_source_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/") or ("/" if parsed.path == "/" else "")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.params, parsed.query, ""))
