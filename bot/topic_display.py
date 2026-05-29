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



_SOURCE_GROUP_LABELS_RU = {
    "official_ai": "официального AI-блога",
    "tech_media": "техно-медиа",
    "ru_tech": "русского техно-медиа",
    "tools": "каталога AI-инструментов",
    "community": "сообщества",
    "github": "GitHub",
    "x": "X/Twitter",
    "custom": "добавленного источника",
    "telegram": "Telegram-канала",
    "other": "источника",
}

_CATEGORY_LABELS_RU = {
    "agent": "AI-агенты",
    "creator": "инструменты для авторов",
    "dev": "разработка",
    "drama": "обсуждение/драма",
    "fun": "развлекательная AI-тема",
    "guide": "практический гайд",
    "meme": "мем/сообщество",
    "mobile": "мобильные AI-инструменты",
    "model": "AI-модели",
    "news": "AI-новость",
    "research": "исследование",
    "tool": "AI-инструмент",
    "video": "видео/демо",
    "other": "AI-тема",
}

_GENERIC_METADATA_FALLBACK_RU = "Нужен ручной просмотр: не удалось нормально обработать тему."


def _shorten_sentence(value: object, max_len: int = 220) -> str:
    text = " ".join(_clean_text(value).split())
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)].rstrip(" .,;:") + "…"


def _domain_from_url(url: object) -> str:
    raw = _clean_text(url)
    if not raw:
        return ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(raw).netloc or "").lower()
    except Exception:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _source_context_ru(source: str, source_group: str) -> str:
    group_label = _SOURCE_GROUP_LABELS_RU.get(source_group or "other", "источника")
    if source:
        if source_group == "telegram":
            return f"Telegram-канала {source}"
        if source_group == "github":
            return source
        return f"{group_label} {source}"
    return group_label


def _repo_name_from_topic(title: str, url: str) -> str:
    clean_title = re.sub(r"(?i)^github\s+trending\s*:\s*", "", _clean_text(title)).strip()
    if clean_title:
        return clean_title
    domain = _domain_from_url(url)
    if domain == "github.com":
        path = _clean_text(url).split("github.com/", 1)[-1].strip("/")
        parts = [p for p in path.split("/") if p][:2]
        if parts:
            return " / ".join(parts)
    return "GitHub-репозиторий"


def _topic_kind_ru(category: str, source_group: str) -> str:
    if source_group == "github":
        return "open-source AI-проект"
    if source_group == "tools" or category in {"tool", "creator", "video"}:
        return "AI-инструмент"
    if category == "model":
        return "обновление AI-модели"
    if category == "agent":
        return "AI-агентов"
    if category == "dev":
        return "AI-разработку"
    if category == "research":
        return "AI-исследование"
    return "AI-новость"


def _english_topic_hint_ru(title: str, description: str, category: str, source_group: str) -> str:
    haystack = f"{title} {description}".casefold()
    hints: list[str] = []
    if any(word in haystack for word in ("shorts", "reels", "tiktok", "video cutter", "viral")):
        hints.append("короткие видео для Shorts, Reels или TikTok")
    if any(word in haystack for word in ("siri", "ios", "iphone", "apple")):
        hints.append("обновления Apple и AI-функции в iPhone")
    if any(word in haystack for word in ("agentic coding", "coding agent", "code agent", "ai agent", "developer", "terminal")):
        hints.append("AI-агента для программирования")
    if any(word in haystack for word in ("benchmark", "beats", "opus", "claude", "gpt", "gemini")):
        hints.append("сравнение AI-моделей и бенчмарки")
    if any(word in haystack for word in ("render", "redesign", "interface", "ui")):
        hints.append("новый интерфейс или редизайн AI-функции")
    if any(word in haystack for word in ("repo", "repository", "github", "open-source", "open source")):
        hints.append("open-source проект")
    if hints:
        return ", ".join(dict.fromkeys(hints[:2]))
    return _topic_kind_ru(category, source_group)


def _safe_original_title_quote(title: str, fallback: str) -> str:
    original = _shorten_sentence(title or fallback, 110).strip('"“”')
    return f'"{original}"' if original else "эту тему"


