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


MANUAL_REVIEW_NOTE_RU = "Нужен ручной просмотр: не удалось перевести тему"


def topic_summary_ru(topic: object) -> str:
    """Return short Russian summary with explicit manual-review fallback."""
    explicit = _clean_text(_topic_value(topic, "summary_ru"))
    if explicit:
        return explicit
    return MANUAL_REVIEW_NOTE_RU


def topic_angle_ru(topic: object) -> str:
    """Return Russian post-angle suggestion with explicit manual-review fallback."""
    explicit = _clean_text(_topic_value(topic, "angle_ru"))
    if explicit:
        return explicit
    return "Сначала открой источник и вручную проверь смысл темы: AI-обогащение не дало понятный русский ракурс."


def _shorten_text(value: str, max_len: int) -> str:
    text = " ".join(_clean_text(value).split())
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)].rstrip() + "…"


def topic_compact_preview_ru(topic: object, max_len: int = 160) -> str:
    """Return a compact Russian preview for collection summaries."""
    title_ru = _clean_text(_topic_value(topic, "title_ru"))
    original = _clean_text(_topic_value(topic, "title"))
    title = title_ru or (f"Нужна проверка: {original}" if original else "Нужна проверка: без названия")
    summary = topic_summary_ru(topic)
    return f"{_shorten_text(title, 90)}\n  О чем: {_shorten_text(summary, max_len)}"


def _split_related_values(value: object) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for raw in _clean_text(value).split("\n"):
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            values.append(item)
    return values


def related_sources_summary(topic: object, limit: int = 3) -> str | None:
    """Return compact Russian summary of related sources for a topic card."""
    try:
        related_count = int(_topic_value(topic, "related_count") or 1)
    except (TypeError, ValueError):
        related_count = 1
    if related_count <= 1:
        return None
    source = _clean_text(_topic_value(topic, "source"))
    sources = [s for s in _split_related_values(_topic_value(topic, "related_sources")) if s != source]
    lines = [f"Повторы: еще {max(0, related_count - 1)} источника"]
    if sources:
        shown = sources[: max(1, limit)]
        suffix = "" if len(sources) <= len(shown) else f" и еще {len(sources) - len(shown)}"
        lines.append(f"Также встречалось: {', '.join(shown)}{suffix}")
    return "\n".join(lines)


def topic_original_title_line(topic: object) -> str | None:
    """Return a compact original-title line when Russian display differs."""
    original = _clean_text(_topic_value(topic, "title"))
    display = topic_display_title(topic)
    if original and original != display:
        return f"Оригинал: {original}"
    return None
