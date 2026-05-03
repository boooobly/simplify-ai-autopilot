"""Helpers for storing and parsing single and grouped Telegram media."""

from __future__ import annotations

import json

ALLOWED_MEDIA_TYPES = {"photo", "video", "animation"}


def is_media_group(media_type: str | None) -> bool:
    return media_type == "media_group"


def encode_media_group(items: list[dict]) -> str:
    normalized: list[dict[str, str]] = []
    for item in items:
        media_type = str(item.get("type") or "").strip()
        file_id = str(item.get("file_id") or "").strip()
        if media_type in ALLOWED_MEDIA_TYPES and file_id:
            normalized.append({"type": media_type, "file_id": file_id})
    return json.dumps(normalized, ensure_ascii=False)


def decode_media_items(media_url: str | None, media_type: str | None) -> list[dict[str, str]]:
    if not media_url or not media_type:
        return []
    if media_type in ALLOWED_MEDIA_TYPES:
        return [{"type": media_type, "file_id": media_url}]
    if not is_media_group(media_type):
        return []
    try:
        parsed = json.loads(media_url)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        file_id = str(item.get("file_id") or "").strip()
        if item_type in ALLOWED_MEDIA_TYPES and file_id:
            items.append({"type": item_type, "file_id": file_id})
    return items


def media_count(media_url: str | None, media_type: str | None) -> int:
    return len(decode_media_items(media_url, media_type))