def build_deterministic_topic_metadata_ru(topic: object) -> dict[str, str]:
    """Build topic-specific Russian display metadata without network or AI calls.

    The fallback is intentionally conservative: it reuses only already collected
    title/source/description/scoring fields and asks the admin to verify details
    when the source text is not enough for a factual Russian summary.
    """
    title = _clean_text(_topic_value(topic, "title"))
    source = _clean_text(_topic_value(topic, "source"))
    source_group = _clean_text(_topic_value(topic, "source_group")) or "other"
    raw_description = _clean_text(_topic_value(topic, "original_description"))
    description = _shorten_sentence(raw_description, 180)
    category = _clean_text(_topic_value(topic, "category")) or "other"
    score_raw = _topic_value(topic, "score")
    try:
        score = int(score_raw or 0)
    except (TypeError, ValueError):
        score = 0
    reason_ru_existing = _clean_text(_topic_value(topic, "reason_ru"))
    reason = _clean_text(_topic_value(topic, "reason"))
    url = _clean_text(_topic_value(topic, "url"))
    domain = _domain_from_url(url)
    stars_today = _clean_text(_topic_value(topic, "stars_today"))
    category_ru = _CATEGORY_LABELS_RU.get(category, category or "AI-тема")
    source_context = _source_context_ru(source, source_group)
    source_name = source or domain or "источник"
    text_is_english = _mostly_english_text(" ".join(part for part in (title, raw_description) if part))
    english_hint = _english_topic_hint_ru(title, raw_description, category, source_group)
    original_quote = _safe_original_title_quote(title, description or source_name)

    if not any([title, source, description]):
        return {
            "title_ru": "Тема без названия: нужен ручной просмотр",
            "summary_ru": _GENERIC_METADATA_FALLBACK_RU,
            "angle_ru": "Нет названия, источника и описания — тему можно оценить только после ручной проверки.",
            "reason_ru": reason_ru_existing or "Недостаточно данных для нормальной оценки темы.",
        }

    title_for_display = title or description or source or domain or "тема без названия"
    title_short = _shorten_sentence(title_for_display, 120)

    if source_group == "github" or domain == "github.com":
        repo = _repo_name_from_topic(title, url)
        title_ru = _shorten_sentence(f"GitHub-репозиторий: {repo}", 120)
        star_text = f" Есть сигнал GitHub Trending: {stars_today} звезд сегодня." if stars_today else ""
        if text_is_english:
            summary_ru = _shorten_sentence(
                f"Источник {source_name} пишет про репозиторий {original_quote}. Нужна проверка README и деталей, но тема может подойти как новость про {english_hint}.{star_text}",
                300,
            )
        else:
            detail_text = description or "описание и метрики нужно проверить по ссылке"
            summary_ru = _shorten_sentence(
                f"GitHub-репозиторий {repo}: {detail_text}. Подойдет как тема про новый инструмент или open-source проект, если после проверки он реально полезен аудитории.{star_text}",
                300,
            )
        angle_ru = "Проверить README, демо и пользу: можно сделать короткий пост о том, какую задачу закрывает репозиторий и кому он пригодится."
    elif source_group == "telegram":
        title_ru = _shorten_sentence(f"Пост из Telegram: {title_short}", 120)
        if text_is_english:
            summary_ru = _shorten_sentence(
                f"Источник {source_name} пишет про тему: {original_quote}. Нужна проверка деталей, но тема может подойти как новость про {english_hint}.",
                260,
            )
        else:
            summary_ru = _shorten_sentence(
                f"Пост из {source_context}: {description or title_short}. Можно использовать как сигнал, но перед публикацией лучше проверить первоисточник и факты.",
                300,
            )
        angle_ru = "Взять как повод для поста только после проверки первоисточника: объяснить, что произошло и почему это важно обычному читателю."
    elif source_group == "tools" or "product hunt" in source.casefold():
        title_ru = _shorten_sentence(f"Новый AI-инструмент: {title_short}", 120)
        if text_is_english:
            summary_ru = _shorten_sentence(
                f"Источник {source_name} пишет про инструмент {original_quote}. Нужна проверка деталей, но тема может подойти как новость про {english_hint}.",
                260,
            )
        else:
            summary_ru = _shorten_sentence(
                f"Новый AI-инструмент из {source_context}: {description or title_short}. Нужно проверить сайт и понять, есть ли практическая польза для аудитории.",
                300,
            )
        angle_ru = "Проверить продукт, цену и реальный сценарий использования; если польза есть — показать простыми словами, какую задачу он решает."
    elif source_group in {"official_ai", "tech_media", "ru_tech"}:
        title_ru = _shorten_sentence(f"Новость от {source or domain or 'источника'}: {title_short}", 120)
        if text_is_english:
            summary_ru = _shorten_sentence(
                f"Источник {source_name} пишет про тему: {original_quote}. Нужна проверка деталей, но тема может подойти как новость про {english_hint}.",
                260,
            )
        else:
            summary_ru = _shorten_sentence(
                f"Источник {source or domain or 'новости'} сообщает: {description or title_short}. Стоит проверить детали и сделать короткий пост про пользу для обычных пользователей.",
                300,
            )
        angle_ru = "Сфокусироваться не на пресс-релизе, а на практическом выводе: что меняется для пользователя, автора или разработчика."
    else:
        title_ru = _shorten_sentence(f"Тема из {source or domain or 'источника'}: {title_short}", 120)
        if text_is_english:
            summary_ru = _shorten_sentence(
                f"Источник {source_name} пишет про тему: {original_quote}. Нужна проверка деталей, но тема может подойти как новость про {english_hint}.",
                260,
            )
        else:
            summary_ru = _shorten_sentence(
                f"Источник {source_context} дает тему: {description or title_short}. Перед публикацией нужно проверить детали, но уже видно, о каком сюжете речь.",
                300,
            )
        angle_ru = "Проверить первоисточник и выбрать простой пользовательский вывод: зачем аудитории знать об этой теме сейчас."

    score_part = f"скоринг {score}/100" if score else "скоринг не указан"
    if reason_ru_existing and "Нужен ручной просмотр" not in reason_ru_existing:
        reason_ru = reason_ru_existing
    elif reason:
        reason_ru = _shorten_sentence(f"Категория: {category_ru}; {score_part}. Сигналы скоринга: {reason}.", 220)
    else:
        reason_ru = _shorten_sentence(f"Категория: {category_ru}; {score_part}. Источник: {source or domain or source_group}.", 220)

    return {
        "title_ru": title_ru,
        "summary_ru": summary_ru,
        "angle_ru": angle_ru,
        "reason_ru": reason_ru,
    }


