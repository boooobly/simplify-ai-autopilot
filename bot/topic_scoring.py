from __future__ import annotations

import re
import string

_CLEAN_RE = re.compile(r"\s+")
_STOP_WORDS = {
    "the", "a", "an", "and", "for", "with", "new", "новый", "новая", "запустил", "запустила"
}

_SOURCE_GROUP_BOOST = {
    "official_ai": 0,
    "tech_media": 5,
    "ru_tech": 8,
    "tools": 15,
    "community": 18,
    "github": 15,
    "custom": 10,
}


def normalize_topic_title(title: str) -> str:
    lowered = title.lower()
    table = str.maketrans({ch: " " for ch in string.punctuation + "«»—–…"})
    cleaned = lowered.translate(table)
    words = [w for w in _CLEAN_RE.sub(" ", cleaned).strip().split(" ") if w and w not in _STOP_WORDS]
    return " ".join(words)


def score_topic(title: str, source: str, url: str, source_group: str = "other") -> tuple[int, str, str]:
    text = f"{title} {source} {url}".lower()
    score = 50 + _SOURCE_GROUP_BOOST.get(source_group, 0)
    reason_parts: list[str] = []

    category = "other"
    if any(k in text for k in ["drama", "fail", "bug", "ban", "leak", "lawsuit", "hack", "утеч", "бан", "баг"]):
        score += 30
        category = "drama"
        reason_parts.append("драма/проблема")
    elif any(k in text for k in ["guide", "tutorial", "prompt", "course", "курс", "гайд"]):
        score += 24
        category = "guide"
        reason_parts.append("практический гайд")
    elif any(k in text for k in ["creator", "image", "video", "voice", "avatar", "music", "design", "editor"]):
        score += 22
        category = "creator"
        reason_parts.append("креаторский формат")
    elif any(k in text for k in ["ios", "android", "mobile", "app", "chrome extension", "browser extension"]):
        score += 20
        category = "mobile"
        reason_parts.append("мобильный/расширение")
    elif any(k in text for k in ["tool", "service", "website", "platform", "api", "plugin", "extension", "app"]) and not any(k in text for k in ["ios", "android", "mobile"]):
        score += 18
        category = "tool"
        reason_parts.append("инструмент")
    elif any(k in text for k in ["meme", "weird", "strange", "tiny", "bizarre", "странн", "необычн"]):
        score += 18
        category = "meme"
        reason_parts.append("вирусный/необычный")
    elif any(k in text for k in ["privacy", "data", "tracking", "слеж", "приватн"]):
        score += 16
        category = "privacy"
        reason_parts.append("приватность")
    elif any(k in text for k in ["agent", "agents", "computer use", "operator"]):
        score += 20
        category = "agent"
        reason_parts.append("агенты")
    elif any(k in text for k in ["gpt", "claude", "gemini", "llama", "mistral", "qwen", "model"]):
        score += 18
        category = "model"
        reason_parts.append("модель")
    elif any(k in text for k in ["benchmark", "reasoning", "coding", "developer", "repo", "github", "open source", "open-source", "sdk", "api"]):
        score += 18
        category = "dev"
        reason_parts.append("разработка/GitHub")
    elif any(k in text for k in ["paper", "research", "arxiv", "study", "dataset", "eval"]):
        score += 12
        category = "research"
        reason_parts.append("исследование")

    if any(k in text for k in ["free", "бесплатн"]):
        score += 20
        reason_parts.append("бесплатно")
    if any(k in text for k in ["github", "open source", "open-source"]):
        score += 18
        reason_parts.append("open-source/GitHub")
    if any(k in text for k in ["course", "курс", "guide", "tutorial", "prompt"]):
        score += 12
    if any(k in text for k in ["fail", "bug", "drama", "ban", "leak", "privacy", "data", "lawsuit", "hack", "слеж", "утеч", "бан", "баг"]):
        score += 25
        reason_parts.append("приватность/данные")
    if any(k in text for k in ["app", "ios", "android", "chrome extension", "browser extension"]):
        score += 25
    if any(k in text for k in ["image", "video", "voice", "avatar", "music", "design", "editor"]):
        score += 25
        reason_parts.append("картинки/видео/голос")
    if any(k in text for k in ["reddit", "x.com", "twitter", "tiktok", "product hunt"]):
        score += 20
        reason_parts.append("соцсети/вирусность")
    if any(k in text for k in ["weird", "strange", "tiny", "unusual", "bizarre", "странн", "необычн"]):
        score += 15

    if any(k in text for k in ["funding", "raises", "partnership", "enterprise", "earnings", "quarterly", "acquisition"]):
        score -= 25
        category = "business"
        reason_parts.append("штраф: сухая корп-тема")
    if any(k in text for k in ["policy", "regulation"]) and not any(k in text for k in ["privacy", "drama", "leak", "lawsuit", "утеч"]):
        score -= 20
    if any(k in text for k in ["release", "launch", "announced", "announces", "announcement", "press release", "officially announced"]) and category == "other":
        category = "news"
        reason_parts.append("новость/релиз")

    if len(title) > 130:
        score -= 10
    score = max(0, min(100, score))
    if not reason_parts:
        reason_parts.append("нейтральная тема")
    return score, category, ", ".join(reason_parts)


