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


def topic_summary_ru(topic: object) -> str:
    """Return short Russian summary with safe fallback."""
    explicit = _clean_text(_topic_value(topic, "summary_ru"))
    if explicit:
        return explicit
    source_group = _clean_text(_topic_value(topic, "source_group")).lower()
    category = _clean_text(_topic_value(topic, "category")).lower()
    description = _clean_text(_topic_value(topic, "original_description"))
    if description:
        return f"Источник описывает тему так: {description[:220]}"
    if source_group == "github":
        return "Похоже на GitHub-проект по AI/разработке. Лучше открыть ссылку и быстро проверить, есть ли там понятная польза для поста."
    if source_group in {"official_ai", "tech_media", "ru_tech"} or category == "news":
        return "Источник предлагает новость по AI. Перед созданием поста стоит открыть ссылку и проверить детали."
    if source_group == "community":
        return "Тема пришла из сообщества: стоит проверить обсуждение и понять, есть ли там живой инсайт для подписчиков."
    if category in {"tool", "guide", "creator", "mobile"}:
        return "Похоже на практическую тему: перед постом стоит проверить, что именно можно показать читателю."
    return "Тема выглядит релевантной AI-повестке, но перед созданием поста стоит открыть источник и уточнить детали."


def topic_angle_ru(topic: object) -> str:
    """Return Russian post-angle suggestion with safe fallback."""
    explicit = _clean_text(_topic_value(topic, "angle_ru"))
    if explicit:
        return explicit
    source_group = _clean_text(_topic_value(topic, "source_group")).lower()
    category = _clean_text(_topic_value(topic, "category")).lower()
    if source_group == "github":
        return "Можно подать как короткий разбор: что делает проект, кому он полезен и почему на него обратили внимание."
    if category == "drama":
        return "Можно объяснить конфликт простыми словами и вынести практический вывод для пользователей AI."
    if category == "tool":
        return "Можно сделать пост в формате: что это за инструмент, чем полезен и кому стоит попробовать."
    if category == "model":
        return "Можно сравнить новую возможность с тем, что уже было, и объяснить, что меняется для обычного пользователя."
    return "Можно сделать короткий пост с объяснением сути темы и одним выводом для аудитории @simplify_ai."


def topic_original_title_line(topic: object) -> str | None:
    """Return a compact original-title line when Russian display differs."""
    original = _clean_text(_topic_value(topic, "title"))
    display = topic_display_title(topic)
    if original and original != display:
        return f"Оригинал: {original}"
    return None
