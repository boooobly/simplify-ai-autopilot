"""Display helpers for topic candidates in admin-facing UI."""

from __future__ import annotations


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _topic_value(topic: object, key: str) -> object:
    if isinstance(topic, dict):
        return topic.get(key)
    return getattr(topic, key, None)


def topic_display_title(topic: object) -> str:
    """Return the Russian display title when available, otherwise original title."""
    return _clean_text(_topic_value(topic, "title_ru")) or _clean_text(_topic_value(topic, "title")) or "Без названия"


def topic_display_reason(topic: object) -> str:
    """Return the Russian display reason when available, otherwise original reason."""
    return _clean_text(_topic_value(topic, "reason_ru")) or _clean_text(_topic_value(topic, "reason")) or "без пояснения"


def topic_original_title_line(topic: object) -> str | None:
    """Return a compact original-title line when Russian display differs."""
    original = _clean_text(_topic_value(topic, "title"))
    display = topic_display_title(topic)
    if original and original != display:
        return f"Оригинал: {original}"
    return None