def humanize_topic_reason_ru(category: str, score: int, source_group: str, reason: str) -> str:
    """Convert technical scoring fragments into an admin-friendly Russian reason."""
    category = (category or "other").strip().lower()
    source_group = (source_group or "other").strip().lower()
    reason_text = (reason or "").strip().lower()

    score_label = "высокий вес" if score >= 75 else "хороший вес" if score >= 55 else "базовый вес"
    source_bits = {
        "github": "это open-source проект с GitHub",
        "community": "тему обсуждают в сообществах",
        "tools": "это похоже на инструмент, который можно показать на практике",
        "official_ai": "это новость из официального AI-источника",
        "tech_media": "это новость из техно-медиа",
        "ru_tech": "это материал из русскоязычного техно-источника",
        "custom": "это тема из добавленного вручную источника",
    }
    category_bits = {
        "tool": "его можно быстро разобрать в формате короткого полезного поста",
        "dev": "его можно показать разработчикам и тем, кто следит за AI-инструментами",
        "agent": "это связано с AI-агентами и рабочими процессами",
        "model": "это связано с моделями и может быть интересно аудитории, которая следит за новыми возможностями AI",
        "creator": "из этого может получиться наглядный пост про создание контента",
        "mobile": "это можно подать как практическую AI-возможность в приложении или расширении",
        "drama": "в теме есть конфликт, риск или проблема, которую легко объяснить аудитории",
        "meme": "в теме есть необычный или вирусный крючок",
        "guide": "из этого можно сделать практичный мини-гайд",
        "privacy": "здесь есть понятный угол про данные, приватность или безопасность",
        "research": "это можно объяснить как важное исследование без лишней академичности",
        "news": "это можно коротко пересказать как AI-новость с выводом для читателя",
        "business": "тему стоит брать только если получится объяснить пользу для обычного читателя",
        "other": "ее можно проверить и превратить в короткий пост, если есть понятная польза",
    }

    source_part = source_bits.get(source_group, "источник выглядит релевантным для AI-тематики")
    category_part = category_bits.get(category, category_bits["other"])
    if "open-source" in reason_text or "github" in reason_text:
        source_part = "это open-source проект с GitHub"
    if "бесплат" in reason_text:
        category_part += ", а бесплатность может усилить интерес"
    if "штраф" in reason_text and score < 55:
        return f"Тема получила {score_label}: {source_part}, но выглядит суховато — перед постом стоит проверить, есть ли понятная польза."
    return f"Тема набрала {score_label}, потому что {source_part}, и {category_part}."