def _has_ai_metadata(topic: object) -> bool:
    value = _topic_value(topic, "ai_value_score")
    if value is None or value == "":
        return False
    try:
        score = int(str(value).strip())
    except (TypeError, ValueError):
        return False
    return 0 <= score <= 100

def topic_display_title(topic: object) -> str:
    """Return the Russian display title when available, otherwise deterministic wrapper."""
    explicit = _clean_text(_topic_value(topic, "title_ru"))
    original = _clean_text(_topic_value(topic, "title"))
    if explicit and (_has_ai_metadata(topic) or not is_weak_topic_metadata(explicit, _topic_value(topic, "summary_ru"), _topic_value(topic, "angle_ru"), original_title=original)):
        return explicit
    metadata = build_deterministic_topic_metadata_ru(topic)
    return metadata.get("title_ru") or explicit or original or "Без названия"


def topic_display_reason(topic: object) -> str:
    """Return the Russian display reason when available, otherwise deterministic scoring context."""
    explicit = _clean_text(_topic_value(topic, "reason_ru"))
    if explicit and (_has_ai_metadata(topic) or "Нужен ручной просмотр" not in explicit):
        return explicit
    metadata = build_deterministic_topic_metadata_ru(topic)
    return metadata.get("reason_ru") or explicit or _clean_text(_topic_value(topic, "reason")) or "без пояснения"


MANUAL_REVIEW_NOTE_RU = "Нужен ручной просмотр: не удалось перевести тему"


def topic_summary_ru(topic: object) -> str:
    """Return topic-specific Russian summary with deterministic fallback."""
    explicit = _clean_text(_topic_value(topic, "summary_ru"))
    if explicit and (_has_ai_metadata(topic) or "Нужен ручной просмотр" not in explicit):
        return explicit
    metadata = build_deterministic_topic_metadata_ru(topic)
    return metadata.get("summary_ru") or explicit or MANUAL_REVIEW_NOTE_RU


def topic_angle_ru(topic: object) -> str:
    """Return Russian post-angle suggestion with deterministic fallback."""
    explicit = _clean_text(_topic_value(topic, "angle_ru"))
    if explicit and (_has_ai_metadata(topic) or ("AI-обогащение не дало" not in explicit and "проверь тему вручную" not in explicit)):
        return explicit
    metadata = build_deterministic_topic_metadata_ru(topic)
    return metadata.get("angle_ru") or explicit or "Сначала открой источник и вручную проверь смысл темы: AI-обогащение не дало понятный русский ракурс."


def _shorten_text(value: str, max_len: int) -> str:
    text = " ".join(_clean_text(value).split())
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)].rstrip() + "…"


def topic_compact_preview_ru(topic: object, max_len: int = 160) -> str:
    """Return a compact Russian preview for collection summaries."""
    title = topic_display_title(topic)
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
