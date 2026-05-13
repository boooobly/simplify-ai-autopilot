"""Display helpers for topic candidates in admin-facing UI."""

from __future__ import annotations

import re


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _topic_value(topic: object, key: str) -> object:
    if isinstance(topic, dict):
        return topic.get(key)
    return getattr(topic, key, None)


def _contains_cyrillic(value: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in value or "")


def _letter_counts(value: str) -> tuple[int, int]:
    cyrillic = 0
    latin = 0
    for ch in value or "":
        lower = ch.lower()
        if "а" <= lower <= "я" or lower == "ё":
            cyrillic += 1
        elif "a" <= lower <= "z":
            latin += 1
    return cyrillic, latin


def _mostly_english_text(value: str) -> bool:
    cyrillic, latin = _letter_counts(value)
    if latin < 12:
        return False
    if cyrillic == 0:
        return True
    return cyrillic / max(1, cyrillic + latin) < 0.35 and latin > cyrillic * 1.8


def _has_untranslated_source_after_russian_label(title_ru: str) -> bool:
    text = _clean_text(title_ru)
    label_patterns = [
        r"(?i)(?:^|[-:—])\s*(?:open-source|opensource)\s+проект\s*:\s*[a-z]",
        r"(?i)(?:^|[-:—])\s*проект\s+(?:about|for)\b",
        r"(?i)(?:^|[-:—])\s*(?:новость|релиз|проект|инструмент)\s*:\s*(?:[a-z][a-z ]{8,})",
    ]
    return any(re.search(pattern, text) for pattern in label_patterns)


def is_weak_topic_metadata(
    title_ru: str | None,
    summary_ru: str | None,
    angle_ru: str | None,
    original_title: str | None = None,
) -> bool:
    """Return True when admin-facing topic metadata is empty, generic or mostly untranslated."""
    title = _clean_text(title_ru)
    summary = _clean_text(summary_ru)
    angle = _clean_text(angle_ru)
    original = _clean_text(original_title)

    if not title:
        return True
    if original and title.casefold() == original.casefold():
        return True
    if not _contains_cyrillic(title):
        return True
    if _mostly_english_text(title):
        return True

    bad_title_patterns = [
        r"(?i)open-source\s+проект:\s*(?:implement|build|create|learn|launch|release|introducing)",
        r"(?i)\bproject\s+about\b",
        r"(?i)\bbased\s+on\s+real-world\s+benchmarks\b",
        r"(?i)\bfor\s+magnifying\s+HUMAN\s+capabilities\b",
        r"(?i)\bPersistent\s+memory\s+for\b",
        r"(?i)\bAgentic\s+AI\s+Infrastructure\b",
        r"(?i)GitHub-проект,\s*нужен\s+AI-перевод",
        r"(?i)GitHub-проект\s+по\s+AI/разработке",
    ]
    if any(re.search(pattern, title) for pattern in bad_title_patterns):
        return True
    if _has_untranslated_source_after_russian_label(title):
        return True

    generic_summary_fragments = [
        "Репозиторий выглядит как проект про",
        "Источник предлагает новость по AI",
        "Нужен ручной просмотр",
    ]
    if any(fragment.casefold() in summary.casefold() for fragment in generic_summary_fragments):
        return True

    if angle and not _contains_cyrillic(angle) and len(angle) > 20:
        return True
    return False


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
