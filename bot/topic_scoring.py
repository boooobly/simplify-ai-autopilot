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

    if any(k in text for k in ["free", "бесплатн", "course", "курс", "guide", "tutorial", "prompt", "github", "open source"]):
        score += 30
    if any(k in text for k in ["fail", "bug", "drama", "ban", "leak", "privacy", "data", "lawsuit", "hack", "слеж", "утеч", "бан", "баг"]):
        score += 25
    if any(k in text for k in ["app", "ios", "android", "chrome extension", "browser extension"]):
        score += 25
    if any(k in text for k in ["image", "video", "voice", "avatar", "music", "design", "editor"]):
        score += 25
    if any(k in text for k in ["reddit", "x.com", "twitter", "tiktok", "product hunt"]):
        score += 20
    if any(k in text for k in ["weird", "strange", "tiny", "unusual", "bizarre", "странн", "необычн"]):
        score += 15

    if any(k in text for k in ["funding", "raises", "partnership", "enterprise", "earnings", "quarterly", "acquisition"]):
        score -= 25
        category = "business"
        reason_parts.append("сухая корп-тема")
    if any(k in text for k in ["policy", "regulation"]) and not any(k in text for k in ["privacy", "drama", "leak", "lawsuit", "утеч"]):
        score -= 20
    if any(k in text for k in ["announces", "announcement", "press release", "officially announced"]):
        score -= 15

    if len(title) > 130:
        score -= 10
    score = max(0, min(100, score))
    if not reason_parts:
        reason_parts.append("нейтральная тема")
    return score, category, ", ".join(reason_parts)
